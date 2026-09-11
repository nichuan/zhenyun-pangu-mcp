"""Configurable Marmot delivery contracts and lightweight static checks.

This module deliberately contains no tenant, table, field, status, or service
rules. Requirement-specific facts are supplied by the caller as JSON.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str
    line: int | None = None
    evidence: str = ""


LOG_STAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "entry": (r"process\s*start", r"script\s*entry", r"脚本入口", r"入口日志"),
    "data_access": (r"data\s*(?:query|access)", r"查询对象", r"数据查询", r"返回数量"),
    "association": (r"association", r"relation", r"关联补全", r"未匹配", r"unmatched"),
    "request_build": (r"request\s*(?:built|variables|parameters)", r"请求变量", r"参数数量", r"variables_count"),
    "external_response": (r"service\s*response", r"external\s*response", r"外部服务响应", r"顶层失败"),
    "mapping": (r"record\s*mapping", r"line\s*mapping", r"字段映射", r"行映射", r"最终字段"),
    "persistence": (r"persist", r"persistence", r"持久化", r"更新行数"),
    "branch": (r"branch", r"分支结果", r"跳过", r"读取已有结果"),
}


def _parse_json_object(value: str, name: str) -> dict[str, Any]:
    if not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{name} 必须是 JSON 对象: {exc.msg}") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{name} 必须是 JSON 对象")
    return parsed


def _string_list(value: Any, name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError(f"{name} 必须是字符串数组")
    return [item for item in value if item]


def _line_number(source: str, position: int) -> int:
    return source.count("\n", 0, position) + 1


def _evidence(source: str, position: int) -> tuple[int, str]:
    line = _line_number(source, position)
    lines = source.splitlines()
    return line, (lines[line - 1].strip()[:240] if lines else "")


def _add_match(
    findings: list[Finding], source: str, code: str, severity: str,
    message: str, pattern: str, *, flags: int = re.IGNORECASE,
) -> re.Match[str] | None:
    match = re.search(pattern, source, flags)
    if match:
        line, evidence = _evidence(source, match.start())
        findings.append(Finding(code, severity, message, line, evidence))
    return match


def _literal_pattern(value: Any) -> str:
    if isinstance(value, str):
        return rf"['\"]{re.escape(value)}['\"]"
    if value is True:
        return r"true"
    if value is False:
        return r"false"
    if value is None:
        return r"null"
    return re.escape(str(value))


def static_check_script(
    source: str,
    *,
    script_code: str = "",
    rules_json: str = "",
    expected_sha256: str = "",
    artifact_sha256: str = "",
) -> dict[str, Any]:
    """Run platform-generic checks plus caller-supplied requirement rules.

    Supported ``rules_json`` keys:
      - numeric_id_fields: fields known from schema to be numeric identifiers;
      - forbidden_direct_fields: exact ``object.field`` paths disallowed by the design;
      - forbidden_service_parameters: property names forbidden by a service contract;
      - required_object_parameters: parameter -> required nested fields;
      - required_constants: parameter -> required literal value;
      - required_log_stages: names from ``LOG_STAGE_PATTERNS``;
      - full_scope_required: whether paging must show an all-page traversal marker.
    """
    text = source or ""
    rules = _parse_json_object(rules_json, "rules_json")
    numeric_id_fields = set(_string_list(rules.get("numeric_id_fields"), "numeric_id_fields"))
    forbidden_direct_fields = _string_list(rules.get("forbidden_direct_fields"), "forbidden_direct_fields")
    forbidden_service_parameters = _string_list(
        rules.get("forbidden_service_parameters"), "forbidden_service_parameters",
    )
    required_log_stages = _string_list(rules.get("required_log_stages", ["entry"]), "required_log_stages")
    required_object_parameters = rules.get("required_object_parameters") or {}
    required_constants = rules.get("required_constants") or {}
    if not isinstance(required_object_parameters, dict):
        raise ValueError("required_object_parameters 必须是对象")
    if not isinstance(required_constants, dict):
        raise ValueError("required_constants 必须是对象")

    findings: list[Finding] = []
    process_matches = list(re.finditer(r"function\s+process\s*\(", text))
    if not process_matches:
        findings.append(Finding("missing_process_entry", "error", "未找到同步全局 function process(input) 入口"))
    elif len(process_matches) > 1:
        findings.append(Finding("multiple_process_entries", "error", "发现多个 process 入口，平台脚本应只有一个"))

    for code, pattern, message in (
        ("commonjs_export", r"\b(module\.exports|exports\.)", "禁止使用 CommonJS 导出"),
        ("esm_syntax", r"(^|\n)\s*(import\s+|export\s+)", "禁止使用 ESM import/export"),
        ("node_global", r"\brequire\s*\(", "禁止依赖 Node.js require 运行时"),
        ("browser_global", r"\b(window|document)\.", "禁止依赖浏览器全局对象"),
    ):
        _add_match(findings, text, code, "error", message, pattern, flags=re.IGNORECASE | re.MULTILINE)

    for match in re.finditer(
        r"(?:_\.isEmpty|isEmpty)\s*\(\s*[A-Za-z_$][\w$]*\s*\.\s*([A-Za-z_$][\w$]*)\s*\)",
        text,
    ):
        field = match.group(1)
        if field not in numeric_id_fields and not field.lower().endswith("id"):
            continue
        severity = "error" if field in numeric_id_fields else "warning"
        code = "numeric_id_is_empty" if severity == "error" else "possible_id_type_mismatch"
        message = (
            f"字段 {field} 已声明为数字型 ID，禁止使用 _.isEmpty 判断空值"
            if severity == "error"
            else f"字段 {field} 使用 _.isEmpty；请先从字段结构确认类型，数字型值应使用 null/undefined/空字符串判断"
        )
        line, evidence = _evidence(text, match.start())
        findings.append(Finding(code, severity, message, line, evidence))

    for path in forbidden_direct_fields:
        match = re.search(rf"(?<![\w$]){re.escape(path)}(?![\w$])", text)
        if match:
            line, evidence = _evidence(text, match.start())
            findings.append(Finding(
                "forbidden_direct_field", "error",
                f"设计门禁禁止直接读取 {path}；请按已确认的关联路径取值",
                line, evidence,
            ))

    for parameter in forbidden_service_parameters:
        _add_match(
            findings, text, "forbidden_service_parameter", "error",
            f"外部服务契约禁止传入参数：{parameter}",
            rf"(?:['\"]{re.escape(parameter)}['\"]|\b{re.escape(parameter)}\b)\s*:",
        )

    for parameter, nested_fields in required_object_parameters.items():
        fields = _string_list(nested_fields, f"required_object_parameters.{parameter}")
        object_match = re.search(
            rf"(?:['\"]{re.escape(parameter)}['\"]|\b{re.escape(parameter)}\b)\s*:\s*\{{([^}}]*)\}}",
            text,
            re.IGNORECASE | re.DOTALL,
        )
        if not object_match:
            findings.append(Finding(
                "object_parameter_not_verifiable", "warning",
                f"未识别到参数 {parameter} 的对象字面量，无法静态确认嵌套字段 {fields}",
            ))
            continue
        body = object_match.group(1)
        for field in fields:
            if not re.search(rf"(?:['\"]{re.escape(field)}['\"]|\b{re.escape(field)}\b)\s*:", body):
                line, evidence = _evidence(text, object_match.start())
                findings.append(Finding(
                    "missing_nested_parameter", "error",
                    f"参数 {parameter} 缺少契约字段：{field}", line, evidence,
                ))

    for parameter, expected in required_constants.items():
        pattern = (
            rf"(?:['\"]{re.escape(parameter)}['\"]|\b{re.escape(parameter)}\b)\s*:\s*"
            rf"{_literal_pattern(expected)}"
        )
        if not re.search(pattern, text, re.IGNORECASE):
            findings.append(Finding(
                "missing_required_constant", "error",
                f"未识别到契约常量 {parameter}={expected!r}",
            ))

    for stage in required_log_stages:
        patterns = LOG_STAGE_PATTERNS.get(stage)
        if not patterns:
            findings.append(Finding("unknown_log_stage", "error", f"未知日志阶段：{stage}"))
            continue
        if not any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
            findings.append(Finding(f"missing_log_{stage}", "warning", f"未识别到日志阶段：{stage}"))

    paging_used = bool(re.search(r"\b(?:page|pageNum|pageSize|currentPage)\b", text, re.IGNORECASE))
    full_scope_marker = bool(re.search(
        r"(?:totalPages|hasNext|nextPage|fetchAll|allPages|全量|全部记录|分页.*汇总)", text, re.IGNORECASE,
    ))
    if paging_used and not full_scope_marker:
        severity = "error" if rules.get("full_scope_required") is True else "warning"
        findings.append(Finding(
            "possible_paging_partial", severity,
            "脚本出现分页变量但未识别到全量遍历/汇总逻辑；请按本产物的数据范围契约确认",
        ))

    actual_sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    hash_result: dict[str, Any] = {
        "actual_sha256": actual_sha256,
        "expected_sha256": expected_sha256 or None,
        "artifact_sha256": artifact_sha256 or None,
        "matched": None,
    }
    if expected_sha256:
        hash_result["matched"] = actual_sha256 == expected_sha256
        if not hash_result["matched"]:
            findings.append(Finding("source_hash_mismatch", "error", "源码 SHA-256 与期望值不一致"))
    if artifact_sha256:
        hash_result["artifact_matched"] = actual_sha256 == artifact_sha256
        if not hash_result["artifact_matched"]:
            findings.append(Finding("artifact_hash_mismatch", "error", "源码 SHA-256 与 artifacts.json 记录不一致"))

    severity_counts = {level: sum(1 for item in findings if item.severity == level) for level in ("error", "warning")}
    return {
        "script_code": script_code or None,
        "status": "fail" if severity_counts["error"] else ("warn" if severity_counts["warning"] else "pass"),
        "severity_counts": severity_counts,
        "applied_rules": rules,
        "hash": hash_result,
        "findings": [asdict(item) for item in findings],
        "available_log_stages": list(LOG_STAGE_PATTERNS),
        "runtime_note": "仅静态检查；未执行 Marmot、数据库、事务、权限或远程服务运行时。",
    }

