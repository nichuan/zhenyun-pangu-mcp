"""批量重建 knowledge_docs / sql_templates / table_catalog 的 embedding。

特点：
* document 文本批量提交给 provider，避免逐行请求；
* Voyage 写入 embedding_voyage，NVIDIA 写入旧 embedding，互不覆盖；
* 每条数据库更新成功后即可从 NULL 过滤断点续跑；
* API 失败按指数退避重试，失败批次不会写入假向量。

示例：
  uv run python scripts/rebuild_embeddings.py --provider voyage --table knowledge_docs --limit 20
  uv run python scripts/rebuild_embeddings.py --provider voyage --table all --batch-size 50
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import datetime, timezone
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


def _metadata(provider: EmbeddingProvider) -> dict[str, Any]:
    if provider.provider_name != "voyage":
        return {}
    return {
        "embedding_voyage_provider": provider.provider_name,
        "embedding_voyage_model": provider.model_name,
        "embedding_voyage_dimension": provider.dimension,
        "embedding_voyage_updated_at": datetime.now(timezone.utc).isoformat(),
    }


def _fetch_rows(
    table: str,
    vector_column: str,
    force: bool,
    limit: int,
    start_id: int | None,
    end_id: int | None,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"select": "*", "order": "id.asc"}
    if not force:
        params[vector_column] = "is.null"
    if limit:
        params["limit"] = limit
    if start_id is not None:
        params["id"] = f"gte.{start_id}"
    elif end_id is not None:
        params["id"] = "gte.0"
    rows = sb._rest(table, params=params)
    if end_id is not None:
        rows = [row for row in rows if int(row.get("id", 0)) <= end_id]
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


def _write_row(table: str, row_id: Any, provider: EmbeddingProvider, vector: list[float]) -> None:
    url = f"{sb.base_url()}/rest/v1/{table}?id=eq.{row_id}"
    body = {provider.vector_column: provider.to_literal(vector), **_metadata(provider)}
    sb._rest_request("PATCH", url, body=body)


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
) -> tuple[int, int]:
    table = _table_name(logical_name)
    rows = _fetch_rows(table, provider.vector_column, force, limit, start_id, end_id)
    print(f"[{logical_name}] provider={provider.provider_name} model={provider.model_name} 待处理 {len(rows)} 条")
    ok = failed = 0
    for offset in range(0, len(rows), batch_size):
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
            for (row, _), vector in zip(usable, vectors):
                _write_row(table, row.get("id"), provider, vector)
                ok += 1
            failed += len(batch) - len(usable)
            print(f"  [{offset + len(batch)}/{len(rows)}] 成功 {len(usable)} 条（维度 {provider.dimension}）")
        except Exception as exc:  # noqa: BLE001 - 记录失败批次并继续后续批次
            failed += len(batch)
            print(f"  [{offset + len(batch)}/{len(rows)}] 批次失败，未写入该批向量：{exc}")
    return ok, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="批量重建知识库 embedding（支持 Voyage / NVIDIA）")
    parser.add_argument("--provider", choices=("voyage", "nvidia"), default=None)
    parser.add_argument("--table", choices=(*TABLE_BUILDERS.keys(), "all"), default="knowledge_docs")
    parser.add_argument("--batch-size", type=int, default=50)
    parser.add_argument("--force", action="store_true", help="覆盖目标 provider 的已有向量；不会触碰另一 provider")
    parser.add_argument("--limit", type=int, default=0, help="最多处理 N 条，0 表示不限制")
    parser.add_argument("--start-id", type=int, default=None)
    parser.add_argument("--end-id", type=int, default=None)
    parser.add_argument("--max-retries", type=int, default=3)
    parser.add_argument("--retry-delay", type=float, default=1.0)
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
        provider = create_embedding_provider(args.provider)
    except Exception as exc:  # noqa: BLE001 - 配置错误需清晰输出
        print(f"❌ embedding provider 配置错误：{exc}")
        return 1

    logical_tables = list(TABLE_BUILDERS) if args.table == "all" else [args.table]
    total_ok = total_failed = 0
    for logical_name in logical_tables:
        ok, failed = _process_table(
            logical_name, provider, args.batch_size, args.force, args.limit,
            args.start_id, args.end_id, args.max_retries, args.retry_delay,
        )
        total_ok += ok
        total_failed += failed
    print(f"完成：成功 {total_ok}，失败 {total_failed}。")
    return 1 if total_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
