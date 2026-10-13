"""Generic design-gate checks for Marmot requirement delivery."""

import json
import os
import sys
from types import SimpleNamespace

import pytest

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


@pytest.mark.parametrize("module", ["@M/v1/object", "@M/v1/json-bigint", "@M/2021/std", "@M/2021/biz", "@M/2021/other"])
def test_static_check_allows_only_verified_literal_platform_requires(module):
    source = f"const helper = require( '{module}' );\nfunction process(input) {{ return input; }}"
    rules = {"allowed_runtime_modules": [module], "required_log_stages": []}

    result = delivery_quality.static_check_script(source, rules_json=json.dumps(rules))

    assert result["status"] == "pass"
    assert result["applied_rules"]["allowed_runtime_modules"] == [module]


def test_static_check_keeps_requires_strict_by_default():
    source = 'const helper = require("@M/v1/object");\nfunction process(input) { return input; }'

    result = delivery_quality.static_check_script(source)

    assert "node_global" in {finding["code"] for finding in result["findings"]}


@pytest.mark.parametrize("call", [
    'require("@M/v1/cache")',
    'require("@M/2021/std")',
    'require(moduleName)',
    'require("@M/v1/" + "object")',
    'require(`@M/v1/object`)',
    'require("fs")',
    'require("node:fs")',
    'require("../object")',
    'require("@M/v1/object/../cache")',
    'loader.require("@M/v1/object")',
])
def test_static_check_rejects_unverified_dynamic_and_node_requires(call):
    source = f"const helper = {call};\nfunction process(input) {{ return input; }}"
    rules = {"allowed_runtime_modules": ["@M/v1/object"], "required_log_stages": []}

    result = delivery_quality.static_check_script(source, rules_json=json.dumps(rules))

    assert result["status"] == "fail"
    assert any(finding["code"] == "node_global" for finding in result["findings"])


def test_static_check_does_not_hide_a_forbidden_require_after_an_allowed_call():
    source = '''const helper = require("@M/v1/object");
const fs = require("node:fs");
function process(input) { return input; }'''
    rules = {"allowed_runtime_modules": ["@M/v1/object"], "required_log_stages": []}

    result = delivery_quality.static_check_script(source, rules_json=json.dumps(rules))

    finding = next(item for item in result["findings"] if item["code"] == "node_global")
    assert finding["line"] == 2
    assert 'require("node:fs")' in finding["evidence"]


@pytest.mark.parametrize("value", [
    None, "@M/v1/object", [1], [""], ["fs"], ["node:fs"], ["../object"],
    ["@M/bridge"], ["@M/2021/object"], ["@M/v1/../object"],
    ["@M/v1/object/../../fs"], ["@M/v1/object/index.js"], [" @M/v1/object"],
])
def test_static_check_rejects_unsafe_or_malformed_runtime_module_allowlist(value):
    with pytest.raises(ValueError, match="allowed_runtime_modules"):
        delivery_quality.static_check_script(
            "function process(input) { return input; }",
            rules_json=json.dumps({"allowed_runtime_modules": value}),
        )


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
    assert result["complete"] is True
    assert calls["args"][4] == '"trace-1" AND _namespace_: namespace AND _container_name_: srm-script-container AND "SCRIPT_CODE"'


def test_script_trace_marks_bounded_result_incomplete(monkeypatch):
    monkeypatch.setattr(
        server.sls_config,
        "resolve_target",
        lambda system, environment: SimpleNamespace(namespace="namespace", project="project", logstore="logstore"),
    )
    monkeypatch.setattr(server.sls_config, "credentials", lambda target: ("id", "secret"))
    monkeypatch.setattr(server.sls_config, "endpoint", lambda: "endpoint")
    monkeypatch.setattr(
        server.sls, "query_sls",
        lambda *_args: ([{"content": "process start"}], "Complete"),
    )

    result = json.loads(server.query_script_trace(
        "trace-1", from_time=1769990000, to_time=1770001000, limit=1,
    ))

    assert result["complete"] is False
    assert result["possibly_truncated"] is True


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
    assert result["error"]["code"] == "bad_param"


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
