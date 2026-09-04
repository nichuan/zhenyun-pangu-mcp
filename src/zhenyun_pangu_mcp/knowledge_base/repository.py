"""仓储层：knowledge_docs / sql_templates / table_catalog / table_relations 四张表的读写与检索。

复用并整合原 sql-template-mcp / knowledge-ops-mcp / table-catalog-mcp 的已验证逻辑，
统一走 supabase_client 的 REST/RPC 封装。返回 Python 原生结构，由 service 层负责格式化。
"""
from __future__ import annotations

import logging
from typing import Any

from .. import supabase_client as sb

logger = logging.getLogger(__name__)

# 合法枚举（与服务层校验保持一致）
VALID_KNOWLEDGE_TYPES = (
    "business", "system", "technical", "troubleshooting",
    "data_model", "configuration", "experience", "rule",
)
VALID_KNOWLEDGE_STATUS = ("draft", "verified", "deprecated", "archived")
VALID_TEMPLATE_STATUS = ("draft", "verified", "trusted", "deprecated")
VALID_RISK_LEVELS = ("LOW", "MEDIUM", "HIGH", "CRITICAL")


# =========================================================================== #
# knowledge_docs
# =========================================================================== #
def insert_knowledge(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{sb.base_url()}/rest/v1/{sb.config.KNOWLEDGE_TABLE}"
    resp = sb._rest_request("POST", url, body=payload)
    if not resp:
        raise RuntimeError("插入知识失败：未返回数据（请检查 RLS / Service Role Key）。")
    return resp[0]


def get_knowledge(doc_id: int) -> dict[str, Any] | None:
    rows = sb.query_table(sb.config.KNOWLEDGE_TABLE, select="*", eq={"id": doc_id}, limit=1)
    return rows[0] if rows else None


def update_knowledge(doc_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    url = f"{sb.base_url()}/rest/v1/{sb.config.KNOWLEDGE_TABLE}?id=eq.{doc_id}"
    resp = sb._rest_request("PATCH", url, body=payload)
    return resp[0] if resp else None


def delete_knowledge(doc_id: int) -> bool:
    url = f"{sb.base_url()}/rest/v1/{sb.config.KNOWLEDGE_TABLE}?id=eq.{doc_id}"
    resp = sb._rest_request("DELETE", url)
    return bool(resp)


def search_knowledge_keyword(
    keyword: str, knowledge_type: str | None = None, system: str | None = None,
    module: str | None = None, status: str | None = None, limit: int = 10,
) -> list[dict[str, Any]]:
    try:
        return sb.rpc("search_knowledge_docs_keyword", {
            "keyword": keyword, "match_count": limit,
            "p_knowledge_type": knowledge_type, "p_system": system,
            "p_module": module, "p_status": status,
        })
    except Exception as e:  # noqa: BLE001 - 失败降级为空
        logger.warning("关键词检索知识失败：%s", e)
        return []


def search_knowledge_semantic(
    query: str, knowledge_type: str | None = None, system: str | None = None,
    module: str | None = None, status: str | None = None,
    threshold: float | None = None, limit: int = 10,
) -> list[dict[str, Any]]:
    if not sb.embedding.available:
        return []
    q_emb = sb.embedding.embed_query(query)
    try:
        return sb.rpc(sb.embedding.rpc_name("knowledge"), {
            "query_embedding": sb.embedding.to_literal(q_emb),
            "match_threshold": threshold if threshold is not None else sb.config.get_semantic_match_threshold(),
            "match_count": limit, "p_knowledge_type": knowledge_type,
            "p_system": system, "p_module": module, "p_status": status,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("语义检索知识失败：%s", e)
        return []


def list_knowledge(
    knowledge_type: str | None = None, system: str | None = None,
    module: str | None = None, status: str | None = None, limit: int = 50,
) -> list[dict[str, Any]]:
    eq: dict[str, Any] = {}
    if knowledge_type:
        eq["knowledge_type"] = knowledge_type
    if system:
        eq["system"] = system
    if module:
        eq["module"] = module
    if status:
        eq["status"] = status
    return sb.query_table(sb.config.KNOWLEDGE_TABLE, select="*", eq=eq, order="updated_at.desc", limit=limit)


# =========================================================================== #
# sql_templates
# =========================================================================== #
def insert_template(payload: dict[str, Any]) -> dict[str, Any]:
    url = f"{sb.base_url()}/rest/v1/{sb.config.SQL_TEMPLATE_TABLE}"
    resp = sb._rest_request("POST", url, body=payload)
    if not resp:
        raise RuntimeError("插入模板失败：未返回数据。")
    return resp[0]


def get_template(template_id: int) -> dict[str, Any] | None:
    rows = sb.query_table(sb.config.SQL_TEMPLATE_TABLE, select="*", eq={"id": template_id}, limit=1)
    return rows[0] if rows else None


def update_template(template_id: int, payload: dict[str, Any]) -> dict[str, Any] | None:
    url = f"{sb.base_url()}/rest/v1/{sb.config.SQL_TEMPLATE_TABLE}?id=eq.{template_id}"
    resp = sb._rest_request("PATCH", url, body=payload)
    return resp[0] if resp else None


def delete_template(template_id: int) -> bool:
    url = f"{sb.base_url()}/rest/v1/{sb.config.SQL_TEMPLATE_TABLE}?id=eq.{template_id}"
    resp = sb._rest_request("DELETE", url)
    return bool(resp)


def search_templates_keyword(
    keyword: str | None = None, category: str | None = None, system: str | None = None,
    business_domain: str | None = None, verified_only: bool = False, limit: int = 10,
) -> list[dict[str, Any]]:
    if keyword and keyword.strip():
        kw = keyword.strip()
        try:
            return sb.rpc("search_sql_templates_keyword", {
                "keyword": kw,
                "match_count": limit,
                "p_category": category,
                "p_system": system,
                "p_business_domain": business_domain,
                "p_verified_only": verified_only,
            })
        except Exception as e:  # noqa: BLE001 - 兼容尚未执行新版 schema 的旧库
            logger.warning("模板关键词 RPC 不可用，降级到 REST 过滤：%s", e)
            # 旧库没有 RPC 时至少扩大候选窗口；此前只取 limit 行再过滤会漏掉
            # 排在前面的不匹配记录之后的所有命中项。
            eq: dict[str, Any] = {}
            if category:
                eq["category"] = category
            if system:
                eq["system"] = system
            if business_domain:
                eq["business_domain"] = business_domain
            if verified_only:
                eq["verified"] = True
            try:
                fetch_limit = min(max(limit * 20, 100), 500)
                rows = sb.query_table(
                    sb.config.SQL_TEMPLATE_TABLE, select="*", eq=eq,
                    order="updated_at.desc", limit=fetch_limit,
                )
                needle = kw.lower()
                filtered = [
                    r for r in rows
                    if needle in (r.get("scenario") or "").lower()
                    or needle in (r.get("title") or "").lower()
                    or needle in " ".join(r.get("keywords") or []).lower()
                    or needle in " ".join(r.get("core_tables") or []).lower()
                ]
                return filtered[:limit]
            except Exception as fallback_error:  # noqa: BLE001
                logger.warning("模板关键词 REST 降级也失败：%s", fallback_error)
                return []
    eq: dict[str, Any] = {}
    if category:
        eq["category"] = category
    if system:
        eq["system"] = system
    if business_domain:
        eq["business_domain"] = business_domain
    if verified_only:
        eq["verified"] = True
    return sb.query_table(sb.config.SQL_TEMPLATE_TABLE, select="*", eq=eq, limit=limit)


def search_templates_semantic(
    query: str, category: str | None = None, system: str | None = None,
    verified_only: bool = False, threshold: float | None = None, limit: int = 10,
) -> list[dict[str, Any]]:
    if not sb.embedding.available:
        return []
    q_emb = sb.embedding.embed_query(query)
    try:
        return sb.rpc(sb.embedding.rpc_name("template"), {
            "query_embedding": sb.embedding.to_literal(q_emb),
            "match_threshold": threshold if threshold is not None else sb.config.get_semantic_match_threshold(),
            "match_count": limit, "p_category": category, "p_system": system,
            "p_verified_only": verified_only,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("语义检索模板失败：%s", e)
        return []


def list_templates(
    category: str | None = None, system: str | None = None, business_domain: str | None = None,
    verified_only: bool = False, limit: int = 50,
) -> list[dict[str, Any]]:
    eq: dict[str, Any] = {}
    if category:
        eq["category"] = category
    if system:
        eq["system"] = system
    if business_domain:
        eq["business_domain"] = business_domain
    if verified_only:
        eq["verified"] = True
    return sb.query_table(sb.config.SQL_TEMPLATE_TABLE, select="*", eq=eq, order="updated_at.desc", limit=limit)


def increment_template_usage(template_id: int) -> None:
    """模板被复用后累加 usage_count。"""
    # PostgREST 的 query 参数不是表达式求值；PATCH usage_count=1 会把计数
    # 重置为 1。通过数据库 RPC 在服务端执行 usage_count = usage_count + 1，
    # 同时避免并发调用下的丢失更新。
    sb.rpc("increment_template_usage", {"p_template_id": int(template_id)})


# =========================================================================== #
# 表级用法计数（table_catalog 命中自进化）
# =========================================================================== #
def increment_table_usage(table_name: str) -> None:
    try:
        sb.rpc("increment_table_usage", {"p_table_name": table_name})
    except Exception as e:  # noqa: BLE001
        logger.warning("累加表使用计数失败：%s", e)


def upsert_table_knowledge(table_name: str, patch: dict[str, Any]) -> dict[str, Any] | None:
    """更新单表元数据（描述/标签/关键字段/入口字段），不存在则插入。upsert 按 (db_name, table_name)。"""
    db_name = patch.get("db_name") or "srm"
    url = f"{sb.base_url()}/rest/v1/{sb.config.TABLE_CATALOG_TABLE}?on_conflict=db_name%2Ctable_name"
    resp = sb._rest_request("POST", url, body={**patch, "table_name": table_name, "db_name": db_name})
    return resp[0] if resp else None


# relation source 枚举（供 SQL Agent 判断可信度）
VALID_RELATION_SOURCES = (
    "archery_select",   # 经 Archery/SELECT 实测两端字段与 join 结果验证
    "ddl",              # 由数据库 DDL / 外键推断
    "manual",           # 人工确认（默认）
    "inferred",         # 自动推断（未实测，需谨慎）
)

_COLUMN_EXISTS_CACHE: dict[tuple[str, str], bool] = {}


def _has_column(table: str, column: str) -> bool:
    """探测指定表是否含某列（避免写入不存在的列导致 REST 400）。"""
    cache_key = (table, column)
    if cache_key in _COLUMN_EXISTS_CACHE:
        return _COLUMN_EXISTS_CACHE[cache_key]
    try:
        rows = sb.query_table(table, select=column, limit=1)
        exists = True
    except Exception:  # noqa: BLE001
        exists = False
    _COLUMN_EXISTS_CACHE[cache_key] = exists
    return exists


def add_table_relation(
    from_table: str, to_table: str, join_on: str,
    relation_type: str = "ref", description: str = "",
    confidence: float = 1.0, from_db: str = "srm", to_db: str = "srm",
    verified: bool = False, source: str = "manual",
) -> dict[str, Any] | None:
    """沉淀一条表关联关系（upsert 按 from_table,to_table,join_on）。

    可信度属性（P0-3 增强，供 SQL Agent 判断 JOIN 可靠性）：
      - confidence：0~1，对 join 正确性的置信度。
      - verified：是否已经 Archery/SELECT 实测验证过两端字段与结果。
      - source：来源（archery_select / ddl / manual / inferred）。
    兼容性：verified/source 列如生产表不存在则跳过写入，不阻塞主流程。
    """
    body: dict[str, Any] = {
        "from_table": from_table, "to_table": to_table, "join_on": join_on,
        "relation_type": relation_type, "description": description or "",
        "confidence": max(0.0, min(1.0, float(confidence))),
        "from_db": from_db or "srm", "to_db": to_db or "srm",
    }
    src = (source or "manual").strip().lower()
    if src not in VALID_RELATION_SOURCES:
        src = "manual"
    # verified 列已被 get_relations select 使用，安全写入；source 列先探测
    body["verified"] = bool(verified)
    if _has_column(sb.config.TABLE_RELATION_TABLE, "source"):
        body["source"] = src
    url = f"{sb.base_url()}/rest/v1/{sb.config.TABLE_RELATION_TABLE}?on_conflict=from_table%2Cto_table%2Cjoin_on"
    resp = sb._rest_request("POST", url, body=body)
    return resp[0] if resp else None


# =========================================================================== #
# table_catalog
# =========================================================================== #
def get_table(table_name: str, db_name: str | None = None) -> dict[str, Any] | None:
    eq: dict[str, Any] = {"table_name": table_name}
    if db_name:
        eq["db_name"] = db_name
    rows = sb.query_table(sb.config.TABLE_CATALOG_TABLE, select="*", eq=eq, limit=1)
    return rows[0] if rows else None


def search_tables_keyword(
    keyword: str, domain: str | None = None, db_name: str | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    try:
        return sb.rpc("search_table_catalog_keyword", {
            "keyword": keyword, "match_count": limit,
            "filter_domain": domain, "filter_db": db_name,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("关键词检索表失败：%s", e)
        return []


def search_tables_semantic(
    query: str, domain: str | None = None, db_name: str | None = None, limit: int = 5,
) -> list[dict[str, Any]]:
    if not sb.embedding.available:
        return []
    q_emb = sb.embedding.embed_query(query)
    try:
        return sb.rpc(sb.embedding.rpc_name("table"), {
            "query_embedding": sb.embedding.to_literal(q_emb),
            "match_count": limit, "filter_domain": domain, "filter_db": db_name,
        })
    except Exception as e:  # noqa: BLE001
        logger.warning("语义检索表失败：%s", e)
        return []


# =========================================================================== #
# table_relations
# =========================================================================== #
def get_relations(table_name: str) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    base_select = "from_table,to_table,join_on,relation_type,description,confidence,from_db,to_db,verified"
    # 兼容：source 列存在才 select，避免旧表上 REST 400
    if _has_column(sb.config.TABLE_RELATION_TABLE, "source"):
        base_select += ",source"
    for col in ("from_table", "to_table"):
        try:
            rows = sb.query_table(
                sb.config.TABLE_RELATION_TABLE,
                select=base_select,
                eq={col: table_name}, limit=100,
            )
            for row in rows or []:
                key = (row.get("from_table"), row.get("to_table"), row.get("join_on"))
                if key not in seen:
                    seen.add(key)
                    out.append(row)
        except Exception as e:  # noqa: BLE001
            logger.warning("查询关系失败(%s)：%s", col, e)
    return out


# =========================================================================== #
# 通用去重合并（混合检索：语义优先 + 关键词补充，按 id 去重）
# =========================================================================== #
def merge_by_id(primary_ids: list[Any] | None, *row_lists: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[Any] = set()
    merged: list[dict[str, Any]] = []
    for rows in row_lists:
        for r in rows:
            rid = r.get("id")
            if rid in seen:
                continue
            seen.add(rid)
            merged.append(r)
    if primary_ids:
        ordered = [r for rid in primary_ids for r in merged if r.get("id") == rid]
        rest = [r for r in merged if r.get("id") not in set(primary_ids)]
        return ordered + rest
    return merged
