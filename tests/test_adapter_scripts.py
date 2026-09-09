"""Adapter script decoding, cache, range, search, and tool exposure tests."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import adapter_scripts, server  # noqa: E402


def _encoded(source: str, trailing: bytes = b"") -> str:
    return base64.b64encode(source.encode("utf-16-be") + trailing).decode("ascii")


@pytest.mark.parametrize(
    "source",
    [
        "function run() { return 1; }",
        "function 处理(input) { return input.供应商; }",
        "const text = `引号: \\\"'\\n换行`;\n// ✓",
        "",
    ],
)
def test_decode_script_content_utf16be(source):
    assert adapter_scripts.decode_script_content(_encoded(source)) == source


def test_decode_script_content_accepts_null_and_legacy_trailing_byte():
    assert adapter_scripts.decode_script_content(None) == ""
    assert adapter_scripts.decode_script_content(_encoded("const x = 1;", b"x")) == "const x = 1;"


@pytest.mark.parametrize("content", ["%%%", "2AA=", 123])
def test_decode_script_content_rejects_invalid_content(content):
    with pytest.raises(adapter_scripts.ScriptDecodeError):
        adapter_scripts.decode_script_content(content)


class FakeRepository:
    def __init__(self, source: str):
        self.source = source
        self.version = "1"
        self.source_version = 1
        self.metadata_calls = 0
        self.source_calls = 0

    def _metadata(self, script_id):
        return {
            "script_id": script_id,
            "header_id": 9,
            "task_code": "SAMPLE_AFTER_HANDLE",
            "description": "外部接口字段映射",
            "apply_tenant_num": "SRM-DEMO",
            "running_service": "srm-source",
            "enabled_flag": 1,
            "script_version": self.version,
            "source_version": self.source_version,
            "source_updated_at": "2026-09-04 10:00:00",
            "script_type": "JS",
            "priority": 1,
        }

    def search(self, **kwargs):
        return [self._metadata(11)], 1.25

    def get_metadata(self, script_id, **kwargs):
        self.metadata_calls += 1
        return self._metadata(script_id), 1.0

    def get_encoded_source(self, script_id, **kwargs):
        self.source_calls += 1
        return _encoded(self.source), 2.0


@pytest.fixture
def service():
    source = "\n".join([
        "function beforeSubmit(input) {",
        "  const supplierId = input.supplierId;",
        "  return supplierId;",
        "}",
        "function afterSubmit() { return true; }",
    ])
    repository = FakeRepository(source)
    cache = adapter_scripts.DecodedScriptCache(max_entries=2, ttl_seconds=60)
    return adapter_scripts.AdapterScriptService(repository, cache), repository


def test_second_source_read_hits_decoded_cache(service):
    script_service, repository = service

    first = script_service.get_source(11, full=True)
    second = script_service.get_source(11, full=True)

    assert first["source"] == second["source"]
    assert first["performance"]["cache"] == "miss"
    assert second["performance"]["cache"] == "hit"
    assert repository.source_calls == 1
    assert repository.metadata_calls == 2


def test_script_version_change_invalidates_cache(service):
    script_service, repository = service
    script_service.get_source(11, full=True)
    repository.version = "2"

    result = script_service.get_source(11, full=True)

    assert result["performance"]["cache"] == "miss"
    assert repository.source_calls == 2


def test_source_row_version_change_invalidates_cache(service):
    script_service, repository = service
    script_service.get_source(11, full=True)
    repository.source_version = 2

    result = script_service.get_source(11, full=True)

    assert result["performance"]["cache"] == "miss"
    assert repository.source_calls == 2


def test_cache_is_isolated_by_environment(service):
    script_service, repository = service
    script_service.get_source(11, full=True, site="cn", instance="test")
    result = script_service.get_source(11, full=True, site="cn", instance="prod")

    assert result["performance"]["cache"] == "miss"
    assert repository.source_calls == 2


def test_cache_evicts_least_recently_used_entry():
    cache = adapter_scripts.DecodedScriptCache(max_entries=2, ttl_seconds=60)
    cache.put("one", "1", 4)
    cache.put("two", "2", 4)
    assert cache.get("one") is not None
    cache.put("three", "3", 4)

    assert cache.get("one") is not None
    assert cache.get("two") is None
    assert cache.get("three") is not None


def test_info_is_lightweight_and_uses_cached_sizes(service):
    script_service, repository = service
    cold = script_service.get_info(11)
    assert cold["source_cached"] is False
    assert repository.source_calls == 0

    script_service.get_source(11, full=True)
    warm = script_service.get_info(11)
    assert warm["source_cached"] is True
    assert warm["source_length"] > 0
    assert "source_hash" in warm


def test_source_range_and_boundaries(service):
    script_service, _ = service
    result = script_service.get_source(11, start_line=2, end_line=3)

    assert result["start_line"] == 2
    assert result["end_line"] == 3
    assert result["source"].splitlines() == [
        "  const supplierId = input.supplierId;",
        "  return supplierId;",
    ]

    with pytest.raises(adapter_scripts.AdapterScriptError):
        script_service.get_source(11, start_line=3, end_line=2)
    with pytest.raises(adapter_scripts.AdapterScriptError):
        script_service.get_source(11, start_line=99)


def test_source_search_literal_case_and_no_match(service):
    script_service, _ = service
    found = script_service.search_source(11, "SUPPLIERID", context_lines=1)
    missing = script_service.search_source(11, "不存在")

    assert found["match_count"] == 2
    assert found["matches"][0]["line"] == 2
    assert "supplierId" in found["matches"][0]["source"]
    assert missing["matches"] == []


def test_source_search_rejects_invalid_regex(service):
    script_service, _ = service
    with pytest.raises(adapter_scripts.AdapterScriptError):
        script_service.search_source(11, "[", regex=True)


def test_search_requires_a_scope(service):
    script_service, _ = service
    with pytest.raises(adapter_scripts.AdapterScriptError):
        script_service.search_scripts()


def test_disabled_gitlab_search_tools_are_not_exposed():
    tools = server.mcp._tool_manager._tools

    assert "gitlab_search_projects" not in tools
    assert "gitlab_search_code" not in tools
    assert "search_repo" in tools
    assert "gitlab_get_file" in tools
    assert "search_adapter_scripts" in tools
    assert "search_adapter_script_source" in tools


def test_disabled_gitlab_search_fails_fast_without_network():
    result = json.loads(server.gitlab_search_code("SomeClass"))

    assert result["ok"] is False
    assert result["error"]["code"] == "capability_disabled"
    assert result["error"]["retryable"] is False
