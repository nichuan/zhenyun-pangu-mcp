"""Generic design-gate checks for Marmot requirement delivery."""

import json
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import delivery_quality, server  # noqa: E402


def test_static_check_applies_only_caller_supplied_business_rules():
    source = """
function process(input) {
  Logger.info("process start");
  const request = { window: { start: input.start }, forbiddenKey: 1 };
  if (_.isEmpty(row.entityId)) return input;
  return request;
}
"""
    rules = {
        "numeric_id_fields": ["entityId"],
        "forbidden_direct_fields": ["row.entityId"],
        "forbidden_service_parameters": ["forbiddenKey"],
        "required_object_parameters": {"window": ["start", "end"]},
        "required_log_stages": ["entry"],
    }

    result = delivery_quality.static_check_script(source, rules_json=json.dumps(rules))
    codes = {item["code"] for item in result["findings"]}

    assert result["status"] == "fail"
    assert "numeric_id_is_empty" in codes
    assert "forbidden_direct_field" in codes
    assert "forbidden_service_parameter" in codes
    assert "missing_nested_parameter" in codes


def test_static_check_does_not_invent_requirement_rules():
    source = """
function process(input) {
  Logger.info("process start");
  return { arbitraryField: input.value };
}
"""

    result = delivery_quality.static_check_script(source)

    assert result["status"] == "pass"
    assert result["applied_rules"] == {}


def test_script_trace_requires_explicit_time_window_before_backend_access():
    result = json.loads(server.query_script_trace("trace-1"))

    assert result["ok"] is False
    assert result["error"]["code"] == "time_required"


def test_script_trace_builds_container_and_script_filters(monkeypatch):
    calls = {}
    monkeypatch.setattr(
        server.sls_config,
        "resolve_target",
        lambda system, environment: SimpleNamespace(namespace="namespace", project="project", logstore="logstore"),
    )
    monkeypatch.setattr(server.sls_config, "credentials", lambda target: ("id", "secret"))
    monkeypatch.setattr(server.sls_config, "endpoint", lambda: "endpoint")

    def fake_query(*args):
        calls["args"] = args
        return ([{
            "__time__": "1770000000",
            "_container_name_": "srm-script-container",
            "content": "process start; request variables count=2; persistence updated count=1",
        }], "Complete")

    monkeypatch.setattr(server.sls, "query_sls", fake_query)
    result = json.loads(server.query_script_trace(
        "trace-1", from_time=1769990000, to_time=1770001000,
        script_code="SCRIPT_CODE",
    ))

    assert result["ok"] is True
    assert "_container_name_: srm-script-container" in result["query"]
    assert '"SCRIPT_CODE"' in result["query"]
    assert result["stage_counts"]["process_start"] == 1
    assert calls["args"][4] == '"trace-1" AND _namespace_: namespace AND _container_name_: srm-script-container AND "SCRIPT_CODE"'


def test_relation_returns_fields_and_join_sample(monkeypatch):
    class FakeClient:
        def list_columns(self, instance, db, table):
            return {"source_object": ["relationKey"], "target_object": ["relationKey", "valueField"]}[table]

        def describe_table(self, instance, db, table):
            return {"create_table": f"CREATE TABLE {table} (...)"}

        def query(self, sql, instance, db, limit):
            return {"rows": [{"source_value": "11", "target_value": "11"}]}

    monkeypatch.setattr(server.archery, "resolve_instance", lambda instance, site, default: "instance")
    monkeypatch.setattr(server.archery, "ArcheryClient", lambda site: FakeClient())
    result = json.loads(server.inspect_object_relation(
        "source_object", "relationKey", "target_object", "relationKey",
    ))

    assert result["ok"] is True
    assert result["source"]["field_exists"] is True
    assert result["target"]["field_exists"] is True
    assert result["relation"]["verified_by_sample"] is True


def test_relation_rejects_unsafe_identifier_before_backend_access():
    result = json.loads(server.inspect_object_relation("source;DROP", "id", "target", "id"))

    assert result["ok"] is False
    assert result["error"]["code"] == "object_relation"


def test_new_delivery_tools_are_exposed():
    tools = server.mcp._tool_manager._tools

    assert {
        "query_script_trace",
        "inspect_object_relation",
        "check_marmot_script_static",
    } <= set(tools)
    assert "marmot_get_delivery_config" not in tools
    assert "validate_price_service_contract" not in tools
    assert "validate_external_service_contract" not in tools
