"""zhenyun-pangu-mcp — 甄云盘古通用工具 MCP（完全自包含，无外部仓库依赖）。

工具按前缀分组：
  - obs_*       日志查询（阿里云 SLS：国内公有云盘古 prod/dev/test；Loki：仅 AWS 海外）
  - archery_*   数据库查询（Archery 双站点 cn/aws + 盘古专属租户/实例/库列表）
  - es_*        正式环境 ES 只读查询（整合自 es-prod；铁律：严禁写、单次 ≤ ES_MAX_SIZE）
  - search_*_scripts 适配器/独立脚本身份发现（当前正文改由 Script Platform MCP 读取）
  - choerodon_* 猪齿鱼协作（内置 Python 客户端，OAuth 账号密码登录）
  - search_repo 跨仓代码搜索（内置纯标准库文件遍历，零外部依赖）
  - gitlab_*    已知 GitLab 项目/分支/路径的精确读取（搜索默认禁用）
  - search/get/save_* 认知层知识、SQL 模板、表目录和关联关系检索/维护

工具选择原则：按缺失事实选择最短入口；规则/历史方案不明时才查认知层，当前事实由
日志/Archery/GitLab/猪齿鱼提供。复用同范围有效证据；认知层写工具不执行业务写 SQL。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Literal

import requests
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Settings as FastMCPSettings

from . import (
    adapter_scripts, archery, choerodon, delivery_quality, es, loki, search,
    sls, sls_config, gitlab, standalone_scripts,
)
from .config import (
    ARCHERY_INSTANCE_ALIASES,
    ARCHERY_DEFAULT_DB,
    GITLAB_SEARCH_ENABLED,
    LOKI_PLATFORMS,
    PANGU_EXPOSE_LEGACY_SCRIPT_READ_TOOLS,
)
from .knowledge_base import service as kb
from . import workflow
from .tool_policy import tool_annotations

# MCP 1.29 + Pydantic Settings 2.15 leaves the generic lifespan annotation
# unresolved until an explicit rebuild.  Resolve it before constructing FastMCP
# so schema/setting validation is complete and startup stays warning-free.
FastMCPSettings.model_rebuild()
mcp = FastMCP(
    "zhenyun-pangu-mcp",
    instructions=(
        "SRM tools: business DB/ES read-only; knowledge/comment tools have side effects. "
        "Script discovery tools only locate adapter/independent identities; use "
        "zhenyun-script-platform-mcp as the authoritative source for current script body, "
        "version, debug, save, and deployment. "
        "When skills are unavailable or a task crosses workflows, use get_workflow_guide; "
        "otherwise call the specific tool directly. Reuse scoped evidence, verify live facts, "
        "and do not treat tool annotations as user authorization."
    ),
)


def _tool():
    """Register with an explicit side-effect policy rather than SDK write defaults."""
    def register(fn):
        return mcp.tool(annotations=tool_annotations(fn.__name__))(fn)
    return register


def _legacy_script_read_tool():
    """Keep old Python APIs for rollback without advertising duplicate MCP tools."""
    if PANGU_EXPOSE_LEGACY_SCRIPT_READ_TOOLS:
        return _tool()
    return lambda fn: fn


BJ = timezone(timedelta(hours=8))
MAX_LOG_QUERY_SPAN = 31 * 24 * 3600


def _advertise_nonempty_any_of(tool_name: str, *fields: str) -> None:
    """Add a cross-field requirement to the generated MCP input schema.

    FastMCP derives a flat schema from the Python signature and cannot infer rules
    such as "tenant, service, or query must be provided".  Keep the convenient
    flat API, but publish the real constraint so clients can validate before a
    tool call.  The tool function still validates the same rule defensively.
    """
    parameters = mcp._tool_manager._tools[tool_name].parameters
    parameters.setdefault("allOf", []).append({
        "anyOf": [
            {
                "required": [field],
                "properties": {field: {"type": "string", "minLength": 1}},
            }
            for field in fields
        ]
    })


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# 统一 MCP Response（P0 标准化：ok / error.code / error.retryable / meta.*）
# ---------------------------------------------------------------------------
# 目标：所有工具返回结构统一，方便 Agent 判断成功/失败与是否可重试。
#   - 成功：{..., "ok": true, "meta": {"source": <来源>, "observed_at": <时间>}}
#   - 失败：{"ok": false, "error": {"code": <分类>, "message": <人类可读>, "retryable": <是否可重试>}}
# 兼容性：保留原有顶层业务字段（results/query/count 等），仅在结构外层补充 ok/meta，
# 不破坏现有 Skill 对返回的解析。


def _now_str() -> str:
    return datetime.now(BJ).strftime("%Y-%m-%d %H:%M:%S %Z")


def _err(code: str, message: str, retryable: bool = False) -> str:
    """统一失败响应：含错误分类与是否可重试。"""
    return _json({
        "ok": False,
        "error": {"code": code, "message": message, "retryable": bool(retryable)},
    })


def _ok(data: object, source: str) -> str:
    """统一成功响应：保留业务字段，顶层补充 ok=true 与 meta。"""
    if isinstance(data, dict):
        data["ok"] = True
        meta = data.get("meta")
        if not isinstance(meta, dict):
            meta = {}
            data["meta"] = meta
        meta.setdefault("source", source)
        meta.setdefault("observed_at", _now_str())
    return _json(data)


# ============================================================================
# 时间解析（北京时间）
# ============================================================================

def _time_bounds(from_time: int | None, to_time: int | None, time_range: str) -> tuple[int, int]:
    """统一时间窗解析（Loki 与 SLS 共用，避免两套实现语义漂移）。

    支持三种入参：
      1. from_time/to_time（秒级时间戳，传任一即可，缺省侧按 2 小时补齐）；
      2. 相对时间：中英文皆可 —— 30m/2h/1d、最近30分钟/最近2小时/最近3天；
      3. 自然语言：今天/昨天/前天/本周/上周/本月/上月（today/yesterday 亦可）；
      4. 绝对时间："YYYY-MM-DD HH:mm~HH:mm"（按北京时间解析）。
    """
    now = int(time.time())
    if from_time is not None or to_time is not None:
        end = int(to_time if to_time is not None else now)
        return int(from_time if from_time is not None else end - 7200), end

    raw = (time_range or "2h").strip().lower()
    text = raw.replace(" ", "")
    current = datetime.now(BJ)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in {"today", "今天", "今日"}:
        return int(today.timestamp()), now
    if text in {"yesterday", "昨天"}:
        return int((today - timedelta(days=1)).timestamp()), int(today.timestamp() - 1)
    if text in {"前天", "前日"}:
        start = today - timedelta(days=2)
        return int(start.timestamp()), int((start + timedelta(days=1)).timestamp() - 1)
    if text in {"本周", "这周", "本星期"}:
        start = today - timedelta(days=current.weekday())
        return int(start.timestamp()), now
    if text in {"上周", "上一周"}:
        week_end = today - timedelta(days=current.weekday())
        start = week_end - timedelta(days=7)
        return int(start.timestamp()), int(week_end.timestamp() - 1)
    if text in {"本月", "这个月"}:
        return int(today.replace(day=1).timestamp()), now
    if text in {"上月", "上个月"}:
        month_end = today.replace(day=1)
        start = (month_end - timedelta(days=1)).replace(day=1)
        return int(start.timestamp()), int(month_end.timestamp() - 1)
    # 相对时间（英文）：30m / 2h / 1d
    rel = re.match(r"^(\d+)(m|h|d)$", text)
    if rel:
        n = int(rel.group(1))
        seconds = {"m": 60, "h": 3600, "d": 86400}[rel.group(2)]
        return now - n * seconds, now
    # 相对时间（中文）：最近30分钟 / 最近2小时 / 最近3天
    for unit, seconds in (("分钟", 60), ("小时", 3600), ("天", 86400)):
        if unit in text:
            number = "".join(c for c in text.split(unit)[0] if c.isdigit())
            if number:
                return now - int(number) * seconds, now
    # 绝对时间 "YYYY-MM-DD HH:mm~HH:mm"（保留空格，避免 strptime 解析失败）
    if "~" in raw:
        parts = raw.split("~")
        if len(parts) != 2:
            raise ValueError('绝对时间格式错误，应为 "YYYY-MM-DD HH:mm~HH:mm"')
        start = int(datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BJ).timestamp())
        end = int(datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BJ).timestamp())
        return start, end
    return now - 7200, now


def _validate_time_bounds(start: int, end: int) -> tuple[int, int]:
    """拒绝反向/空时间窗和过宽查询，避免日志 API 被无意打爆。"""
    if end <= start:
        raise ValueError("时间范围无效：to_time 必须晚于 from_time")
    span = end - start
    if span > MAX_LOG_QUERY_SPAN:
        raise ValueError("时间范围过大：单次日志查询最多支持 31 天")
    return int(start), int(end)


def _bounded_limit(value: int, maximum: int) -> int:
    """把工具入参限制在服务端允许范围内，并把非法值转成明确的参数错误。"""
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError) as e:
        raise ValueError("limit 必须是整数") from e


def _timeout_hint(start: int, end: int) -> str:
    """Loki 查询失败/超时时的处置建议（基于时间窗宽窄给出可执行提示）。"""
    span = max(0, end - start)
    if span > 2 * 3600:
        return "（时间窗较宽导致查询慢/超时，请缩小时间范围后重试）"
    return "（查询失败，请确认 query 是否正确、env 是否来自 obs_log_datasources）"


# 国内公有云盘古日志已迁回阿里云 SLS（prod/dev/test 全覆盖），Loki 仅保留 AWS 海外。
# 传 region="cn" 时给出明确的可执行提示，避免 Agent 反复用错误工具重试。
_CN_LOKI_HINT = (
    "国内公有云(cn)盘古日志已全部迁回阿里云 SLS，Loki(obs_log_*)仅支持 AWS 海外；"
    "查国内盘古请用 obs_sls_query(environment=\"prod\"|\"dev\"|\"test\")。"
)


def _check_loki_region(region: str) -> str | None:
    """校验 Loki 的 region 参数；不合法时返回统一错误串，合法返回 None。"""
    if region in LOKI_PLATFORMS:
        return None
    if region == "cn":
        return _err("bad_param", _CN_LOKI_HINT)
    return _err(
        "bad_param",
        f"未知 region: {region}（Loki 仅支持 {list(LOKI_PLATFORMS)}）；{_CN_LOKI_HINT}",
    )


# ============================================================================
# obs_* 日志工具
# ============================================================================

@_tool()
def obs_log_query(
    region: str = "aws",
    env: str = "nonprod",
    query: str = "",
    time_range: str = "2h",
    from_time: int | None = None,
    to_time: int | None = None,
    limit: int = 50,
    direction: str = "BACKWARD",
) -> str:
    """查询 AWS 海外 Loki 日志；国内盘古改用 obs_sls_query，时间窗与结果有界，返回 JSON ok 或 error.retryable。"""
    region_error = _check_loki_region(region)
    if region_error:
        return region_error
    if not query or not query.strip():
        return _err("bad_param", "query 不能为空，必须传入 LogQL 表达式，如 '{app=\"srm-gateway\"} |= \"xxx\"'")
    try:
        ds_name = loki.resolve_datasource(region, env)
    except loki.LokiError as e:
        return _err("config", str(e), retryable=False)
    try:
        client = loki._get_client(region)
        uid = client.resolve_uid(ds_name)
    except loki.LokiError as e:
        return _err("loki_auth", str(e), retryable=True)

    warning = loki.warn_unscoped(query)

    try:
        start, end = _validate_time_bounds(*_time_bounds(from_time, to_time, time_range))
        limit = _bounded_limit(limit, 5000)
    except ValueError as e:
        return _err("bad_param", str(e), retryable=False)
    try:
        resp = client.loki_query_range(uid, query, start, end, limit, direction)
    except loki.LokiError as e:
        hint = _timeout_hint(start, end)
        return _err("loki_query", f"{e}{hint}", retryable=True)

    if resp.get("status") != "success":
        return _err("loki_query", f"Loki 查询失败: {json.dumps(resp)[:500]}", retryable=True)

    rows = []
    for stream in resp.get("data", {}).get("result", []):
        for ns, line in stream.get("values", []):
            rows.append({"ts": int(ns) // 1_000_000_000, "line": line})

    def fmt(sec: int) -> str:
        return datetime.fromtimestamp(sec, BJ).strftime("%Y-%m-%d %H:%M:%S")

    rows.sort(key=lambda r: r["ts"], reverse=(direction == "BACKWARD"))
    top = rows[:limit]
    return _ok({
        "region": region,
        "env": env,
        "datasource": ds_name,
        "query": query,
        "start": fmt(start),
        "end": fmt(end),
        "total": len(rows),
        **({"warning": warning} if warning else {}),
        "results": [{"time": fmt(r["ts"]), "line": r["line"][:400]} for r in top],
    }, "loki")


@_tool()
def obs_log_trace(
    trace_id: str,
    region: str = "aws",
    env: str = "nonprod",
    time_range: str = "2h",
    from_time: int | None = None,
    to_time: int | None = None,
    limit: int = 200,
    direction: str = "BACKWARD",
    level: str = "all",
    clip_len: int = 600,
) -> str:
    """按 traceId 查询 AWS 海外 Loki 调用链；国内盘古改用 obs_sls_query，返回裁剪后的时间线与 JSON ok/error.retryable。"""
    region_error = _check_loki_region(region)
    if region_error:
        return region_error
    try:
        loki.resolve_datasource(region, env)
    except loki.LokiError as e:
        return _err("config", str(e), retryable=False)

    try:
        start, end = _validate_time_bounds(*_time_bounds(from_time, to_time, time_range))
        limit = _bounded_limit(limit, 5000)
    except ValueError as e:
        return _err("bad_param", str(e), retryable=False)
    try:
        rows, meta = loki.query_trace(
            region, env, trace_id, start, end, limit, direction,
            level=level, clip_len=clip_len,
        )
    except loki.LokiError as e:
        return _err("loki_query", f"{e}{_timeout_hint(start, end)}", retryable=True)

    def fmt(sec: int) -> str:
        return datetime.fromtimestamp(sec, BJ).strftime("%Y-%m-%d %H:%M:%S")

    return _ok({
        **meta,
        "start": fmt(start),
        "end": fmt(end),
        "total": len(rows),
        "results": [{"time": fmt(r["ts_ns"] // 1_000_000_000), "line": r["line"]} for r in rows],
    }, "loki")


@_tool()
def obs_log_datasources(region: str = "aws") -> str:
    """列出 AWS 海外 Loki 数据源；只读，返回 JSON ok 或 error.retryable。"""
    region_error = _check_loki_region(region)
    if region_error:
        return region_error
    try:
        client = loki._get_client(region)
        ds_list = client.discover_loki_datasources()
    except loki.LokiError as e:
        return _err("loki_auth", str(e), retryable=True)
    return _ok({
        "region": region,
        "label": LOKI_PLATFORMS[region]["label"],
        "count": len(ds_list),
        "datasources": [
            {"id": d.get("id"), "uid": d.get("uid"), "name": d.get("name"), "isDefault": d.get("isDefault")}
            for d in ds_list
        ],
    }, "loki")


# ============================================================================
# archery_* 数据库工具
# ============================================================================

@_tool()
def archery_query(
    sql: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
    limit: int = 100,
) -> str:
    """执行 Archery 只读 SQL；site 必须为 cn/aws，允许单条 SELECT/EXPLAIN SELECT/SHOW CREATE TABLE 及白名单无副作用函数，拒绝子查询、窗口、多语句、注释和写入。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.query(sql, instance_name, db_name, max(1, min(int(limit), 5000)))
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@_tool()
def archery_describe_table(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取 Archery 实时 SHOW CREATE TABLE 结构；只读，返回 JSON ok 或 error.retryable。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.describe_table(instance_name, db_name, table)
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@_tool()
def archery_list_columns(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取 Archery 实时字段名；只读，返回 JSON ok 或 error.retryable。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        columns = client.list_columns(instance_name, db_name, table)
        return _ok({"site": site, "instance": instance_name, "db": db_name, "table": table, "columns": columns}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@_tool()
def archery_query_tenant(
    tenant: str = "",
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """按必填 tenant 查询 hpfm_tenant 的租户信息；site 必须为 cn/aws，空参返回参数错误，不执行全表列举。"""
    if not tenant or not tenant.strip():
        return _err("archery_query_tenant", "tenant 必填：请传租户编码或名称，不支持空参列举租户")
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        result = archery.query_tenant(site, tenant or None, instance_name, db_name)
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@_tool()
def archery_list_databases(
    site: str = "cn",
    instance: str | None = None,
) -> str:
    """列出指定 Archery 实例数据库；这是固定只读发现能力，不开放任意 SHOW，返回 JSON ok 或 error.retryable。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        result = archery.query_db_list(site, instance_name)
        return _ok({"site": site, "instance": instance_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@_tool()
def archery_list_instances(site: Literal["", "cn", "aws"] = "") -> str:
    """列出按 site 分组的 Archery 实例别名；只读，调用后按返回值传 site，返回 JSON ok 或 error.retryable。"""
    selected = site.strip().lower()
    if selected and selected not in ARCHERY_INSTANCE_ALIASES:
        return _err(
            "archery_instance_site",
            f"未知 Archery 站点: {site}（可选 cn/aws，留空返回全部）",
            retryable=False,
        )
    instances = (
        {selected: ARCHERY_INSTANCE_ALIASES[selected]}
        if selected else ARCHERY_INSTANCE_ALIASES
    )
    return _ok({
        "instances_by_site": instances,
        "requested_site": selected or None,
        "default_site": "cn",
        "default_db": ARCHERY_DEFAULT_DB,
        "note": "查询实例时须同时传对应 site（如 aws 实例传 site=\"aws\"），"
                "仅传 instance 而不传 site 会按默认 site=cn 解析而报「未关联该实例」。",
    }, "archery")


_DB_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$-]*(?:\.[A-Za-z_][A-Za-z0-9_$-]*)?$")


def _validate_db_identifier(value: str, label: str) -> str:
    value = (value or "").strip()
    if not _DB_IDENTIFIER.fullmatch(value):
        raise ValueError(f"{label} 不是安全的表名/字段名")
    return value


def _quote_db_identifier(value: str) -> str:
    return ".".join(f"`{part}`" for part in value.split("."))


@_tool()
def inspect_object_relation(
    source_object: str,
    source_field: str,
    target_object: str,
    target_field: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
    sample_limit: int = 5,
) -> str:
    """核验两个数据库对象的字段、关联条件和受限样例；只读，缺字段时返回 field_exists=false 与 JSON error.retryable。"""
    try:
        source_object = _validate_db_identifier(source_object, "source_object")
        source_field = _validate_db_identifier(source_field, "source_field")
        target_object = _validate_db_identifier(target_object, "target_object")
        target_field = _validate_db_identifier(target_field, "target_field")
        sample_limit = _bounded_limit(sample_limit, 20)
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        source_columns = client.list_columns(instance_name, db_name, source_object)
        target_columns = client.list_columns(instance_name, db_name, target_object)
        source_ddl = client.describe_table(instance_name, db_name, source_object)
        target_ddl = client.describe_table(instance_name, db_name, target_object)
        source_exists = source_field.split(".")[-1] in source_columns
        target_exists = target_field.split(".")[-1] in target_columns
        source_q = _quote_db_identifier(source_object)
        target_q = _quote_db_identifier(target_object)
        source_f = _quote_db_identifier(source_field)
        target_f = _quote_db_identifier(target_field)
        recommended_query = (
            f"SELECT s.{source_f} AS source_value, t.{target_f} AS target_value "
            f"FROM {source_q} s JOIN {target_q} t "
            f"ON s.{source_f} = t.{target_f} LIMIT {sample_limit}"
        )
        samples = []
        if source_exists and target_exists:
            result = client.query(recommended_query, instance_name, db_name, sample_limit)
            samples = result.get("rows", [])
        return _ok({
            "site": site,
            "instance": instance_name,
            "db": db_name,
            "source": {
                "object": source_object,
                "field": source_field,
                "field_exists": source_exists,
                "columns": source_columns,
                "ddl": source_ddl.get("create_table", ""),
            },
            "target": {
                "object": target_object,
                "field": target_field,
                "field_exists": target_exists,
                "columns": target_columns,
                "ddl": target_ddl.get("create_table", ""),
            },
            "relation": {
                "join_condition": f"{source_object}.{source_field} = {target_object}.{target_field}",
                "sample_count": len(samples),
                "sample_rows": samples,
                "verified_by_sample": bool(samples),
            },
            "recommended_query": recommended_query,
            "next_step": (
                "字段和 JOIN 样例已通过实时数据库核验，可写入字段来源表。"
                if samples else
                "两端字段存在，但当前样例查询未命中，关联关系仍需结合业务条件核实。"
                if source_exists and target_exists else
                "至少一个字段不存在，停止从对象中直接取值；请重新确认对象、字段或关联路径。"
            ),
        }, "archery")
    except (ValueError, archery.ArcheryError) as e:
        return _err("object_relation", str(e), retryable=isinstance(e, archery.ArcheryError))


# ============================================================================
# choerodon_* 猪齿鱼工具（内置 Python 客户端,无外部脚本依赖）
# ============================================================================

def _choerodon_call(dispatch_name: str, **kwargs) -> str:
    try:
        fn = choerodon.CHOERODON_DISPATCH[dispatch_name]
        data = fn(**kwargs)
        if isinstance(data, dict) and data.get("ok") is False:
            # 底层返回的显式失败（如写操作前置校验失败），透传其错误信息
            return _err("choerodon", data.get("note") or str(data), retryable=False)
        return _ok(data, "choerodon")
    except choerodon.ChoerodonError as e:
        # 认证/网络/解析等可重试错误
        return _err("choerodon", str(e), retryable=True)
    except Exception as e:  # 其它未知异常,不抛 500
        return _err("choerodon", f"{type(e).__name__}: {e}", retryable=False)


@_tool()
def choerodon_list_projects(keyword: str = "", size: int = 100) -> str:
    """列出或搜索当前账号可访问的猪齿鱼项目；只读，返回真实 projectId 与 JSON ok/error.retryable。"""
    return _choerodon_call("list_projects", keyword=keyword, size=size)


@_tool()
def choerodon_query_issue(issue_id: str, project_id: str = "") -> str:
    """按真实加密 issue_id 查询猪齿鱼任务详情；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("query_issue", issue_id=issue_id, project_id=project_id or None)


@_tool()
def choerodon_list_issue(
    keyword: str = "",
    assignee: str = "",
    status: str = "",
    size: int = 20,
    project_id: str = "",
) -> str:
    """按关键词、经办人或状态查询猪齿鱼任务；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call(
        "list_issue", keyword=keyword, assignee=assignee, status=status,
        size=size, project_id=project_id or None,
    )


@_tool()
def choerodon_search_users(name: str, size: int = 50, project_id: str = "") -> str:
    """搜索猪齿鱼项目成员并返回真实用户 id；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("search_users", name=name, size=size, project_id=project_id or None)


@_tool()
def choerodon_get_status_map(project_id: str = "") -> str:
    """读取猪齿鱼项目状态名到加密 id 的映射；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("get_status_map", project_id=project_id or None)


@_tool()
def choerodon_search_tasks_by_person(name: str, size: int = 50, project_id: str = "") -> str:
    """按经办人查询猪齿鱼任务；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("search_tasks_by_person", name=name, size=size, project_id=project_id or None)


@_tool()
def choerodon_list_attachments(issue_id: str, project_id: str = "") -> str:
    """列出猪齿鱼任务附件；只读，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("list_attachments", issue_id=issue_id, project_id=project_id or None)


@_tool()
def choerodon_download_attachment(file_url: str) -> str:
    """按真实 file_url 获取猪齿鱼附件签名地址；只读，不上传或回写文件，返回 JSON ok/error.retryable。"""
    return _choerodon_call("download_attachment", file_url=file_url)


@_tool()
def choerodon_list_comments(issue_id: str, size: int = 100, project_id: str = "") -> str:
    """读取猪齿鱼任务评论；只读，写评论使用独立的确认边界，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("list_comments", issue_id=issue_id, size=size, project_id=project_id or None)


@_tool()
def choerodon_add_comment(issue_id: str, comment: str, project_id: str = "") -> str:
    """向猪齿鱼写入规范 Markdown 评论；真实副作用，必须先确认内容，返回 JSON ok 或 error.retryable。"""
    return _choerodon_call("create_comment", issue_id=issue_id, comment=comment, project_id=project_id or None)


# ============================================================================
# search_repo 跨仓搜索（内置纯标准库文件遍历,无外部脚本依赖）
# ============================================================================

@_tool()
def search_repo(
    keyword: str,
    mode: str = "content",
    max_results: int = 30,
    context: int = 2,
    depth: int = 4,
) -> str:
    """在 PG_ROOT 范围内搜索本地代码内容、文件名或模块；只读且有界，返回 JSON ok 或 error.retryable。"""
    try:
        return _ok(search.search_repo(
            keyword, mode=mode, max_results=max_results,
            context=context, depth=depth,
        ), "local-repo")
    except Exception as e:  # 文件系统错误等
        return _err("search_repo", str(e), retryable=False)


# ============================================================================
# adapter_script_* 数据库存储脚本（Base64 仅停留在 MCP 内部）
# ============================================================================

@_tool()
def search_adapter_scripts(
    tenant: str = "",
    running_service: str = "",
    query: str = "",
    enabled_only: bool = True,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
    limit: int = 20,
) -> str:
    """发现适配器脚本身份而不返回正文；tenant、running_service、query 至少一项，当前源码须回到 Script Platform MCP。"""
    if not any(value.strip() for value in (tenant, running_service, query)):
        raise ValueError("tenant、running_service、query 至少提供一项非空值")
    try:
        data = adapter_scripts.service.search_scripts(
            tenant=tenant,
            service=running_service,
            query=query,
            enabled_only=enabled_only,
            site=site,
            instance=instance,
            db=db,
            limit=limit,
        )
        return _ok(data, "adapter-script")
    except archery.ArcheryError as e:
        return _err("adapter_script_query", str(e), retryable=True)
    except adapter_scripts.AdapterScriptError as e:
        return _err("adapter_script", str(e), retryable=False)


_advertise_nonempty_any_of(
    "search_adapter_scripts", "tenant", "running_service", "query"
)


@_legacy_script_read_tool()
def get_adapter_script_info(
    script_id: int,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取适配器脚本轻量元信息；只读且不返回 Base64 正文，返回 JSON ok 或 error.retryable。"""
    try:
        return _ok(adapter_scripts.service.get_info(
            script_id, site=site, instance=instance, db=db,
        ), "adapter-script")
    except archery.ArcheryError as e:
        return _err("adapter_script_query", str(e), retryable=True)
    except adapter_scripts.AdapterScriptError as e:
        return _err("adapter_script", str(e), retryable=False)


@_legacy_script_read_tool()
def get_adapter_script_source(
    script_id: int,
    start_line: int = 1,
    end_line: int = 0,
    full: bool = False,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取已解码的适配器源码局部或全文；只读、有界且不返回 Base64，返回 JSON ok 或 error.retryable。"""
    try:
        return _ok(adapter_scripts.service.get_source(
            script_id,
            start_line=start_line,
            end_line=end_line,
            full=full,
            site=site,
            instance=instance,
            db=db,
        ), "adapter-script")
    except archery.ArcheryError as e:
        return _err("adapter_script_query", str(e), retryable=True)
    except adapter_scripts.AdapterScriptError as e:
        return _err("adapter_script", str(e), retryable=False)


@_legacy_script_read_tool()
def search_adapter_script_source(
    script_id: int,
    query: str,
    context_lines: int = 10,
    max_matches: int = 20,
    regex: bool = False,
    case_sensitive: bool = False,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """在已解码的适配器源码中搜索少量上下文；只读，不返回 Base64 或默认全文。"""
    try:
        return _ok(adapter_scripts.service.search_source(
            script_id,
            query,
            context_lines=context_lines,
            max_matches=max_matches,
            regex=regex,
            case_sensitive=case_sensitive,
            site=site,
            instance=instance,
            db=db,
        ), "adapter-script")
    except archery.ArcheryError as e:
        return _err("adapter_script_query", str(e), retryable=True)
    except adapter_scripts.AdapterScriptError as e:
        return _err("adapter_script", str(e), retryable=False)


# ============================================================================
# standalone_script_* 独立脚本（Marmot 脚本库，rel-table 宽表虚拟表）
# ============================================================================

@_tool()
def search_standalone_scripts(
    tenant: str = "",
    query: str = "",
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
    limit: int = 20,
) -> str:
    """发现独立脚本身份而不返回正文；tenant 或 query 至少一项，当前源码须回到 Script Platform MCP。"""
    if not any(value.strip() for value in (tenant, query)):
        raise ValueError("tenant、query 至少提供一项非空值")
    try:
        data = standalone_scripts.service.search_scripts(
            tenant=tenant,
            query=query,
            site=site,
            instance=instance,
            db=db,
            limit=limit,
        )
        return _ok(data, "standalone-script")
    except archery.ArcheryError as e:
        return _err("standalone_script_query", str(e), retryable=True)
    except standalone_scripts.AdapterScriptError as e:
        return _err("standalone_script", str(e), retryable=False)


_advertise_nonempty_any_of("search_standalone_scripts", "tenant", "query")


@_legacy_script_read_tool()
def get_standalone_script_info(
    script_id: int,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取独立脚本轻量元信息；只读且不返回 Base64 正文，返回 JSON ok 或 error.retryable。"""
    try:
        return _ok(standalone_scripts.service.get_info(
            script_id, site=site, instance=instance, db=db,
        ), "standalone-script")
    except archery.ArcheryError as e:
        return _err("standalone_script_query", str(e), retryable=True)
    except standalone_scripts.AdapterScriptError as e:
        return _err("standalone_script", str(e), retryable=False)


@_legacy_script_read_tool()
def get_standalone_script_source(
    script_id: int,
    start_line: int = 1,
    end_line: int = 0,
    full: bool = False,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取独立脚本 longValue5 解码后的正文；只读、有界且不返回 Base64，返回 JSON ok 或 error.retryable。"""
    try:
        return _ok(standalone_scripts.service.get_source(
            script_id,
            start_line=start_line,
            end_line=end_line,
            full=full,
            site=site,
            instance=instance,
            db=db,
        ), "standalone-script")
    except archery.ArcheryError as e:
        return _err("standalone_script_query", str(e), retryable=True)
    except standalone_scripts.AdapterScriptError as e:
        return _err("standalone_script", str(e), retryable=False)


@_legacy_script_read_tool()
def search_standalone_script_source(
    script_id: int,
    query: str,
    context_lines: int = 10,
    max_matches: int = 20,
    regex: bool = False,
    case_sensitive: bool = False,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """在独立脚本 longValue5 源码中搜索少量上下文；只读，不返回测试用例或默认全文。"""
    try:
        return _ok(standalone_scripts.service.search_source(
            script_id,
            query,
            context_lines=context_lines,
            max_matches=max_matches,
            regex=regex,
            case_sensitive=case_sensitive,
            site=site,
            instance=instance,
            db=db,
        ), "standalone-script")
    except archery.ArcheryError as e:
        return _err("standalone_script_query", str(e), retryable=True)
    except standalone_scripts.AdapterScriptError as e:
        return _err("standalone_script", str(e), retryable=False)


@_tool()
def check_marmot_script_static(
    source: str,
    script_code: str = "",
    rules_json: str = "",
    expected_sha256: str = "",
    artifact_sha256: str = "",
) -> str:
    """对 Marmot JavaScript 做本地静态门禁检查；不执行脚本，规则由 rules_json 提供，返回 JSON ok 或 error.retryable。"""
    try:
        result = delivery_quality.static_check_script(
            source,
            script_code=script_code,
            rules_json=rules_json,
            expected_sha256=expected_sha256,
            artifact_sha256=artifact_sha256,
        )
        return _ok(result, "local-static-check")
    except Exception as e:  # 防止单个源码样本让 MCP 进程退出
        return _err("static_check", str(e), retryable=False)


# ============================================================================
# gitlab_* 代码平台（GitLab 仓库：项目/代码/文件/目录/分支，整合自 gitlab-code-mcp）
# ============================================================================

@_tool()
def gitlab_search_projects(query: str, per_page: int = 20) -> str:
    """搜索 GitLab 项目；仅在配置显式启用时注册，默认不暴露，返回 JSON ok 或 error.retryable。"""
    if not GITLAB_SEARCH_ENABLED:
        return _err(
            "capability_disabled",
            "GitLab 项目/代码搜索当前未启用，请直接使用 search_repo 检索本地代码。",
            retryable=False,
        )
    try:
        client = gitlab.GitLabClient()
        items = client.list_projects(query, per_page=per_page)
        slim = [
            {
                "id": p.get("id"),
                "name": p.get("name"),
                "path_with_namespace": p.get("path_with_namespace"),
                "web_url": p.get("web_url"),
                "default_branch": p.get("default_branch"),
            }
            for p in items
        ]
        return _ok({"count": len(slim), "projects": slim}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


@_tool()
def gitlab_search_code(query: str, per_page: int = 20) -> str:
    """搜索 GitLab 代码；仅在配置显式启用时注册，默认不暴露，返回 JSON ok 或 error.retryable。"""
    if not GITLAB_SEARCH_ENABLED:
        return _err(
            "capability_disabled",
            "GitLab 项目/代码搜索当前未启用，请直接使用 search_repo 检索本地代码。",
            retryable=False,
        )
    try:
        client = gitlab.GitLabClient()
        results = client.search_code(query, per_page=per_page)
        slim = [
            {
                "project_id": r.get("project_id"),
                "path_with_namespace": r.get("path_with_namespace"),
                "path": r.get("path"),
                "filename": r.get("filename"),
                "startline": r.get("startline"),
                "data": (r.get("data") or "")[:300],
                "ref": r.get("ref"),
            }
            for r in results
        ]
        return _ok({"count": len(slim), "results": slim}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


# 已知不可用的搜索能力不暴露给 Agent，避免每次先等待失败再回退本地。
# 精确分支、目录和文件读取工具在下方继续独立注册。
if not GITLAB_SEARCH_ENABLED:
    mcp.remove_tool("gitlab_search_projects")
    mcp.remove_tool("gitlab_search_code")


@_tool()
def gitlab_get_file(project_id: str, path: str, ref: str = "master") -> str:
    """读取已知 GitLab project/ref/path 的文件；只读，不用枚举绕过搜索禁用，返回 JSON ok 或 error.retryable。"""
    try:
        content = gitlab.GitLabClient().get_file(project_id, path, ref=ref)
        return _ok({"project_id": project_id, "path": path, "ref": ref, "content": content}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


@_tool()
def gitlab_list_tree(
    project_id: str,
    path: str = "",
    ref: str = "master",
    recursive: bool = False,
    per_page: int = 100,
) -> str:
    """列出已知 GitLab 仓库目录树；只读且范围由 recursive 控制，返回 JSON ok 或 error.retryable。"""
    try:
        items = gitlab.GitLabClient().list_tree(
            project_id, path=path, ref=ref, recursive=recursive, per_page=per_page
        )
        return _ok({"count": len(items), "tree": items}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


@_tool()
def gitlab_list_branches(project_id: str, per_page: int = 50) -> str:
    """列出已知 GitLab 仓库分支及保护状态；只读，返回 JSON ok 或 error.retryable。"""
    try:
        branches = gitlab.GitLabClient().list_branches(project_id, per_page=per_page)
        slim = [
            {
                "name": b.get("name"),
                "default": b.get("default"),
                "protected": b.get("protected"),
                "commit_short_id": (b.get("commit") or {}).get("short_id"),
            }
            for b in branches
        ]
        return _ok({"count": len(slim), "branches": slim}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


# ============================================================================
# obs_sls_* 阿里云 SLS 日志（国内公有云盘古 prod + 非生产 dev/test 全覆盖）
# ============================================================================

# 默认时间窗（未显式指定时间时，先按 2 小时查；0 命中再自动扩窗）
_DEFAULT_SLS_RANGES = {"最近2小时", "2h", ""}
# 自动扩窗的备用窗口：最近 24h、最近 72h
_EXPAND_WINDOWS_HOURS = (24, 72)


def _clip_logs(logs: list[dict], clip_len: int) -> list[dict]:
    """按 clip_len 裁剪每条日志的字符串字段（0 表示不裁剪），防止长堆栈撑爆 token。"""
    if clip_len <= 0:
        return logs
    return [
        {key: (value[:clip_len] if isinstance(value, str) else value) for key, value in log.items()}
        for log in logs
    ]


@_tool()
def obs_sls_query(
    environment: str = "prod",
    trace_id: str = "",
    keyword: str = "",
    level: str = "ERROR",
    system: str = "盘古",
    from_time: int = 0,
    to_time: int = 0,
    time_range: str = "最近2小时",
    limit: int = 200,
    auto_expand: bool = True,
    clip_len: int = 2000,
    container_name: str = "",
) -> str:
    """查询国内盘古阿里云 SLS 日志；由 environment 路由项目/日志库且不暴露 AK，时间窗与返回均有界，返回 JSON ok/error.retryable。"""
    try:
        container = container_name.strip()
        if container and not re.fullmatch(r"[A-Za-z0-9._-]+", container):
            raise ValueError("container_name 只能包含字母、数字、点、下划线和连字符")
        target = sls_config.resolve_target(system, environment)
        ak_id, ak_secret = sls_config.credentials(target)
        limit = _bounded_limit(limit, 500)
        start, end = _validate_time_bounds(
            *_time_bounds(from_time or None, to_time or None, time_range)
        )

        normalized_range = (time_range or "").strip().lower().replace(" ", "")
        explicit_window = bool(from_time or to_time) or normalized_range not in _DEFAULT_SLS_RANGES
        windows = [(start, end)]
        if auto_expand and not explicit_window:
            windows.extend([(end - hours * 3600, end) for hours in _EXPAND_WINDOWS_HOURS])

        clauses = [f"_namespace_: {target.namespace}"]
        if container:
            clauses.append(f"_container_name_: {container}")
        if level:
            clauses.append(f"level: {level}")
        if keyword:
            clauses.append(keyword)
        query = " AND ".join(clauses)

        attempted_windows: list[dict] = []
        logs: list[dict] = []
        progress = "Complete"
        for window_start, window_end in windows:
            attempted_windows.append({"from_time": window_start, "to_time": window_end})
            if trace_id:
                logs, progress = sls.query_trace(
                    target.project, target.logstore, ak_id, ak_secret,
                    trace_id, target.namespace, window_start, window_end,
                    sls_config.endpoint(), limit, container,
                )
                query_used = f'"{trace_id}" AND _namespace_: {target.namespace}'
                if container:
                    query_used += f" AND _container_name_: {container}"
            else:
                logs, progress = sls.query_sls(
                    target.project, target.logstore, ak_id, ak_secret,
                    query, window_start, window_end, sls_config.endpoint(), limit,
                )
                query_used = query
            if logs or progress == "Incomplete":
                break

        used_start, used_end = attempted_windows[-1]["from_time"], attempted_windows[-1]["to_time"]
        return _ok({
            "meta": {
                "system": target.system, "environment": target.environment,
                "project": target.project, "logstore": target.logstore,
                "namespace": target.namespace,
                "container_name": container or None,
                "from_time": used_start, "to_time": used_end,
                "from_time_bj": datetime.fromtimestamp(used_start, BJ).strftime("%Y-%m-%d %H:%M:%S"),
                "to_time_bj": datetime.fromtimestamp(used_end, BJ).strftime("%Y-%m-%d %H:%M:%S"),
                "query": query_used, "progress": progress, "count": len(logs),
                "auto_expanded": len(attempted_windows) > 1,
                "attempted_windows": attempted_windows,
                "clip_len": clip_len,
            },
            "logs": _clip_logs(logs, clip_len),
        }, "sls")
    except (ValueError, RuntimeError) as e:
        return _err("sls_query", str(e), retryable=True)


_SCRIPT_TRACE_STAGE_PATTERNS: dict[str, tuple[str, ...]] = {
    "process_start": (r"process\s*start", r"脚本入口", r"入口日志"),
    "data_access": (r"data\s*(?:query|access)", r"查询对象", r"数据查询", r"返回数量"),
    "association": (r"association", r"relation", r"关联补全", r"未匹配", r"unmatched"),
    "request_build": (r"request\s*(?:built|variables|parameters)", r"请求变量", r"参数数量", r"variables_count"),
    "external_response": (r"service response", r"external response", r"外部服务响应", r"顶层失败"),
    "mapping": (r"record mapping", r"line mapping", r"字段映射", r"行映射", r"最终字段"),
    "persistence": (r"persist", r"persistence", r"持久化", r"更新行数"),
    "branch_result": (r"branch", r"分支结果", r"读取已有结果", r"跳过"),
}


def _classify_script_trace_stage(content: str) -> str:
    for stage, patterns in _SCRIPT_TRACE_STAGE_PATTERNS.items():
        if any(re.search(pattern, content, re.IGNORECASE) for pattern in patterns):
            return stage
    return "other"


def _extract_log_count(content: str) -> int | None:
    match = re.search(r"(?:count|数量|返回数量|更新行数|成功数量|失败数量)\s*[:=：]\s*(\d+)", content, re.IGNORECASE)
    return int(match.group(1)) if match else None


@_tool()
def query_script_trace(
    trace_id: str,
    from_time: int = 0,
    to_time: int = 0,
    script_code: str = "",
    container_name: str = "srm-script-container",
    environment: str = "prod",
    system: str = "盘古",
    limit: int = 200,
    include_raw: bool = False,
) -> str:
    """按 traceId 查询 srm-script-container SLS 日志并整理阶段时间线；必须提供时间边界，返回 JSON ok 或 error.retryable。"""
    if not trace_id.strip():
        return _err("bad_param", "trace_id 不能为空", retryable=False)
    if not from_time or not to_time:
        return _err("time_required", "query_script_trace 必须同时提供 from_time 和 to_time，不能只传 traceId", retryable=False)
    try:
        start, end = _validate_time_bounds(int(from_time), int(to_time))
        limit = _bounded_limit(limit, 500)
        container = container_name.strip()
        if not container or re.search(r"[\"'\n\r]", container):
            raise ValueError("container_name 不能为空且不能包含引号/换行")
        target = sls_config.resolve_target(system, environment)
        ak_id, ak_secret = sls_config.credentials(target)
        clauses = [f'"{trace_id.replace(chr(34), "")}"', f"_namespace_: {target.namespace}", f"_container_name_: {container}"]
        if script_code.strip():
            clauses.append(f'"{script_code.replace(chr(34), "")}"')
        query = " AND ".join(clauses)
        logs, progress = sls.query_sls(
            target.project, target.logstore, ak_id, ak_secret, query,
            start, end, sls_config.endpoint(), limit,
        )
        timeline = []
        stage_counts: dict[str, int] = {}
        for log in logs:
            content = str(log.get("content") or log.get("message") or "")
            stage = _classify_script_trace_stage(content)
            stage_counts[stage] = stage_counts.get(stage, 0) + 1
            item = {
                "time": log.get("__time__"),
                "stage": stage,
                "container": log.get("_container_name_") or container,
                "script_code_matched": not script_code.strip() or script_code in content,
                "count": _extract_log_count(content),
                "message": content[:800],
            }
            if include_raw:
                item["raw"] = log
            timeline.append(item)
        return _ok({
            "trace_id": trace_id,
            "script_code": script_code or None,
            "container_name": container,
            "system": system,
            "environment": environment,
            "from_time": start,
            "to_time": end,
            "from_time_bj": datetime.fromtimestamp(start, BJ).strftime("%Y-%m-%d %H:%M:%S"),
            "to_time_bj": datetime.fromtimestamp(end, BJ).strftime("%Y-%m-%d %H:%M:%S"),
            "query": query,
            "progress": progress,
            "count": len(timeline),
            "stage_counts": stage_counts,
            "timeline": timeline,
            "raw_included": include_raw,
            "hint": "未命中时请先确认 script_code、容器名和 from_time/to_time 是否覆盖真实执行时间。",
        }, "sls")
    except (ValueError, RuntimeError) as e:
        return _err("script_trace", str(e), retryable=isinstance(e, RuntimeError))


@_tool()
def obs_sls_targets() -> str:
    """列出国内盘古 SLS 环境与命名空间映射；只读，返回 JSON ok 或 error.retryable。"""
    return _ok({
        "note": "国内公有云盘古 prod 与非生产 dev/test 均在阿里云 SLS；Loki(obs_log_*)仅 AWS 海外。",
        "targets": sls_config.supported_targets(),
    }, "sls")


# ============================================================================
# 知识库「认知层」能力（整合 self sql-template-mcp / knowledge-ops-mcp / table-catalog-mcp）
# 工具语义化分层：Discovery / Context / Composite / Action
# ============================================================================

# ---- Discovery：让 Agent 找东西 ----
@_tool()
def search_knowledge(
    query: str = "",
    knowledge_type: str = "",
    system: str = "",
    module: str = "",
    status: str = "",
    verified_only: bool = False,
    limit: int = 10,
    use_semantic: bool = True,
) -> str:
    """检索盘古认知库知识；只读，支持关键词/语义和过滤条件，返回结果或可读错误。"""
    return kb.search_knowledge(query, knowledge_type, system, module, status, verified_only, limit, use_semantic)


@_tool()
def search_sql_templates(
    keyword: str = "",
    category: str = "",
    system: str = "",
    business_domain: str = "",
    verified_only: bool = False,
    limit: int = 10,
    use_semantic: bool = True,
) -> str:
    """检索 SQL/修复模板；只读，支持关键词/语义和过滤条件，返回结果或可读错误。"""
    return kb.search_sql_templates(keyword, category, system, business_domain, verified_only, limit, use_semantic)


@_tool()
def search_tables(
    query: str,
    domain: str = "",
    db_name: str = "",
    top_k: int = 5,
    use_semantic: bool = True,
) -> str:
    """检索表目录候选；只读，结果不替代实时 Archery 字段事实，返回结果或可读错误。"""
    return kb.search_tables(query, domain, db_name, top_k, use_semantic)


@_tool()
def search_pangu(query: str, system: str = "", module: str = "", category: str = "", top_k: int = 3) -> str:
    """并行发现知识、模板、表及候选关系；只读，不替代实时日志或数据库查询，返回结果或可读错误。"""
    return kb.search_pangu(query, system, module, category, top_k)


# ---- Context：获取完整上下文 ----
@_tool()
def get_knowledge(doc_id: int) -> str:
    """按 doc_id 读取完整认知库知识；只读，返回正文或可读错误。"""
    return kb.get_knowledge(doc_id)


@_tool()
def get_sql_template(template_id: int) -> str:
    """按 template_id 读取 SQL 模板及风险/验证信息；只读，模板 SQL 不会被执行。"""
    return kb.get_sql_template(template_id)


@_tool()
def get_table(table_name: str, db_name: str = "") -> str:
    """按表名读取目录元数据；只读，不替代实时 DDL，返回结果或可读错误。"""
    return kb.get_table(table_name, db_name)


@_tool()
def get_table_relations(table_name: str) -> str:
    """读取表关联元数据；只读，关系须再用 Archery 核验字段与 JOIN，返回结果或可读错误。"""
    return kb.get_table_relations(table_name)


# ---- Composite：组合诊断 ----
@_tool()
def diagnose_context(query: str, system: str = "", module: str = "", limit: int = 3) -> str:
    """汇集知识、模板、表和关系的组合诊断上下文；只读，不查询实时后端或执行修复 SQL。"""
    return kb.diagnose_context(query, system, module, limit)


# ---- Action：写权限（谨慎暴露；默认需显式确认/去重） ----
@_tool()
def save_knowledge(
    title: str,
    content_md: str,
    knowledge_type: str = "business",
    system: str = "",
    module: str = "",
    summary: str = "",
    core_tables: str = "",
    related_template_ids: str = "",
    tags: str = "",
    status: str = "draft",
    source_type: str = "manual",
    created_by: str = "",
    skip_dup_check: bool = False,
) -> str:
    """写入认知库知识；只写元数据，必须确认内容，不修改业务数据库。"""
    return kb.save_knowledge(
        title, content_md, knowledge_type, system, module, summary,
        core_tables, related_template_ids, tags, status, source_type, created_by, skip_dup_check,
    )


@_tool()
def update_knowledge(
    doc_id: int,
    title: str = "",
    content_md: str = "",
    knowledge_type: str = "",
    system: str = "",
    module: str = "",
    summary: str = "",
    core_tables: str = "",
    related_template_ids: str = "",
    tags: str = "",
    status: str = "",
    source_type: str = "",
) -> str:
    """更新认知库知识；只写元数据，必须确认目标与变更，不修改业务数据库。"""
    return kb.update_knowledge(
        doc_id, title, content_md, knowledge_type, system, module, summary,
        core_tables, related_template_ids, tags, status, source_type,
    )


@_tool()
def delete_knowledge(doc_id: int) -> str:
    """删除认知库知识；破坏性元数据写入，必须明确确认，不影响业务数据库。"""
    return kb.delete_knowledge(doc_id)


@_tool()
def save_sql_template(
    title: str,
    category: str,
    scenario: str,
    sql_text: str,
    keywords: str = "",
    core_tables: str = "",
    verified: bool = False,
    template_no: str = "",
    system: str = "",
    status: str = "draft",
    risk_level: str = "LOW",
    business_domain: str = "",
    source_type: str = "generated",
    parameters: str = "",
    execution_policy: str = "",
    execution_flow: str = "",
    example_case: str = "",
    problem_description: str = "",
    symptom: str = "",
    root_cause: str = "",
    preconditions: str = "",
    diagnosis_steps: str = "",
    verify_sql: str = "",
    rollback_sql: str = "",
    created_by: str = "",
    skip_dup_check: bool = False,
) -> str:
    """写入 SQL/修复模板元数据；必须确认内容，不执行模板 SQL。"""
    return kb.save_sql_template(
        title, category, scenario, sql_text, keywords, core_tables, verified, template_no,
        system, status, risk_level, business_domain, source_type, parameters,
        execution_policy, execution_flow, example_case, problem_description,
        symptom, root_cause, preconditions, diagnosis_steps, verify_sql,
        rollback_sql, created_by, skip_dup_check,
    )


@_tool()
def list_sql_templates(
    category: str = "",
    system: str = "",
    business_domain: str = "",
    verified_only: bool = False,
    limit: int = 50,
) -> str:
    """列出 SQL 模板元数据；只读，返回结果或可读错误。"""
    return kb.list_sql_templates(category, system, business_domain, verified_only, limit)


@_tool()
def update_sql_template(
    template_id: int,
    title: str = "",
    scenario: str = "",
    sql_text: str = "",
    category: str = "",
    system: str = "",
    status: str = "",
    risk_level: str = "",
    business_domain: str = "",
    keywords: str = "",
    core_tables: str = "",
    template_no: str = "",
    parameters: str = "",
    execution_policy: str = "",
    execution_flow: str = "",
    example_case: str = "",
    problem_description: str = "",
    symptom: str = "",
    root_cause: str = "",
    preconditions: str = "",
    diagnosis_steps: str = "",
    verify_sql: str = "",
    rollback_sql: str = "",
    source_type: str = "",
    verified: bool = False,
) -> str:
    """更新 SQL 模板元数据；必须确认目标与变更，不执行模板 SQL。"""
    return kb.update_sql_template(
        template_id, title, scenario, sql_text, category, system, status, risk_level,
        business_domain, keywords, core_tables, template_no, parameters,
        execution_policy, execution_flow, example_case, problem_description,
        symptom, root_cause, preconditions, diagnosis_steps, verify_sql,
        rollback_sql, source_type, verified,
    )


@_tool()
def delete_sql_template(template_id: int) -> str:
    """删除 SQL 模板元数据；破坏性写入，必须明确确认，不影响业务数据库。"""
    return kb.delete_sql_template(template_id)


@_tool()
def record_template_usage(template_id: int) -> str:
    """记录 SQL 模板使用统计；仅写认知层元数据，不执行模板 SQL。"""
    return kb.record_template_usage(template_id)


@_tool()
def add_table_relation(
    from_table: str,
    to_table: str,
    join_on: str,
    relation_type: str = "ref",
    description: str = "",
    confidence: float = 1.0,
    from_db: str = "srm",
    to_db: str = "srm",
    verified: bool = False,
    source: str = "manual",
) -> str:
    """写入或更新表关联元数据；需已有 Archery/SELECT 证据并确认，不创建数据库外键。"""
    return kb.add_table_relation(
        from_table, to_table, join_on, relation_type, description,
        confidence, from_db, to_db, verified=verified, source=source,
    )


@_tool()
def record_table_usage(table_names: str) -> str:
    """记录表目录使用统计；仅写认知层元数据，不修改表或业务数据。"""
    return kb.record_table_usage(table_names)


@_tool()
def upsert_table_knowledge(
    table_name: str, description: str = "", tags: str = "", db_name: str = "",
) -> str:
    """修正或补录表目录元数据；至少提供 description、tags、db_name 之一并确认，不替代实时 DDL。"""
    if not any(value.strip() for value in (description, tags, db_name)):
        raise ValueError("description、tags、db_name 至少提供一项非空值")
    return kb.upsert_table_knowledge(table_name, description, tags, db_name)


_advertise_nonempty_any_of(
    "upsert_table_knowledge", "description", "tags", "db_name"
)


# ============================================================================
# es_* 正式环境 ES 只读查询（整合自 es-prod）
# 铁律：严禁任何写操作；单次查询最多 ES_MAX_SIZE（默认 100）条。
# 未配置 ES_BASE_URL 时返回 es_unconfigured，提示按 .env.example 补齐。
# ============================================================================

@_tool()
def es_search(index: str, dsl: str = "", size: int = 10, env: str = "prod") -> str:
    """在指定环境执行 ES 只读 _search；禁止写入且单次最多返回 ES_MAX_SIZE 条，返回 JSON ok 或 error.retryable。"""
    try:
        client = es.get_client(env)
    except es.ESSafetyError as e:
        return _err("es_unconfigured", str(e), retryable=False)
    try:
        info = client.search(index, dsl, size)
    except es.ESSafetyError as e:
        return _err("es_search", str(e), retryable=True)
    return _ok({"index": index, "env": env, **info}, "elasticsearch")


@_tool()
def es_count(index: str, dsl: str = "", env: str = "prod") -> str:
    """统计指定环境 ES 文档数；只读 _count，不受返回条数上限影响，返回 JSON ok 或 error.retryable。"""
    try:
        client = es.get_client(env)
    except es.ESSafetyError as e:
        return _err("es_unconfigured", str(e), retryable=False)
    try:
        info = client.count(index, dsl)
    except es.ESSafetyError as e:
        return _err("es_count", str(e), retryable=True)
    if info.get("error"):
        return _err("es_count", f"ES error: {info['error']}", retryable=False)
    return _ok({"index": index, "env": env, "count": info.get("count")}, "elasticsearch")


@_tool()
def es_get(index: str, doc_id: str, env: str = "prod") -> str:
    """读取指定环境 ES 单个文档；只读 _source，返回 JSON ok 或 error.retryable。"""
    try:
        client = es.get_client(env)
    except es.ESSafetyError as e:
        return _err("es_unconfigured", str(e), retryable=False)
    try:
        info = client.get(index, doc_id)
    except es.ESSafetyError as e:
        return _err("es_get", str(e), retryable=True)
    if not info.get("found"):
        return _ok({"found": False, "index": index, "env": env, "_id": doc_id}, "elasticsearch")
    return _ok(
        {"found": True, "index": index, "env": env, "_id": doc_id, "_source": info.get("_source")},
        "elasticsearch",
    )



@_tool()
def get_workflow_guide(
    topic: Literal["overview", "requirement", "triage", "repair", "knowledge", "handoff", "capabilities"] = "overview",
) -> str:
    """读取本地协作协议或工具能力清单；只读，不探测后端健康，返回 JSON ok 或 error.retryable。"""
    if topic == "capabilities":
        return _ok({
            "contract_version": 1,
            "backend_health_checked": False,
            "tools": [
                {"name": tool.name, **tool.annotations.model_dump(exclude_none=True)}
                for tool in sorted(mcp._tool_manager.list_tools(), key=lambda item: item.name)
            ],
        }, "workflow-contract")
    try:
        content = workflow.get_guide(topic)
    except ValueError as exc:
        return _err("workflow_topic", str(exc))
    return _ok({"contract_version": 1, "topic": topic, "content": content}, "workflow-contract")


def main() -> None:
    mcp.run()
