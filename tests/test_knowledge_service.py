"""知识服务的组合检索与并发回归测试。"""
import os
import sys
import threading

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp.knowledge_base import service  # noqa: E402


def _barrier_result(barrier, value):
    barrier.wait(timeout=2)
    return value


def test_search_pangu_runs_independent_searches_concurrently(monkeypatch):
    barrier = threading.Barrier(3)
    monkeypatch.setattr(
        service.repo, "search_knowledge_keyword",
        lambda *_args, **_kwargs: _barrier_result(barrier, []),
    )
    monkeypatch.setattr(
        service.repo, "search_templates_keyword",
        lambda *_args, **_kwargs: _barrier_result(barrier, []),
    )
    monkeypatch.setattr(
        service.repo, "search_tables_keyword",
        lambda *_args, **_kwargs: _barrier_result(barrier, []),
    )

    result = service.search_pangu("订单状态", system="盘古")

    assert "统一搜索" in result


def test_hybrid_template_search_runs_keyword_and_semantic_concurrently(monkeypatch):
    barrier = threading.Barrier(2)
    monkeypatch.setattr(
        service.repo, "search_templates_keyword",
        lambda *_args, **_kwargs: _barrier_result(
            barrier,
            [{"id": 1, "title": "关键词", "scenario": "场景", "sql_text": "SELECT 1"}],
        ),
    )
    monkeypatch.setattr(
        service.repo, "search_templates_semantic",
        lambda *_args, **_kwargs: _barrier_result(barrier, []),
    )

    result = service.search_sql_templates("订单状态", use_semantic=True)

    assert "关键词" in result


def test_semantic_failure_is_visible_without_discarding_keyword_results(monkeypatch):
    monkeypatch.setattr(
        service.repo, "search_knowledge_keyword",
        lambda *_args, **_kwargs: [{"id": 1, "title": "订单规则"}],
    )
    monkeypatch.setattr(
        service.repo, "search_knowledge_semantic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("embedding offline")),
    )

    result = service.search_knowledge("订单", use_semantic=True)

    assert "订单规则" in result
    assert "语义检索失败" in result
    assert "embedding offline" in result


def test_keyword_failure_is_not_reported_as_empty_search(monkeypatch):
    monkeypatch.setattr(
        service.repo, "search_knowledge_keyword",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("database offline")),
    )

    result = service.search_knowledge("订单", use_semantic=False)

    assert "知识库操作失败" in result
    assert "未检索到匹配知识" not in result


def test_candidate_table_relations_are_loaded_concurrently(monkeypatch):
    search_barrier = threading.Barrier(3)
    relation_barrier = threading.Barrier(2)
    monkeypatch.setattr(
        service.repo, "search_knowledge_keyword",
        lambda *_args, **_kwargs: _barrier_result(search_barrier, []),
    )
    monkeypatch.setattr(
        service.repo, "search_templates_keyword",
        lambda *_args, **_kwargs: _barrier_result(search_barrier, []),
    )
    monkeypatch.setattr(
        service.repo, "search_tables_keyword",
        lambda *_args, **_kwargs: _barrier_result(search_barrier, [
            {"table_name": "table_a", "db_name": "srm", "domain": "test"},
            {"table_name": "table_b", "db_name": "srm", "domain": "test"},
        ]),
    )
    monkeypatch.setattr(
        service.repo, "get_relations",
        lambda table: _barrier_result(relation_barrier, [{
            "from_table": table,
            "to_table": "common",
            "relation_type": "ref",
            "join_on": f"{table}.id = common.id",
        }]),
    )

    result = service.diagnose_context("订单状态", system="盘古")

    assert "table_a" in result
    assert "table_b" in result
