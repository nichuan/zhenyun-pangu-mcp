"""知识库各类记录的 embedding 输入文本拼装。

这里保持迁移前的字段顺序和分隔规则不变，迁移阶段只替换模型，不同时改变输入文本。
"""

from __future__ import annotations

import json
from typing import Any


def compose_knowledge(payload: dict[str, Any]) -> str:
    parts = [
        payload.get("title") or "", payload.get("knowledge_type") or "", payload.get("system") or "",
        payload.get("module") or "", payload.get("summary") or "",
        " ".join(payload.get("tags") or []), " ".join(payload.get("core_tables") or []),
        payload.get("content_md") or "",
    ]
    return "\n".join(value for value in parts if value).strip()


def compose_template(payload: dict[str, Any]) -> str:
    parameters = payload.get("parameters")
    if isinstance(parameters, (dict, list)):
        parameters = json.dumps(parameters, ensure_ascii=False, sort_keys=True)
    elif parameters is not None:
        parameters = str(parameters)
    parts = [
        payload.get("title") or "", payload.get("category") or "", payload.get("system") or "",
        payload.get("scenario") or "", " ".join(payload.get("keywords") or []),
        " ".join(payload.get("core_tables") or []), payload.get("sql_text") or "",
        payload.get("problem_description") or "", payload.get("symptom") or "",
        payload.get("root_cause") or "", payload.get("preconditions") or "",
        payload.get("diagnosis_steps") or "", payload.get("execution_flow") or "",
        payload.get("example_case") or "", payload.get("verify_sql") or "",
        payload.get("rollback_sql") or "", parameters or "",
        payload.get("execution_policy") or "", payload.get("business_domain") or "",
    ]
    return "\n".join(value for value in parts if value).strip()


def compose_table(name: str, comment: str, description: str, tags: list[str]) -> str:
    return f"{name} {comment or ''} {description or ''} " + " ".join(tags or [])
