"""服务层：面向 Agent 的「认知层」语义化能力。

工具分层（对齐 GPT 建议）：
  Discovery  search_knowledge / search_sql_templates / search_tables / search_pangu
  Context    get_knowledge / get_sql_template / get_table / get_table_relations
  Composite  diagnose_context（组合诊断）
  Action     save_knowledge / save_sql_template（写权限谨慎暴露，需 skip_dup_check）

所有方法返回 Markdown 字符串，由 server.py 直接注册为 MCP 工具。
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from .. import supabase_client as sb
from . import repository as repo

logger = logging.getLogger(__name__)

# 知识类型 / 状态标签
_KTYPE_LABEL = {
    "business": "🏢 业务知识", "system": "⚙️ 系统机制", "technical": "🛠 技术知识",
    "troubleshooting": "🔧 排查经验", "data_model": "🗄 数据模型",
    "configuration": "📐 配置知识", "experience": "📝 经验沉淀", "rule": "📏 业务规则",
}
_KSTATUS_LABEL = {"draft": "📝 草稿", "verified": "✅ 已验证", "deprecated": "🗑 废弃", "archived": "📦 归档"}
_TSTATUS_LABEL = {"draft": "📝 草稿", "verified": "✅ 已验证", "trusted": "⭐ 可信", "deprecated": "🗑 废弃"}


def _split(value: str | None) -> list[str] | None:
    if value is None:
        return None
    items = [v.strip() for v in value.split(",") if v.strip()]
    return items or None


def _err(e: Exception) -> str:
    return (
        f"❌ 知识库操作失败：{e}\n\n"
        "（请检查 SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY 配置，或确认相关表已创建。）"
    )


# =========================================================================== #
# 格式化
# =========================================================================== #
def fmt_knowledge(r: dict[str, Any], similarity: float | None = None) -> str:
    ktype = r.get("knowledge_type") or "business"
    status = r.get("status") or "draft"
    sys_v = r.get("system") or "—"
    module_v = r.get("module") or "—"
    sim = f"- **语义相似度**：{similarity:.2f}\n" if similarity is not None else ""
    return (
        f"### [{r.get('id')}] {r.get('title')}\n"
        f"- **知识类型**：{_KTYPE_LABEL.get(ktype, ktype)} ｜ **系统/模块**：{sys_v} / {module_v} ｜ **状态**：{_KSTATUS_LABEL.get(status, status)}\n"
        f"- **摘要**：{r.get('summary') or '—'}\n"
        f"- **标签**：{'、'.join(r.get('tags') or []) or '—'}\n"
        f"- **关联核心表**：{'、'.join(r.get('core_tables') or []) or '—'}"
        f" ｜ **关联模板 id**：{'、'.join(str(x) for x in (r.get('related_template_ids') or [])) or '—'}\n"
        f"{sim}\n```markdown\n{r.get('content_md') or '（无正文）'}\n```\n"
    )


def fmt_template(r: dict[str, Any], similarity: float | None = None) -> str:
    status = r.get("status") or "draft"
    risk = r.get("risk_level") or "LOW"
    sim = f"- **语义相似度**：{similarity:.2f}\n" if similarity is not None else ""
    return (
        f"### [{r.get('id')}] {r.get('title')}（编号 {r.get('template_no') or '—'}）\n"
        f"- **分类**：{r.get('category')} ｜ **系统**：{r.get('system') or '—'} ｜ **状态**：{_TSTATUS_LABEL.get(status, status)} ｜ **风险**：{risk}\n"
        f"- **业务场景**：{r.get('scenario')}\n"
        f"- **关键词**：{'、'.join(r.get('keywords') or []) or '—'} ｜ **核心表**：{'、'.join(r.get('core_tables') or []) or '—'}\n"
        f"- **来源**：{r.get('source_type') or 'migrated'} ｜ **使用次数**：{r.get('usage_count') or 0}\n"
        f"{sim}\n```sql\n{r.get('sql_text')}\n```\n"
    )


def fmt_table(r: dict[str, Any]) -> str:
    key_cols = r.get("key_columns") or []
    entry_cols = r.get("entry_columns") or []
    key_lines = _fmt_cols(key_cols)
    entry_lines = _fmt_cols(entry_cols)
    return (
        f"### {r.get('table_name')}（{r.get('db_name') or 'srm'} / {r.get('domain')}）\n"
        f"- **表注释**：{r.get('table_comment') or '—'}\n"
        f"- **描述**：{r.get('description') or '—'}\n"
        f"- **标签**：{'、'.join(r.get('tags') or []) or '—'} ｜ **已验证**：{'是' if r.get('verified') else '否'}\n"
        f"- **关键字段**：\n{key_lines or '  —'}\n"
        f"- **入口字段**：\n{entry_lines or '  —'}\n"
    )


def _fmt_cols(cols: list[Any]) -> str:
    lines = []
    for c in cols if isinstance(cols, list) else []:
        if isinstance(c, dict):
            pk = " (PK)" if c.get("is_pk") else ""
            lines.append(f"  - `{c.get('name')}` {c.get('type') or ''}{pk} — {c.get('comment') or ''}")
        elif c:
            lines.append(f"  - `{c}`")
    return "\n".join(lines)


def fmt_relation(r: dict[str, Any]) -> str:
    conf = r.get("confidence")
    conf_txt = f"{float(conf):.1f}" if conf is not None else "—"
    verified = r.get("verified")
    verified_txt = "✅ 已验证" if verified else "⚠️ 未验证"
    src = r.get("source") or ""
    src_txt = f"，来源 {src}" if src else ""
    return (
        f"- `{r.get('from_table')}` → `{r.get('to_table')}`  [{r.get('relation_type')}] "
        f"置信度 {conf_txt}，{verified_txt}{src_txt}\n"
        f"  - join：`{r.get('join_on')}`\n"
        f"  - {r.get('description') or ''}\n"
    )


# =========================================================================== #
# Discovery
# =========================================================================== #
def search_knowledge(
    query: str = "", knowledge_type: str = "", system: str = "", module: str = "",
    status: str = "", verified_only: bool = False, limit: int = 10, use_semantic: bool = True,
) -> str:
    try:
        limit = max(1, min(int(limit), 50))
        status = status.strip() or None
        if verified_only:
            status = status or "verified"
        kw = query.strip() or None
        ktype = knowledge_type.strip() or None
        sys_v = system.strip() or None
        mod = module.strip() or None

        keyword_rows = repo.search_knowledge_keyword(kw, ktype, sys_v, mod, status, limit) if kw else []
        semantic_rows: list[dict[str, Any]] = []
        if use_semantic and kw:
            semantic_rows = repo.search_knowledge_semantic(kw, ktype, sys_v, mod, status, limit=limit)

        if not kw:
            rows = repo.list_knowledge(ktype, sys_v, mod, status, limit)
            return f"📚 知识库共 {len(rows)} 条：\n\n" + "\n".join(fmt_knowledge(r) for r in rows) if rows else "📭 知识库为空。"
        if semantic_rows:
            rows = repo.merge_by_id([r.get("id") for r in semantic_rows], semantic_rows, keyword_rows)[:limit]
            return (
                f"🔍 混合检索到 {len(rows)} 条候选知识（语义 {len(semantic_rows)} + 关键词 {len(keyword_rows)}，已去重）"
                f"（查询：{kw}）：\n\n" + "\n".join(fmt_knowledge(r, r.get("similarity")) for r in rows)
            )
        if not keyword_rows:
            return "🔍 未检索到匹配知识。可更换关键词/类型，或通过 save_knowledge 沉淀。"
        return f"🔍 检索到 {len(keyword_rows)} 条候选知识（关键词：{kw}）：\n\n" + "\n".join(fmt_knowledge(r) for r in keyword_rows)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def search_sql_templates(
    keyword: str = "", category: str = "", system: str = "", business_domain: str = "",
    verified_only: bool = False, limit: int = 10, use_semantic: bool = True,
) -> str:
    try:
        limit = max(1, min(int(limit), 50))
        kw = keyword.strip() or None
        cat = category.strip() or None
        sys_v = system.strip() or None
        dom = business_domain.strip() or None

        keyword_rows = repo.search_templates_keyword(kw, cat, sys_v, dom, verified_only, limit)
        semantic_rows: list[dict[str, Any]] = []
        if use_semantic and kw:
            semantic_rows = repo.search_templates_semantic(kw, cat, sys_v, verified_only, limit=limit)
            if dom:
                semantic_rows = [r for r in semantic_rows if (r.get("business_domain") or "") == dom]

        if not kw:
            rows = repo.list_templates(cat, sys_v, dom, verified_only, limit)
            return f"📚 模板库共 {len(rows)} 条：\n\n" + "\n".join(fmt_template(r) for r in rows) if rows else "📭 模板库为空。"
        if semantic_rows:
            rows = repo.merge_by_id([r.get("id") for r in semantic_rows], semantic_rows, keyword_rows)[:limit]
            return (
                f"🔍 混合检索到 {len(rows)} 条候选模板（语义 {len(semantic_rows)} + 关键词 {len(keyword_rows)}，已去重）"
                f"（查询：{kw}）：\n\n" + "\n".join(fmt_template(r, r.get("similarity")) for r in rows)
            )
        if not keyword_rows:
            return "🔍 未检索到匹配模板。可尝试更换关键词/分类。"
        return f"🔍 检索到 {len(keyword_rows)} 条候选模板（关键词：{kw}）：\n\n" + "\n".join(fmt_template(r) for r in keyword_rows)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def search_tables(
    query: str, domain: str = "", db_name: str = "", top_k: int = 5, use_semantic: bool = True,
) -> str:
    try:
        top_k = max(1, min(int(top_k), 20))
        dom = domain.strip() or None
        db = db_name.strip() or None
        kw = query.strip() or None
        if not kw:
            return "⚠️ 请输入查询关键词。"
        semantic_rows = repo.search_tables_semantic(kw, dom, db, top_k) if use_semantic else []
        keyword_rows = repo.search_tables_keyword(kw, dom, db, top_k)
        rows = repo.merge_by_id([r.get("table_name") for r in semantic_rows], semantic_rows, keyword_rows)[:top_k]
        if not rows:
            return f"🔍 未检索到匹配表（查询：{kw}）。"
        return f"🔍 检索到 {len(rows)} 张候选表（查询：{kw}）：\n\n" + "\n".join(fmt_table(r) for r in rows)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# =========================================================================== #
# Context
# =========================================================================== #
def get_knowledge(doc_id: int) -> str:
    try:
        row = repo.get_knowledge(int(doc_id))
        return fmt_knowledge(row) if row else f"⚠️ 未找到 id={doc_id} 的知识。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def get_sql_template(template_id: int) -> str:
    try:
        row = repo.get_template(int(template_id))
        return fmt_template(row) if row else f"⚠️ 未找到 id={template_id} 的模板。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def get_table(table_name: str, db_name: str = "") -> str:
    try:
        row = repo.get_table(table_name.strip().lower(), db_name.strip() or None)
        return fmt_table(row) if row else f"⚠️ 未找到表 {table_name}。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def get_table_relations(table_name: str) -> str:
    try:
        rows = repo.get_relations(table_name.strip().lower())
        if not rows:
            return f"📭 表 `{table_name}` 暂无已沉淀的关联关系。"
        header = f"🔗 `{table_name}` 的关联关系（{len(rows)} 条）：\n\n"
        return header + "\n".join(fmt_relation(r) for r in rows)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# =========================================================================== #
# Composite：统一搜索 + 组合诊断
# =========================================================================== #
def search_pangu(
    query: str, system: str = "", module: str = "", category: str = "", top_k: int = 3,
) -> str:
    """统一搜索：一次调用同时检索 知识 / 模板 / 表，并附上相关表关系。

    适合「快速发现」；需要精准结果时请分别用 search_knowledge / search_sql_templates / search_tables。
    """
    try:
        top_k = max(1, min(int(top_k), 5))
        sys_v = system.strip() or None
        mod = module.strip() or None
        cat = category.strip() or None

        kw_rows = repo.search_knowledge_keyword(query, None, sys_v, mod, None, top_k)
        tpl_rows = repo.search_templates_keyword(query, cat, sys_v, None, False, top_k)
        tbl_rows = repo.search_tables_keyword(query, None, None, top_k)

        parts: list[str] = [f"🔍 统一搜索「{query}」结果：\n"]
        parts.append(f"## 📚 知识（{len(kw_rows)}）")
        parts.append("\n".join(fmt_knowledge(r) for r in kw_rows) if kw_rows else "  无")
        parts.append(f"\n## 🛠 模板（{len(tpl_rows)}）")
        parts.append("\n".join(fmt_template(r) for r in tpl_rows) if tpl_rows else "  无")
        parts.append(f"\n## 🗄 表（{len(tbl_rows)}）")
        parts.append("\n".join(fmt_table(r) for r in tbl_rows) if tbl_rows else "  无")

        # 表关系（基于候选表）
        rel_parts: list[str] = []
        seen_rel: set[tuple] = set()
        for t in tbl_rows:
            for rel in repo.get_relations(t.get("table_name") or ""):
                key = (rel.get("from_table"), rel.get("to_table"))
                if key in seen_rel:
                    continue
                seen_rel.add(key)
                rel_parts.append(fmt_relation(rel))
        parts.append(f"\n## 🔗 相关关系（{len(rel_parts)}）")
        parts.append("\n".join(rel_parts) if rel_parts else "  无")
        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def diagnose_context(query: str, system: str = "", module: str = "", limit: int = 3) -> str:
    """组合诊断：针对一个业务问题，自动汇集 知识 → 模板 → 表 → 关系 的诊断上下文。

    流程：问题 → 知识检索(怎么想) → 模板检索(怎么处理) → 相关表(查什么) → 表关系(怎么关联)。
    """
    try:
        limit = max(1, min(int(limit), 5))
        sys_v = system.strip() or None
        mod = module.strip() or None

        kw = repo.search_knowledge_keyword(query, None, sys_v, mod, None, limit)
        tpl = repo.search_templates_keyword(query, None, sys_v, None, False, limit)
        tbl = repo.search_tables_keyword(query, None, None, limit)

        parts: list[str] = [f"🧭 诊断上下文「{query}」：\n"]
        parts.append("## ① 认知（知识库建议怎么想）")
        parts.append("\n".join(
            f"- **[{r.get('id')}] {r.get('title')}**（{r.get('knowledge_type')}）\n  {r.get('summary') or ''}" for r in kw
        ) if kw else "  无既有知识，可考虑沉淀。")
        parts.append("\n## ② 行动（可复用模板怎么处理）")
        parts.append("\n".join(
            f"- **[{r.get('id')}] {r.get('title')}**（{r.get('category')}，风险 {r.get('risk_level')}）\n  {r.get('scenario')}\n  ```sql\n  {r.get('sql_text')}\n  ```" for r in tpl
        ) if tpl else "  无既有模板。")
        parts.append("\n## ③ 数据（相关表查什么）")
        parts.append("\n".join(
            f"- `{r.get('table_name')}`（{r.get('db_name') or 'srm'}）：{r.get('table_comment') or ''}" for r in tbl
        ) if tbl else "  无相关表。")

        rel_parts: list[str] = []
        seen: set[tuple] = set()
        for t in tbl:
            for rel in repo.get_relations(t.get("table_name") or ""):
                key = (rel.get("from_table"), rel.get("to_table"))
                if key in seen:
                    continue
                seen.add(key)
                rel_parts.append(
                    f"- `{rel.get('from_table')}` → `{rel.get('to_table')}` [{rel.get('relation_type')}] {rel.get('description') or ''}"
                )
        parts.append("\n## ④ 关系（表之间怎么关联）")
        parts.append("\n".join(rel_parts) if rel_parts else "  无已沉淀关系。")
        return "\n".join(parts)
    except Exception as e:  # noqa: BLE001
        return _err(e)


# =========================================================================== #
# Action（写权限：谨慎暴露）
# =========================================================================== #
def save_knowledge(
    title: str, content_md: str, knowledge_type: str = "business", system: str = "",
    module: str = "", summary: str = "", core_tables: str = "", related_template_ids: str = "",
    tags: str = "", status: str = "draft", source_type: str = "manual", created_by: str = "",
    skip_dup_check: bool = False,
) -> str:
    try:
        if knowledge_type not in repo.VALID_KNOWLEDGE_TYPES:
            return f"⚠️ knowledge_type 非法：{knowledge_type}（可选 {', '.join(repo.VALID_KNOWLEDGE_TYPES)}）。"
        if status not in repo.VALID_KNOWLEDGE_STATUS:
            return f"⚠️ status 非法：{status}（可选 {', '.join(repo.VALID_KNOWLEDGE_STATUS)}）。"
        payload = {
            "title": title, "knowledge_type": knowledge_type,
            "system": system.strip() or None, "module": module.strip() or None,
            "content_md": content_md, "summary": summary.strip() or None,
            "core_tables": _split(core_tables) or [], "tags": _split(tags) or [],
            "related_template_ids": _split_ids(related_template_ids) or [],
            "status": status, "source_type": source_type, "created_by": created_by.strip() or None,
        }
        if status == "verified":
            payload["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if sb.embedding.available:
            emb = sb.embedding.embed_knowledge(payload)
            payload[sb.embedding.vector_column] = sb.embedding.to_literal(emb)
            payload.update(sb.embedding.metadata_payload())
        row = repo.insert_knowledge(payload)
        return f"✅ 知识已沉淀（id={row.get('id')}）。\n\n" + fmt_knowledge(row)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def update_knowledge(
    doc_id: int, title: str = "", content_md: str = "", knowledge_type: str = "",
    system: str = "", module: str = "", summary: str = "", core_tables: str = "",
    related_template_ids: str = "", tags: str = "", status: str = "", source_type: str = "",
) -> str:
    """部分更新已有知识条目（修正正文/标题/归类、补充核验状态等）。"""
    try:
        existing = repo.get_knowledge(int(doc_id))
        if not existing:
            return f"⚠️ 未找到 id={doc_id} 的知识，先用 get_knowledge 确认 doc_id。"
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if content_md:
            payload["content_md"] = content_md
        if knowledge_type:
            if knowledge_type not in repo.VALID_KNOWLEDGE_TYPES:
                return f"⚠️ knowledge_type 非法：{knowledge_type}（可选 {', '.join(repo.VALID_KNOWLEDGE_TYPES)}）。"
            payload["knowledge_type"] = knowledge_type
        if system:
            payload["system"] = system.strip()
        if module:
            payload["module"] = module.strip()
        if summary:
            payload["summary"] = summary.strip()
        if core_tables:
            payload["core_tables"] = _split(core_tables) or []
        if related_template_ids:
            parsed_ids = _split_ids(related_template_ids)
            if parsed_ids is None:
                return "⚠️ related_template_ids 格式非法（应为逗号分隔数字，如 1,2），未更新。"
            payload["related_template_ids"] = parsed_ids
        if tags:
            payload["tags"] = _split(tags) or []
        if status:
            if status not in repo.VALID_KNOWLEDGE_STATUS:
                return f"⚠️ status 非法：{status}（可选 {', '.join(repo.VALID_KNOWLEDGE_STATUS)}）。"
            payload["status"] = status
        if source_type:
            payload["source_type"] = source_type
        if not payload:
            return "⚠️ 未提供需要更新的字段。"
        if payload.get("status") == "verified":
            payload["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # 修改影响语义向量的字段时，基于「现有行 + 变更字段」合并后重新生成 embedding
        vector_fields = {"title", "content_md", "knowledge_type", "system", "module", "summary", "core_tables", "tags"}
        if sb.embedding.available and vector_fields & payload.keys():
            merged = {
                "title": payload.get("title") or existing.get("title") or "",
                "knowledge_type": payload.get("knowledge_type") or existing.get("knowledge_type") or "",
                "system": payload.get("system") or existing.get("system") or "",
                "module": payload.get("module") or existing.get("module") or "",
                "summary": payload.get("summary") or existing.get("summary") or "",
                "tags": payload.get("tags") if "tags" in payload else (existing.get("tags") or []),
                "core_tables": payload.get("core_tables") if "core_tables" in payload else (existing.get("core_tables") or []),
                "content_md": payload.get("content_md") or existing.get("content_md") or "",
            }
            emb = sb.embedding.embed_knowledge(merged)
            payload[sb.embedding.vector_column] = sb.embedding.to_literal(emb)
            payload.update(sb.embedding.metadata_payload())
        row = repo.update_knowledge(int(doc_id), payload)
        return f"✅ 知识 id={doc_id} 已更新。\n\n" + (fmt_knowledge(row) if row else "（无返回行）")
    except Exception as e:  # noqa: BLE001
        return _err(e)


def delete_knowledge(doc_id: int) -> str:
    """删除指定知识条目（清理错误/重复知识；谨慎，仅维护场景使用）。"""
    try:
        existing = repo.get_knowledge(int(doc_id))
        if not existing:
            return f"⚠️ 未找到 id={doc_id} 的知识。"
        ok = repo.delete_knowledge(int(doc_id))
        if not ok:
            return f"⚠️ 删除知识 id={doc_id} 未生效，请重试或检查权限。"
        return f"✅ 已删除知识 id={doc_id}（{existing.get('title', '')}）。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def save_sql_template(
    title: str, category: str, scenario: str, sql_text: str, keywords: str = "",
    core_tables: str = "", verified: bool = False, template_no: str = "", system: str = "",
    status: str = "draft", risk_level: str = "LOW", business_domain: str = "",
    source_type: str = "generated", parameters: str = "", execution_policy: str = "",
    created_by: str = "", skip_dup_check: bool = False,
) -> str:
    try:
        if status not in repo.VALID_TEMPLATE_STATUS:
            return f"⚠️ status 非法：{status}（可选 {', '.join(repo.VALID_TEMPLATE_STATUS)}）。"
        if risk_level not in repo.VALID_RISK_LEVELS:
            return f"⚠️ risk_level 非法：{risk_level}（可选 {', '.join(repo.VALID_RISK_LEVELS)}）。"
        payload = {
            "title": title, "category": category, "scenario": scenario, "sql_text": sql_text,
            "keywords": _split(keywords) or [], "core_tables": _split(core_tables) or [],
            "verified": verified, "template_no": template_no.strip() or None,
            "system": system.strip() or None, "status": status, "risk_level": risk_level,
            "business_domain": business_domain.strip() or None,
            "source_type": source_type, "execution_policy": execution_policy.strip() or None,
            "created_by": created_by.strip() or None,
        }
        if parameters.strip():
            parsed = _parse_json(parameters)
            if parsed is None:
                return "⚠️ parameters 不是合法 JSON，未保存。"
            payload["parameters"] = parsed
        if verified:
            payload["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if sb.embedding.available:
            emb = sb.embedding.embed_template(payload)
            payload[sb.embedding.vector_column] = sb.embedding.to_literal(emb)
            payload.update(sb.embedding.metadata_payload())
        row = repo.insert_template(payload)
        return f"✅ 模板已沉淀（id={row.get('id')}）。\n\n" + fmt_template(row)
    except Exception as e:  # noqa: BLE001
        return _err(e)


def list_sql_templates(
    category: str = "", system: str = "", business_domain: str = "",
    verified_only: bool = False, limit: int = 50,
) -> str:
    """列出模板库中的模板（可按分类/系统/业务域/验证状态过滤），用于总览与维护。"""
    try:
        rows = repo.list_templates(
            category or None, system or None, business_domain or None,
            verified_only, max(1, min(int(limit), 200)),
        )
        return f"📚 模板库共 {len(rows)} 条：\n\n" + "\n".join(fmt_template(r) for r in rows) if rows else "📭 模板库为空。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def update_sql_template(
    template_id: int, title: str = "", scenario: str = "", sql_text: str = "",
    category: str = "", system: str = "", status: str = "", risk_level: str = "",
    business_domain: str = "", keywords: str = "", core_tables: str = "",
    parameters: str = "", execution_policy: str = "", source_type: str = "",
    verified: bool = False,
) -> str:
    """更新已有模板字段（补充验证标记、修正 SQL、调整分类/风险等级等）。"""
    try:
        payload: dict[str, Any] = {}
        if title:
            payload["title"] = title
        if scenario:
            payload["scenario"] = scenario
        if sql_text:
            payload["sql_text"] = sql_text
        if category:
            payload["category"] = category
        if system:
            payload["system"] = system.strip()
        if status:
            if status not in repo.VALID_TEMPLATE_STATUS:
                return f"⚠️ status 非法：{status}（可选 {', '.join(repo.VALID_TEMPLATE_STATUS)}）。"
            payload["status"] = status
        if risk_level:
            if risk_level not in repo.VALID_RISK_LEVELS:
                return f"⚠️ risk_level 非法：{risk_level}（可选 {', '.join(repo.VALID_RISK_LEVELS)}）。"
            payload["risk_level"] = risk_level
        if business_domain:
            payload["business_domain"] = business_domain.strip()
        if keywords:
            payload["keywords"] = _split(keywords)
        if core_tables:
            payload["core_tables"] = _split(core_tables)
        if execution_policy:
            payload["execution_policy"] = execution_policy.strip()
        if source_type:
            payload["source_type"] = source_type
        if parameters.strip():
            parsed = _parse_json(parameters)
            if parsed is None:
                return "⚠️ parameters 不是合法 JSON，未更新。"
            payload["parameters"] = parsed
        if verified:
            payload["status"] = "verified"
            payload["verified_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if not payload:
            return "⚠️ 未提供需要更新的字段。"
        # 修改影响语义向量的字段时，只更新当前 provider 对应的向量列。
        template_vector_fields = {
            "title", "scenario", "sql_text", "category", "system",
            "business_domain", "keywords", "core_tables",
        }
        if sb.embedding.available and template_vector_fields & payload.keys():
            existing = repo.get_template(int(template_id))
            if existing is None:
                return f"⚠️ 未找到 id={template_id} 的模板，未更新。"
            merged = {
                field: payload.get(field) if field in payload else (existing.get(field) or "")
                for field in (
                    "title", "category", "system", "scenario", "keywords",
                    "core_tables", "sql_text", "problem_description", "symptom",
                    "root_cause", "business_domain",
                )
            }
            emb = sb.embedding.embed_template(merged)
            payload[sb.embedding.vector_column] = sb.embedding.to_literal(emb)
            payload.update(sb.embedding.metadata_payload())
        row = repo.update_template(int(template_id), payload)
        return f"✅ 模板 id={template_id} 已更新。\n\n" + (fmt_template(row) if row else "（无返回行）")
    except Exception as e:  # noqa: BLE001
        return _err(e)


def delete_sql_template(template_id: int) -> str:
    """删除指定模板（清理错误/过期模板；谨慎，仅维护场景使用）。"""
    try:
        ok = repo.delete_template(int(template_id))
        return f"✅ 已删除模板 id={template_id}。" if ok else f"⚠️ 未找到 id={template_id} 的模板。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def record_template_usage(template_id: int) -> str:
    """模板被复用后累加使用次数（自进化权重）。"""
    try:
        repo.increment_template_usage(int(template_id))
        return f"✅ 已记录模板 id={template_id} 的使用。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def add_table_relation(
    from_table: str, to_table: str, join_on: str, relation_type: str = "ref",
    description: str = "", confidence: float = 1.0, from_db: str = "srm",
    to_db: str = "srm", verified: bool = False, source: str = "manual",
) -> str:
    """沉淀一条表关联关系（写操作，upsert 去重）。

    ``verified=True`` 表示已经 Archery/SELECT 实测验证过两端字段与 join 结果；
    ``source`` 表示来源（archery_select / ddl / manual / inferred）。
    未实测的关系请保持 verified=False，让 SQL Agent 谨慎使用。
    """
    try:
        from_table = (from_table or "").strip().lower()
        to_table = (to_table or "").strip().lower()
        join_on = (join_on or "").strip()
        if not (from_table and to_table and join_on):
            return "⚠️ from_table / to_table / join_on 均不能为空。"
        src = (source or "manual").strip().lower()
        if src not in repo.VALID_RELATION_SOURCES:
            src = "manual"
        row = repo.add_table_relation(
            from_table, to_table, join_on, relation_type.upper(), description,
            confidence, from_db, to_db, verified=bool(verified), source=src,
        )
        vmark = "（已验证）" if verified else "（未验证）"
        return (
            f"✅ 已沉淀关联 `{from_table}` → `{to_table}` [{relation_type.upper()}] "
            f"置信度 {float(confidence):.1f}{vmark} 来源 {src}。"
        )
    except Exception as e:  # noqa: BLE001
        return _err(e)


def record_table_usage(table_names: str) -> str:
    """记录表被使用（自进化权重），table_names 逗号分隔。"""
    try:
        for name in [n.strip() for n in table_names.split(",") if n.strip()]:
            repo.increment_table_usage(name)
        return f"✅ 已记录表使用：{table_names}。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


def upsert_table_knowledge(
    table_name: str, description: str = "", tags: str = "", db_name: str = "",
) -> str:
    """修正/补录单表元数据描述与标签（写操作，upsert）。"""
    try:
        patch: dict[str, Any] = {}
        if description:
            patch["description"] = description
        if tags:
            patch["tags"] = _split(tags)
        if db_name:
            patch["db_name"] = db_name
        if not patch:
            return "⚠️ 请提供 description / tags / db_name 至少一项。"
        normalized_name = table_name.strip().lower()
        target_db = patch.get("db_name") or "srm"
        if sb.embedding.available:
            existing = repo.get_table(normalized_name, target_db) or {}
            vector = sb.embedding.embed_table(
                normalized_name,
                existing.get("table_comment") or "",
                patch.get("description") or existing.get("description") or "",
                patch.get("tags") if "tags" in patch else (existing.get("tags") or []),
            )
            patch[sb.embedding.vector_column] = sb.embedding.to_literal(vector)
            patch.update(sb.embedding.metadata_payload())
        row = repo.upsert_table_knowledge(normalized_name, patch)
        return f"✅ 已更新表 `{table_name}` 元数据。" if row else f"⚠️ 表 `{table_name}` 更新未返回行。"
    except Exception as e:  # noqa: BLE001
        return _err(e)


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def _split_ids(value: str | None) -> list[int] | None:
    if value is None:
        return None
    ids = []
    for v in value.split(","):
        v = v.strip()
        if v:
            try:
                ids.append(int(v))
            except ValueError:
                continue
    return ids or None


def _parse_json(value: str) -> dict[str, Any] | None:
    v = value.strip()
    if not v:
        return None
    try:
        parsed = json.loads(v)
    except (json.JSONDecodeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None
