"""Standalone (Marmot) script decoding, cache, range, search, and tool exposure tests."""
import base64
import hashlib
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import server, standalone_scripts  # noqa: E402


def _encoded(source: str, encoding: str = "utf-16-le", trailing: bytes = b"") -> str:
    return base64.b64encode(source.encode(encoding) + trailing).decode("ascii")


def test_decode_base64_test_input_does_not_establish_source_mapping():
    # Historical longValue1 sample is test input, NOT a script/template body.
    # Keep as an encoding fixture only; repository tests verify source selection.
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


@pytest.mark.parametrize("source", [
    "// 中文注释\nfunction process(input) { return input; }",
    "const value = input.value;",
    "not base64!",
])
def test_decode_script_content_accepts_plain_source(source):
    assert standalone_scripts.decode_script_content(source) == source


@pytest.mark.parametrize("content", ["\x00\x01\x02", 123])
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


def test_only_script_identity_discovery_tools_are_exposed_by_default():
    tools = server.mcp._tool_manager._tools

    assert "search_standalone_scripts" in tools
    assert "search_adapter_scripts" in tools
    assert "get_standalone_script_info" not in tools
    assert "get_standalone_script_source" not in tools
    assert "search_standalone_script_source" not in tools
    assert "get_adapter_script_info" not in tools
    assert "get_adapter_script_source" not in tools
    assert "search_adapter_script_source" not in tools


def test_tool_rejects_empty_search_before_backend_access():
    with pytest.raises(ValueError, match="tenant、query"):
        server.search_standalone_scripts()


@pytest.fixture
def stored_script(monkeypatch):
    """Exercise real repository SELECT/projection, not a preselected fake source."""
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("""
        CREATE TABLE spfm_rel_table_record (
            id INTEGER PRIMARY KEY, table_code TEXT, value1 TEXT, value2 TEXT,
            value3 TEXT, value4 TEXT, value5 TEXT, creation_date TEXT,
            last_update_date TEXT, longValue TEXT, longValue1 TEXT,
            longValue2 TEXT, longValue3 TEXT, longValue4 TEXT, longValue5 TEXT
        )
    """)
    source = "function process(input) {\n  return input.actualSourceMarker;\n}"
    connection.execute(
        """INSERT INTO spfm_rel_table_record
           (id, table_code, value2, value3, last_update_date, longValue1, longValue5)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (42, "marmot_script_library", "TEST-TENANT", "TEST_SCRIPT",
         "2026-09-16 00:00:00", _encoded('{"testInputOnly": true}'), _encoded(source)),
    )
    queries = []

    class Client:
        def query(self, sql, instance, db, limit):
            queries.append(sql)
            return {"rows": [dict(row) for row in connection.execute(sql)]}

    repository = standalone_scripts.StandaloneScriptRepository()
    monkeypatch.setattr(repository, "_connection", lambda *args: (Client(), "test", "srm"))
    service = standalone_scripts.StandaloneScriptService(repository)
    yield connection, service, source, queries
    connection.close()


def test_repository_reads_actual_source_when_test_input_is_present(stored_script):
    _, service, source, queries = stored_script
    result = service.get_source(42, full=True)
    assert result["source"] == source
    assert result["source_hash"] == hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert result["source_column"] == "longValue5"
    assert service.get_info(42)["source_column"] == "longValue5"
    assert all("longValue1" not in sql for sql in queries)
    assert service.search_source(42, "testInputOnly")["match_count"] == 0
    found = service.search_source(42, "actualSourceMarker", context_lines=0)
    assert found["matches"][0]["line"] == 2
    assert found["source_column"] == "longValue5"


def test_repository_returns_plain_text_source_from_long_value5(stored_script):
    connection, service, source, _ = stored_script
    connection.execute("UPDATE spfm_rel_table_record SET longValue5 = ? WHERE id = 42", (source,))
    result = service.get_source(42, full=True)
    assert result["source"] == source
    assert result["source_column"] == "longValue5"
    assert result["stored_size"] == len(source)


@pytest.mark.parametrize("value", [None, "", " \n\t"])
def test_empty_source_never_falls_back_to_test_input(stored_script, value):
    connection, service, _, _ = stored_script
    connection.execute("UPDATE spfm_rel_table_record SET longValue5 = ? WHERE id = 42", (value,))
    result = service.get_source(42, full=True)
    assert result["source"] == ""
    assert result["total_lines"] == 0
    assert service.search_source(42, "testInputOnly")["match_count"] == 0


@pytest.mark.parametrize("value", [b"unexpected binary", "\x00\x01"])
def test_invalid_source_fails_instead_of_using_valid_test_input(stored_script, value):
    connection, service, _, _ = stored_script
    connection.execute("UPDATE spfm_rel_table_record SET longValue5 = ? WHERE id = 42", (value,))
    with pytest.raises(standalone_scripts.ScriptDecodeError):
        service.get_source(42, full=True)


def test_missing_source_projection_fails_closed(monkeypatch):
    class Client:
        def query(self, *args):
            return {"rows": [{"longValue1": _encoded('{"testInputOnly": true}')}]}

    repository = standalone_scripts.StandaloneScriptRepository()
    monkeypatch.setattr(repository, "_connection", lambda *args: (Client(), "test", "srm"))
    with pytest.raises(standalone_scripts.ScriptDecodeError, match="缺少源码字段"):
        repository.get_encoded_source(42)


def test_old_slot_cache_entry_cannot_be_reused(stored_script):
    _, service, source, _ = stored_script
    service.cache.put("cn:default:srm:42:2026-09-16 00:00:00", '{"testInputOnly": true}', 42)
    result = service.get_source(42, full=True)
    assert result["source"] == source
    assert result["performance"]["cache"] == "miss"
