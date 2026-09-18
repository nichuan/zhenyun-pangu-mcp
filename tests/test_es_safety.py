"""ES 只读安全边界测试（H2 回归）。

覆盖：
1. 只读端点（含 _source）放行；
2. 写语义 / 双用途 / 未知路径一律 fail-closed 拒绝；
3. get() 必须校验「实际请求路径」，而不是借用 _search 放行。
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import es  # noqa: E402
from zhenyun_pangu_mcp.es import ESSafetyError, ESClient  # noqa: E402


@pytest.mark.parametrize(
    "path",
    [
        "swbh_card/_search",
        "srm_workbench/_count",
        "idx/_mget",
        "idx/_field_caps",
        "idx/_search_template",
        "idx/_source/abc123",  # get() 实际使用的只读端点
    ],
)
def test_check_readonly_allows_read_only_endpoints(path):
    es._check_readonly(path)


@pytest.mark.parametrize(
    "path",
    [
        "idx/_delete_by_query",
        "idx/_update/1",
        "idx/_bulk",
        "idx/_doc/1",
        "idx/_create/1",
        "idx/_reindex",
        "idx/_ingest/pipeline/x",
        "_aliases",  # 回归：旧实现写成 "_aliases?" 导致永不命中
        "idx/_mapping",
        "idx/_settings",
        "anything",  # 不在白名单的未知路径
    ],
)
def test_check_readonly_rejects_write_or_unknown_paths(path):
    with pytest.raises(ESSafetyError):
        es._check_readonly(path)


def test_get_validates_actual_request_path(monkeypatch):
    """get() 必须对真实路径（含 _source）做只读校验，而非借用 _search。"""
    seen = {}

    def fake_check(path):
        seen["path"] = path

    class _Resp:
        status_code = 404

    monkeypatch.setattr(es, "_check_readonly", fake_check)
    client = ESClient(base_url="http://es.local")
    monkeypatch.setattr(client.session, "get", lambda *a, **k: _Resp())

    result = client.get("my_index", "doc-1")

    assert seen["path"] == "my_index/_source/doc-1"
    assert result["found"] is False
    assert result["_id"] == "doc-1"
