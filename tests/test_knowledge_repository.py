"""知识库仓储层的无网络回归测试。"""
import os
import sys
from types import SimpleNamespace

import pytest
import requests

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


def test_template_keyword_search_keeps_server_side_matches_beyond_client_window(monkeypatch):
    def fake_rpc(function, payload):
        assert function == "search_sql_templates_keyword"
        assert payload["match_count"] == 1
        # This represents a match ranked after the first unmatching row in the
        # table; the RPC filters and ranks before applying its own LIMIT.
        return [{"id": 99, "title": "窗口外仍命中的订单模板"}]

    monkeypatch.setattr(repo.sb, "rpc", fake_rpc)
    monkeypatch.setattr(
        repo.sb,
        "query_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("keyword search must not fetch a client-side limit window")
        ),
    )

    assert repo.search_templates_keyword("订单", limit=1) == [
        {"id": 99, "title": "窗口外仍命中的订单模板"}
    ]


def test_template_keyword_search_legacy_fallback_scans_more_than_limit(monkeypatch):
    def unavailable(*_args, **_kwargs):
        response = requests.Response()
        response.status_code = 404
        response._content = b'{"code":"PGRST202"}'
        raise requests.HTTPError("missing RPC", response=response)

    monkeypatch.setattr(repo.sb, "rpc", unavailable)
    rows = [
        {"id": 1, "title": "不匹配"},
        {"id": 2, "title": "订单查询"},
    ]
    monkeypatch.setattr(repo.sb, "query_table", lambda *args, **kwargs: rows)

    result = repo.search_templates_keyword("订单", limit=1)

    assert [row["id"] for row in result] == [2]


def test_keyword_search_does_not_turn_rpc_failure_into_no_results(monkeypatch):
    monkeypatch.setattr(repo.sb, "rpc", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")))

    with pytest.raises(RuntimeError, match="offline"):
        repo.search_knowledge_keyword("订单")


def test_template_keyword_search_does_not_fallback_on_server_error(monkeypatch):
    response = requests.Response()
    response.status_code = 503
    response._content = b'{"code":"PGRST003"}'
    monkeypatch.setattr(
        repo.sb, "rpc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            requests.HTTPError("database unavailable", response=response)
        ),
    )
    monkeypatch.setattr(
        repo.sb, "query_table",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not fall back")),
    )

    with pytest.raises(requests.HTTPError, match="database unavailable"):
        repo.search_templates_keyword("订单")


def test_template_keyword_legacy_scan_reports_incomplete_results(monkeypatch):
    response = requests.Response()
    response.status_code = 404
    response._content = b'{"code":"PGRST202"}'
    monkeypatch.setattr(
        repo.sb, "rpc",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            requests.HTTPError("missing RPC", response=response)
        ),
    )
    monkeypatch.setattr(repo.sb, "query_table", lambda *_args, **_kwargs: [{"title": "other"}] * 100)

    with pytest.raises(RuntimeError, match="结果不完整"):
        repo.search_templates_keyword("订单", limit=1)


def test_template_usage_is_incremented_by_rpc(monkeypatch):
    calls = []
    monkeypatch.setattr(repo.sb, "rpc", lambda function, payload: calls.append((function, payload)))

    repo.increment_template_usage(7)

    assert calls == [("increment_template_usage", {"p_template_id": 7})]


def test_semantic_search_uses_match_knowledge_docs_rpc(monkeypatch):
    calls = []
    fake_embedding = SimpleNamespace(
        available=True,
        embed_query=lambda _query: [0.1, 0.2],
        rpc_name=lambda resource: {"knowledge": "match_knowledge_docs"}[resource],
        to_literal=lambda vector: str(vector),
    )
    monkeypatch.setattr(repo.sb, "embedding", fake_embedding)
    monkeypatch.setattr(repo.sb, "rpc", lambda function, payload: calls.append((function, payload)) or [{"id": 1}])

    rows = repo.search_knowledge_semantic("报价", limit=3)

    assert rows == [{"id": 1}]
    assert calls == [("match_knowledge_docs", {
        "query_embedding": "[0.1, 0.2]",
        "match_threshold": 0.5,
        "match_count": 3,
        "p_knowledge_type": None,
        "p_system": None,
        "p_module": None,
        "p_status": None,
    })]
