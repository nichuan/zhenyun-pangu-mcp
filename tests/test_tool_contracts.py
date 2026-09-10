"""MCP tool schemas must match the validation and routing contracts used by skills."""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import server  # noqa: E402


def _required_alternatives(tool_name: str) -> set[str]:
    schema = server.mcp._tool_manager._tools[tool_name].parameters
    alternatives = schema["allOf"][0]["anyOf"]
    return {item["required"][0] for item in alternatives}


def test_archery_list_instances_filters_by_site():
    all_sites = json.loads(server.archery_list_instances())
    aws_only = json.loads(server.archery_list_instances("aws"))

    assert set(all_sites["instances_by_site"]) == {"cn", "aws"}
    assert set(aws_only["instances_by_site"]) == {"aws"}
    assert aws_only["requested_site"] == "aws"


def test_archery_list_instances_rejects_unknown_site():
    result = json.loads(server.archery_list_instances("unknown"))

    assert result["ok"] is False
    assert result["error"]["code"] == "archery_instance_site"


def test_conditional_required_fields_are_advertised_in_tool_schemas():
    assert _required_alternatives("search_adapter_scripts") == {
        "tenant", "running_service", "query",
    }
    assert _required_alternatives("search_standalone_scripts") == {"tenant", "query"}
    assert _required_alternatives("upsert_table_knowledge") == {
        "description", "tags", "db_name",
    }


def test_conditional_required_fields_fail_before_backend_access():
    with pytest.raises(ValueError, match="tenant、running_service、query"):
        server.search_adapter_scripts()
    with pytest.raises(ValueError, match="tenant、query"):
        server.search_standalone_scripts()
    with pytest.raises(ValueError, match="description、tags、db_name"):
        server.upsert_table_knowledge("some_table")


def test_expected_es_tools_are_exposed():
    tools = server.mcp._tool_manager._tools

    assert {"es_search", "es_count", "es_get"} <= set(tools)
