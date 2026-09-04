"""Standalone (Marmot) script decoding, cache, range, search, and tool exposure tests."""
import base64
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import server, standalone_scripts  # noqa: E402


def _encoded(source: str, encoding: str = "utf-16-le", trailing: bytes = b"") -> str:
    return base64.b64encode(source.encode(encoding) + trailing).decode("ascii")


def test_decode_real_utf16le_template_sample():
    # Row captured on dev (SRM-PECHION print template, 2026-09-05).
    content = ("AHsACgAgACAAIAAgACIAdABlAG4AYQBuAHQASQBkACIAOgA3ADMAMQAsAAoA"
               "IAAgACAAIAAiAHMAZQB0AHQAbABlAEgAZQBhAGQAZQByAEkAZAAiADoANQAzADkANAA0AAoAfQ==")
    assert standalone_scripts.decode_script_content(content) == (
        '{\n    "tenantId":731,\n    "settleHeaderId":53944\n}'
    )


@pytest.mark.parametrize(
    "source",
    [
        "function run() { return 1; }",
        "function 处理(input) { return input.供应商; }",
        '{"tenantId": 731, "settleHeaderId": 53944}',
        "",
    ],
)
def test_decode_script_content_multibyte_sources(source):
    assert standalone_scripts.decode_script_content(_encoded(source)) == source


def test_decode_script_content_plain_utf8():
    raw = base64.b64encode("const x = 1;".encode("utf-8")).decode("ascii")
    assert standalone_scripts.decode_script_content(raw) == "const x = 1;"
    assert standalone_scripts.decode_script_content(None) == ""


@pytest.mark.parametrize("content", ["%%%", 123])
def test_decode_script_content_rejects_invalid_content(content):
    with pytest.raises(standalone_scripts.ScriptDecodeError):
        standalone_scripts.decode_script_content(content)


def test_binary_garbage_is_rejected():
    encoded = base64.b64encode(bytes(range(256))).decode("ascii")
    with pytest.raises(standalone_scripts.ScriptDecodeError):
        standalone_scripts.decode_script_content(encoded)


class FakeRepository:
    def __init__(self, source: str):
        self.source = source
        self.updated_at = "2026-09-05 00:20:05"
        self.metadata_calls = 0
        self.source_calls = 0

    def _metadata(self, script_id):
        return {
            "script_id": script_id,
            "type_flag": "2",
            "tenant_num": "SRM-PECHION",
            "task_code": "SCUX_SRM_PECHION_PAYMENT_STATEMENT_PDF_PRINT_ADAPTOR",
            "description": "srm-84641，百雀羚付款结算单打印.",
            "content_kind": None,
            "source_updated_at": self.updated_at,
        }

    def search(self, **kwargs):
        return [self._metadata(91890309802811180)], 1.25

    def get_metadata(self, script_id, **kwargs):
        self.metadata_calls += 1
        return self._metadata(script_id), 1.0

    def get_encoded_source(self, script_id, **kwargs):
        self.source_calls += 1
        return _encoded(self.source), None, 2.0


@pytest.fixture
def service():
    source = "\n".join([
        "function process(input) {",
        "  const settleHeaderId = input.settleHeaderId;",
        "  return settleHeaderId;",
        "}",
    ])
    repository = FakeRepository(source)
    cache = standalone_scripts.DecodedScriptCache(max_entries=2, ttl_seconds=60)
    return standalone_scripts.StandaloneScriptService(repository, cache), repository


def test_second_source_read_hits_decoded_cache(service):
    script_service, repository = service

    first = script_service.get_source(91890309802811180, full=True)
    second = script_service.get_source(91890309802811180, full=True)

    assert first["source"] == second["source"]
    assert first["performance"]["cache"] == "miss"
    assert second["performance"]["cache"] == "hit"
    assert repository.source_calls == 1


def test_updated_at_change_invalidates_cache(service):
    script_service, repository = service
    script_service.get_source(91890309802811180, full=True)
    repository.updated_at = "2026-09-06 00:00:00"

    result = script_service.get_source(91890309802811180, full=True)

    assert result["performance"]["cache"] == "miss"
    assert repository.source_calls == 2


def test_search_requires_a_scope(service):
    script_service, _ = service
    with pytest.raises(standalone_scripts.AdapterScriptError):
        script_service.search_scripts()


def test_search_result_carries_storage_context(service):
    script_service, _ = service
    result = script_service.search_scripts(tenant="SRM-PECHION")

    assert result["count"] == 1
    assert result["storage"]["table_code"] == "marmot_script_library"
    assert result["scripts"][0]["tenant_num"] == "SRM-PECHION"


def test_source_range_and_search(service):
    script_service, _ = service
    ranged = script_service.get_source(91890309802811180, start_line=2, end_line=2)
    assert ranged["source"] == "  const settleHeaderId = input.settleHeaderId;"

    found = script_service.search_source(91890309802811180, "SETTLEHEADERID", context_lines=1)
    assert found["match_count"] == 2
    assert "settleHeaderId" in found["matches"][0]["source"]

    with pytest.raises(standalone_scripts.AdapterScriptError):
        script_service.search_source(91890309802811180, "[", regex=True)


def test_standalone_script_tools_are_exposed():
    tools = server.mcp._tool_manager._tools

    assert "search_standalone_scripts" in tools
    assert "get_standalone_script_info" in tools
    assert "get_standalone_script_source" in tools
    assert "search_standalone_script_source" in tools
    # 历史工具仍保留为适配器脚本检索
    assert "search_adapter_scripts" in tools
    assert "get_adapter_script_source" in tools


def test_tool_error_shape_is_standardized():
    result = json.loads(server.search_standalone_scripts())

    assert result["ok"] is False
    assert result["error"]["code"] == "standalone_script"
    assert result["error"]["retryable"] is False
