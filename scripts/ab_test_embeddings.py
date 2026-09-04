"""NVIDIA / Voyage 独立向量检索 A/B 报告。

输入 JSON 可以是字符串数组，也可以是：
  [{"query": "供应商报价在哪张表", "expected_ids": [12, 18]}]

示例：
  uv run python scripts/ab_test_embeddings.py \
    --table knowledge_docs --queries-file scripts/ab_queries.example.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import supabase_client as sb  # noqa: E402
from zhenyun_pangu_mcp.embedding.base import EmbeddingProvider  # noqa: E402
from zhenyun_pangu_mcp.embedding.factory import create_embedding_provider  # noqa: E402


RESOURCE_BY_TABLE = {
    "knowledge_docs": "knowledge",
    "sql_templates": "template",
    "table_catalog": "table",
}


def _load_queries(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("queries 文件必须是 JSON 数组。")
    queries: list[dict[str, Any]] = []
    for item in raw:
        if isinstance(item, str):
            queries.append({"query": item})
        elif isinstance(item, dict) and isinstance(item.get("query"), str):
            queries.append(item)
        else:
            raise ValueError("queries 数组中的每项必须是字符串或包含 query 的对象。")
    return queries


def _search(provider: EmbeddingProvider, resource: str, query: str, limit: int, threshold: float) -> list[dict[str, Any]]:
    vector = provider.embed_query(query)
    payload: dict[str, Any] = {"query_embedding": provider.to_literal(vector), "match_count": limit}
    if resource in {"knowledge", "template"}:
        payload["match_threshold"] = threshold
    if resource == "knowledge":
        payload.update({"p_knowledge_type": None, "p_system": None, "p_module": None, "p_status": None})
    elif resource == "template":
        payload.update({"p_category": None, "p_system": None, "p_business_domain": None, "p_verified_only": False})
    else:
        payload.update({"filter_domain": None, "filter_db": None})
    return sb.rpc(provider_rpc(provider, resource), payload)


def provider_rpc(provider: EmbeddingProvider, resource: str) -> str:
    names = {
        "nvidia": {"knowledge": "match_knowledge_docs", "template": "match_sql_templates", "table": "search_table_catalog"},
        "voyage": {"knowledge": "match_knowledge_docs_voyage", "template": "match_sql_templates_voyage", "table": "search_table_catalog_voyage"},
    }
    return names[provider.provider_name][resource]


def _metrics(rows: list[dict[str, Any]], expected_ids: list[Any]) -> dict[str, Any]:
    if not expected_ids:
        return {}
    ids = [row.get("id") for row in rows]
    expected = set(expected_ids)
    rank = next((index + 1 for index, row_id in enumerate(ids) if row_id in expected), None)
    return {
        "hit_at_1": bool(rank and rank <= 1),
        "hit_at_3": bool(rank and rank <= 3),
        "hit_at_5": bool(rank and rank <= 5),
        "mrr": (1 / rank) if rank else 0.0,
        "first_expected_rank": rank,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="输出 NVIDIA / Voyage 语义检索 A/B 报告")
    parser.add_argument("--table", choices=tuple(RESOURCE_BY_TABLE), default="knowledge_docs")
    parser.add_argument("--queries-file", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--threshold", type=float, default=None)
    parser.add_argument("--output", type=Path, default=None, help="可选：同时写入 JSON 报告")
    args = parser.parse_args(argv)
    if args.limit < 1:
        print("❌ --limit 必须大于 0。")
        return 2
    threshold = args.threshold if args.threshold is not None else sb.config.get_semantic_match_threshold()
    try:
        queries = _load_queries(args.queries_file)
        providers = [create_embedding_provider("nvidia"), create_embedding_provider("voyage")]
        resource = RESOURCE_BY_TABLE[args.table]
        report: list[dict[str, Any]] = []
        for item in queries:
            query = item["query"]
            expected_ids = item.get("expected_ids") or []
            result: dict[str, Any] = {"query": query, "expected_ids": expected_ids}
            for provider in providers:
                rows = _search(provider, resource, query, args.limit, threshold)
                result[provider.provider_name] = {
                    "results": [
                        {"id": row.get("id"), "title": row.get("title") or row.get("table_name"), "similarity": row.get("similarity")}
                        for row in rows
                    ],
                    "metrics": _metrics(rows, expected_ids),
                }
            report.append(result)
    except Exception as exc:  # noqa: BLE001 - provider/API/输入错误统一返回失败
        print(f"❌ A/B 测试失败：{exc}")
        return 1

    rendered = json.dumps(report, ensure_ascii=False, indent=2)
    print(rendered)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
        print(f"报告已写入：{args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
