"""批量重建 knowledge_docs / sql_templates / table_catalog 的 embedding。

特点：
* 统一使用 Cloudflare Workers AI（@cf/qwen/qwen3-embedding-0.6b，1024 维），
  向量写入单列 embedding；
* document 文本批量提交给 provider，避免逐行请求；
* 默认只处理 embedding 为 NULL 的行，每条更新成功后即可断点续跑；
* 换模型或需全量重算时加 --force（会覆盖该表全部已有向量）；
* API 失败按指数退避重试，失败批次不会写入假向量。

限速提示：Cloudflare 免费层每天 10,000 Neurons；若撞到速率限制，可用
--min-interval 增加批间间隔、--batch-size 调小（长文本 5~10、表目录短文本 30~50）。

示例：
  uv run python scripts/rebuild_embeddings.py --table knowledge_docs --limit 20
  uv run python scripts/rebuild_embeddings.py --table all --batch-size 50
  uv run python scripts/rebuild_embeddings.py --table all --force   # 全量重算
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import supabase_client as sb  # noqa: E402
from zhenyun_pangu_mcp.embedding.base import EmbeddingError, EmbeddingProvider  # noqa: E402
from zhenyun_pangu_mcp.embedding.factory import create_embedding_provider  # noqa: E402
from zhenyun_pangu_mcp.embedding.text import compose_knowledge, compose_table, compose_template  # noqa: E402


TABLE_BUILDERS: dict[str, Callable[[dict[str, Any]], str]] = {
    "knowledge_docs": compose_knowledge,
    "sql_templates": compose_template,
    "table_catalog": lambda row: compose_table(
        row.get("table_name") or "",
        row.get("table_comment") or "",
        row.get("description") or "",
        row.get("tags") or [],
    ),
}


def _table_name(logical_name: str) -> str:
    return {
        "knowledge_docs": sb.config.KNOWLEDGE_TABLE,
        "sql_templates": sb.config.SQL_TEMPLATE_TABLE,
        "table_catalog": sb.config.TABLE_CATALOG_TABLE,
    }[logical_name]


def _fetch_rows(
    table: str,
    vector_column: str,
    force: bool,
    limit: int,
    start_id: int | None,
    end_id: int | None,
) -> list[dict[str, Any]]:
    """分页拉全量行。

    PostgREST 单请求默认最多返回 1000 行，table_catalog 这种两千行级的表必须翻页，
    否则 --force 全量重算会静默只处理前一页。
    """
    page_size = 1000
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        params: dict[str, Any] = {
            "select": "*", "order": "id.asc", "limit": page_size, "offset": offset,
        }
        if not force:
            params[vector_column] = "is.null"
        if start_id is not None:
            params["id"] = f"gte.{start_id}"
        elif end_id is not None:
            params["id"] = "gte.0"
        page = sb._rest(table, params=params)
        rows.extend(page)
        offset += page_size
        if len(page) < page_size or (limit and len(rows) >= limit):
            break
    if end_id is not None:
        rows = [row for row in rows if int(row.get("id", 0)) <= end_id]
    if limit:
        rows = rows[:limit]
    return rows


def _embed_with_retry(
    provider: EmbeddingProvider,
    texts: list[str],
    max_retries: int,
    retry_delay: float,
) -> list[list[float]]:
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return provider.embed_documents(texts)
        except Exception as exc:  # noqa: BLE001 - provider normalizes API failures
            last_error = exc
            if attempt >= max_retries:
                break
            delay = retry_delay * (2**attempt)
            print(f"  batch API 失败，{delay:.1f}s 后重试（{attempt + 1}/{max_retries}）：{exc}")
            time.sleep(delay)
    raise EmbeddingError(f"批量 embedding 失败，已重试 {max_retries} 次：{last_error}") from last_error


def _write_row(
    table: str,
    row_id: Any,
    provider: EmbeddingProvider,
    vector: list[float],
    max_retries: int = 3,
    retry_delay: float = 1.0,
) -> None:
    """写回单行向量。网络抖动（如 SSLEOFError）按指数退避重试，失败才向上抛出。"""
    url = f"{sb.base_url()}/rest/v1/{table}?id=eq.{row_id}"
    body = {provider.vector_column: provider.to_literal(vector)}
    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            sb._rest_request("PATCH", url, body=body)
            return
        except Exception as exc:  # noqa: BLE001 - 网络抖动可重试
            last_error = exc
            if attempt >= max_retries:
                break
            time.sleep(retry_delay * (2**attempt))
    raise EmbeddingError(f"写回 {table}.id={row_id} 失败：{last_error}")


def _write_rows_concurrent(
    table: str,
    items: list[tuple[Any, list[float]]],
    provider: EmbeddingProvider,
    workers: int = 8,
) -> tuple[int, int]:
    """并发写回一批向量；单行失败只影响该行，返回 (成功数, 失败数)。"""
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(_write_row, table, row_id, provider, vector): row_id
            for row_id, vector in items
        }
        ok = failed = 0
        for future, row_id in futures.items():
            try:
                future.result()
                ok += 1
            except Exception as exc:  # noqa: BLE001 - 单行失败不影响其他行
                failed += 1
                print(f"    id={row_id} 写回失败：{exc}")
    return ok, failed


def _process_table(
    logical_name: str,
    provider: EmbeddingProvider,
    batch_size: int,
    force: bool,
    limit: int,
    start_id: int | None,
    end_id: int | None,
    max_retries: int,
    retry_delay: float,
    min_interval: float = 0.0,
) -> tuple[int, int]:
    table = _table_name(logical_name)
    rows = _fetch_rows(table, provider.vector_column, force, limit, start_id, end_id)
    print(f"[{logical_name}] provider={provider.provider_name} model={provider.model_name} 待处理 {len(rows)} 条")
    ok = failed = 0
    for offset in range(0, len(rows), batch_size):
        if offset and min_interval > 0:
            time.sleep(min_interval)
        batch = rows[offset : offset + batch_size]
        texts = [TABLE_BUILDERS[logical_name](row) for row in batch]
        usable = [(row, text) for row, text in zip(batch, texts) if text]
        if not usable:
            failed += len(batch)
            print(f"  [{offset + len(batch)}/{len(rows)}] 空文本，跳过")
            continue
        try:
            vectors = _embed_with_retry(
                provider, [text for _, text in usable], max_retries, retry_delay,
            )
            if len(vectors) != len(usable):
                raise EmbeddingError("provider 返回数量与批次不一致。")
            batch_ok, batch_failed = _write_rows_concurrent(
                table, list(zip((r.get("id") for r, _ in usable), vectors)), provider,
            )
            ok += batch_ok
            failed += batch_failed + (len(batch) - len(usable))
            print(f"  [{offset + len(batch)}/{len(rows)}] 成功 {batch_ok} 条，失败 {batch_failed} 条（维度 {provider.dimension}）")
        except Exception as exc:  # noqa: BLE001 - 记录失败批次并继续后续批次
            failed += len(batch)
            print(f"  [{offset + len(batch)}/{len(rows)}] 批次失败，未写入该批向量：{exc}")
    return ok, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量重建知识库 embedding（Cloudflare Workers AI，写入单列 embedding）")
    parser.add_argument("--table", choices=(*TABLE_BUILDERS.keys(), "all"), default="knowledge_docs")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--force", action="store_true", help="覆盖目标 provider 的已有向量；不会触碰另一 provider")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条，0 表示不限制")
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--end-id", type=int, default=None)
    # Cloudflare 免费层默认无严格 RPM 限制；如遇速率限制可通过 --min-interval 放缓。
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=2.0)
    parser.add_argument(
        "--min-interval", type=float, default=0.0,
        help="相邻两个批次之间的最小间隔秒数（默认 0；撞速率限制时再调大）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 1000:
        print("❌ --batch-size 必须在 1～1000 之间。")
        return 2
    if args.limit < 0 or args.max_retries < 0 or args.retry_delay < 0:
        print("❌ --limit / --max-retries / --retry-delay 不能为负数。")
        return 2
    if args.start_id is not None and args.end_id is not None and args.start_id > args.end_id:
        print("❌ --start-id 不能大于 --end-id。")
        return 2

    try:
        provider = create_embedding_provider()
    except Exception as exc:  # noqa: BLE001 - 配置错误需清晰输出
        print(f"❌ embedding provider 配置错误：{exc}")
        return 1

    logical_tables = list(TABLE_BUILDERS) if args.table == "all" else [args.table]
    total_ok = total_failed = 0
    for logical_name in logical_tables:
        ok, failed = _process_table(
            logical_name, provider, args.batch_size, args.force, args.limit,
            args.start_id, args.end_id, args.max_retries, args.retry_delay,
            args.min_interval,
        )
        total_ok += ok
        total_failed += failed
    print(f"完成：成功 {total_ok}，失败 {total_failed}。")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
