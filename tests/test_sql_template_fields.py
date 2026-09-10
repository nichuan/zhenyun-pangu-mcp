from __future__ import annotations

import inspect
import json
from types import SimpleNamespace

from zhenyun_pangu_mcp import server
from zhenyun_pangu_mcp.embedding.text import compose_template
from zhenyun_pangu_mcp.knowledge_base import service


DETAIL_FIELDS = {
    "execution_flow",
    "example_case",
    "problem_description",
    "symptom",
    "root_cause",
    "preconditions",
    "diagnosis_steps",
    "verify_sql",
    "rollback_sql",
}


def _template_row(**overrides):
    row = {
        "id": 101,
        "title": "测试模板",
        "template_no": "TMP-101",
        "category": "数据修复",
        "system": "盘古",
        "business_domain": "采购寻源",
        "scenario": "测试场景",
        "keywords": ["测试"],
        "core_tables": ["ssrc_rfx_header"],
        "sql_text": "SELECT 1;",
        "status": "verified",
        "risk_level": "MEDIUM",
        "source_type": "generated",
        "usage_count": 0,
    }
    row.update(overrides)
    return row


def test_mcp_tool_signatures_expose_all_template_detail_fields():
    save_fields = set(inspect.signature(server.save_sql_template).parameters)
    update_fields = set(inspect.signature(server.update_sql_template).parameters)

    assert DETAIL_FIELDS <= save_fields
    assert DETAIL_FIELDS <= update_fields
    assert "template_no" in update_fields


def test_save_sql_template_persists_all_detail_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(service.sb, "embedding", SimpleNamespace(available=False))

    def insert_template(payload):
        captured.update(payload)
        return _template_row(**payload)

    monkeypatch.setattr(service.repo, "insert_template", insert_template)
    result = service.save_sql_template(
        "测试模板",
        "数据修复",
        "测试场景",
        "SELECT 1;",
        parameters=json.dumps({"tenant_id": {"type": "bigint"}}),
        execution_policy="REQUIRES_CONFIRMATION",
        execution_flow="[STEP 1] QUERY: SELECT 1",
        example_case="输入 tenant_id=1，校验通过",
        problem_description="问题描述",
        symptom="问题现象",
        root_cause="根因",
        preconditions="前置条件",
        diagnosis_steps="诊断步骤",
        verify_sql="SELECT 1;",
        rollback_sql="SELECT 2;",
    )

    assert DETAIL_FIELDS <= captured.keys()
    assert captured["execution_flow"].startswith("[STEP 1]")
    assert captured["example_case"].startswith("输入")
    assert captured["parameters"] == {"tenant_id": {"type": "bigint"}}
    assert "#### 执行流程" in result
    assert "#### 脱敏案例" in result


def test_update_sql_template_persists_all_detail_fields(monkeypatch):
    captured = {}
    monkeypatch.setattr(service.sb, "embedding", SimpleNamespace(available=False))

    def update_template(template_id, payload):
        assert template_id == 101
        captured.update(payload)
        return _template_row(**payload)

    monkeypatch.setattr(service.repo, "update_template", update_template)
    result = service.update_sql_template(
        101,
        template_no="TMP-UPDATED",
        execution_flow="[STEP 1] QUERY: SELECT 1",
        example_case="脱敏案例",
        problem_description="问题描述",
        symptom="问题现象",
        root_cause="根因",
        preconditions="前置条件",
        diagnosis_steps="诊断步骤",
        verify_sql="SELECT 1;",
        rollback_sql="SELECT 2;",
    )

    assert DETAIL_FIELDS <= captured.keys()
    assert captured["template_no"] == "TMP-UPDATED"
    assert "#### 校验 SQL" in result
    assert "#### 回滚 SQL" in result


def test_compose_template_indexes_detail_fields_and_parameters():
    payload = _template_row(
        execution_flow="FLOW_TOKEN",
        example_case="CASE_TOKEN",
        problem_description="PROBLEM_TOKEN",
        symptom="SYMPTOM_TOKEN",
        root_cause="CAUSE_TOKEN",
        preconditions="PRECONDITION_TOKEN",
        diagnosis_steps="DIAGNOSIS_TOKEN",
        verify_sql="VERIFY_TOKEN",
        rollback_sql="ROLLBACK_TOKEN",
        execution_policy="POLICY_TOKEN",
        parameters={"tenant_id": {"type": "bigint"}},
    )

    text = compose_template(payload)

    for token in (
        "FLOW_TOKEN",
        "CASE_TOKEN",
        "PROBLEM_TOKEN",
        "SYMPTOM_TOKEN",
        "CAUSE_TOKEN",
        "PRECONDITION_TOKEN",
        "DIAGNOSIS_TOKEN",
        "VERIFY_TOKEN",
        "ROLLBACK_TOKEN",
        "POLICY_TOKEN",
        "tenant_id",
    ):
        assert token in text
