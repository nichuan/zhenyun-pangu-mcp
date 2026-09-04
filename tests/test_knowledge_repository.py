"""知识库仓储层的无网络回归测试。"""
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp.knowledge_base import repository as repo  # noqa: E402


def test_template_keyword_search_uses_ranked_rpc(monkeypatch):
    calls = []

    def fake_rpc(function, payload):
        calls.append((function, payload))
        return [{"id": 2, "title": "订单查询"}]

    monkeypatch.setattr(repo.sb, "rpc", fake_rpc)
    rows = repo.search_templates_keyword(
        "订单", category="query", system="pangu", business_domain="采购", verified_only=True, limit=3,
    )

    assert rows == [{"id": 2, "title": "订单查询"}]
    assert calls == [("search_sql_templates_keyword", {
        "keyword": "订单",
        "match_count": 3,
        "p_category": "query",
        "p_system": "pangu",
        "p_business_domain": "采购",
        "p_verified_only": True,
    })]


def test_template_keyword_search_legacy_fallback_scans_more_than_limit(monkeypatch):
    def unavailable(*_args, **_kwargs):
        raise RuntimeError("old schema")

    monkeypatch.setattr(repo.sb, "rpc", unavailable)
    rows = [
        {"id": 1, "title": "不匹配"},
        {"id": 2, "title": "订单查询"},
    ]
    monkeypatch.setattr(repo.sb, "query_table", lambda *args, **kwargs: rows)

    result = repo.search_templates_keyword("订单", limit=1)

    assert [row["id"] for row in result] == [2]


def test_template_usage_is_incremented_by_rpc(monkeypatch):
    calls = []
    monkeypatch.setattr(repo.sb, "rpc", lambda function, payload: calls.append((function, payload)))

    repo.increment_template_usage(7)

    assert calls == [("increment_template_usage", {"p_template_id": 7})]


def test_semantic_search_uses_provider_specific_voyage_rpc(monkeypatch):
    calls = []
    fake_embedding = SimpleNamespace(
        available=True,
        embed_query=lambda _query: [0.1, 0.2],
        rpc_name=lambda resource: {"knowledge": "match_knowledge_docs_voyage"}[resource],
        to_literal=lambda vector: str(vector),
    )
    monkeypatch.setattr(repo.sb, "embedding", fake_embedding)
    monkeypatch.setattr(repo.sb, "rpc", lambda function, payload: calls.append((function, payload)) or [{"id": 1}])

    rows = repo.search_knowledge_semantic("报价", limit=3)

    assert rows == [{"id": 1}]
    assert calls == [("match_knowledge_docs_voyage", {
        "query_embedding": "[0.1, 0.2]",
        "match_threshold": 0.5,
        "match_count": 3,
        "p_knowledge_type": None,
        "p_system": None,
        "p_module": None,
        "p_status": None,
    })]
