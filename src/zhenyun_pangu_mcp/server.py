"""zhenyun-pangu-mcp — 甄云盘古通用工具 MCP（完全自包含，无外部仓库依赖）。

工具按前缀分组：
  - obs_*       日志查询（阿里云 SLS：国内公有云盘古 prod/dev/test；Loki：仅 AWS 海外）
  - archery_*   数据库查询（Archery 双站点 cn/aws + 盘古专属租户/实例/库列表）
  - *_adapter_script* 适配器脚本发现、服务端解码、局部读取与正文搜索
  - choerodon_* 猪齿鱼协作（内置 Python 客户端，OAuth 账号密码登录）
  - search_repo 跨仓代码搜索（内置纯标准库文件遍历，零外部依赖）
  - gitlab_*    已知 GitLab 项目/分支/路径的精确读取（搜索默认禁用）
  - search/get/save_* 认知层知识、SQL 模板、表目录和关联关系检索/维护

工具选择原则：先用认知层工具发现稳定规则、历史方案和候选表，再用日志/Archery/
GitLab/猪齿鱼获取当前事实；认知层写工具只沉淀用户确认后的元数据，不执行业务写 SQL。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone

import requests
from mcp.server.fastmcp import FastMCP

from . import adapter_scripts, archery, choerodon, loki, search, sls, sls_config, gitlab
from .config import (
    ARCHERY_INSTANCE_ALIASES,
    ARCHERY_DEFAULT_DB,
    GITLAB_SEARCH_ENABLED,
    LOKI_PLATFORMS,
)
from .knowledge_base import service as kb

mcp = FastMCP("zhenyun-pangu-mcp")
BJ = timezone(timedelta(hours=8))
MAX_LOG_QUERY_SPAN = 31 * 24 * 3600


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
_SOURCE_MAP = {
    "obs_log_query": "loki",
    "obs_log_trace": "loki",
    "obs_log_datasources": "loki",
    "obs_sls_query": "sls",
    "obs_sls_targets": "sls",
    "archery_query": "archery",
    "archery_describe_table": "archery",
    "archery_list_columns": "archery",
    "archery_query_tenant": "archery",
    "archery_list_databases": "archery",
    "archery_list_instances": "archery",
    "search_adapter_scripts": "adapter-script",
    "get_adapter_script_info": "adapter-script",
    "get_adapter_script_source": "adapter-script",
    "search_adapter_script_source": "adapter-script",
    "search_repo": "local-repo",
    "gitlab_search_projects": "gitlab",
    "gitlab_search_code": "gitlab",
    "gitlab_get_file": "gitlab",
    "gitlab_list_tree": "gitlab",
    "gitlab_list_branches": "gitlab",
    "search_knowledge": "knowledge-base",
    "search_sql_templates": "knowledge-base",
    "search_tables": "knowledge-base",
    "search_pangu": "knowledge-base",
    "get_knowledge": "knowledge-base",
    "get_sql_template": "knowledge-base",
    "get_table": "knowledge-base",
    "get_table_relations": "knowledge-base",
    "diagnose_context": "knowledge-base",
    "list_sql_templates": "knowledge-base",
    "save_knowledge": "knowledge-base",
    "save_sql_template": "knowledge-base",
    "update_sql_template": "knowledge-base",
    "delete_sql_template": "knowledge-base",
    "record_template_usage": "knowledge-base",
    "add_table_relation": "knowledge-base",
    "record_table_usage": "knowledge-base",
    "upsert_table_knowledge": "knowledge-base",
    "choerodon_query_issue": "choerodon",
    "choerodon_list_issue": "choerodon",
    "choerodon_search_users": "choerodon",
    "choerodon_get_status_map": "choerodon",
    "choerodon_search_tasks_by_person": "choerodon",
    "choerodon_list_attachments": "choerodon",
    "choerodon_download_attachment": "choerodon",
    "choerodon_list_comments": "choerodon",
    "choerodon_add_comment": "choerodon",
}


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

@mcp.tool()
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
    """查询 Loki 日志（仅 AWS 海外 jp-saas-1；国内盘古请改用 obs_sls_query）。

    适用范围：region=aws（AWS 海外 jp-saas-1，prod/nonprod/ops 全环境）。
    ⚠️ 国内公有云(cn)盘古日志（prod/dev/test）已迁回阿里云 SLS，本工具不再支持
    region=cn；查国内盘古请用 obs_sls_query(environment="prod"/"dev"/"test")。

    query 为 LogQL 表达式，如 '{app="srm-gateway"} |= "403"'。
    time_range 支持 30m/2h/1d、今天/昨天/本周 或 "YYYY-MM-DD HH:mm~HH:mm"（北京时间）。

    注：MCP 直接调 Loki HTTP API，query 即 LogQL 字符串；与「用户手册」在
    Grafana 页面手工选 namespace/app 缩小范围不同，这里必须在 query 中显式写
    标签过滤（如 {namespace="..."}），否则将扫描全部数据流（见 warning）。
    """
    region_error = _check_loki_region(region)
    if region_error:
        return region_error
    if not query or not query.strip():
        return _err("bad_param", "query 不能为空，必须传入 LogQL 表达式，如 '{app=\"srm-gateway\"} |= \"xxx\"'")
    try:
        ds_name = loki.resolve_datasource(region, env)
    except loki.LokiError as e:
        return _err("config", str(e), retryable=False)
    client = loki._get_client(region)
    try:
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


@mcp.tool()
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
    """按 traceId 查整条调用链日志（Loki，仅 AWS 海外；国内盘古用 obs_sls_query）。

    ⚠️ 国内公有云(cn)盘古日志（prod/dev/test）已迁回阿里云 SLS，本工具不再支持
    region=cn；查国内盘古链路请用 obs_sls_query(trace_id=..., environment=...)。

    推荐优先用本工具替代 obs_log_query+手写 query 来追链路，自动处理：
    按正文子串匹配 traceId（覆盖 `[xxx]` / `traceId=xxx` / `trace_id: xxx`，
    不写死字段前缀），带 namespace 限定取全链路按时间排序还原调用链；带 ns 查
    为 0 时自动降级为不限 namespace 重查一次。ERROR/WARN 行已包含在结果中，
    meta.error_count / warn_count 给出数量，无需单独再查。
    防 TOKEN 膨胀：
    - level：all（默认，全量）/ error（仅 ERROR 级）/ warn（WARN+ERROR 级）。
      排障时建议先用 level=error 或 level=warn 只取异常行，能省 80%+ token。
    - clip_len：每条日志行内容截断长度（默认 600，可调小到 200 更省 token）。
    - meta.truncated：命中行数达到 limit 时为 true，提示结果可能被截断，可调大 limit。
    默认 region=aws, env=nonprod；env 取值以 obs_log_datasources(region) 返回的
    真实数据源键为准（aws 下为 prod/nonprod/ops）。
    direction：BACKWARD（默认，从最近往回，先看最新）| FORWARD（从最早开始）。
    """
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


@mcp.tool()
def obs_log_datasources(region: str = "aws") -> str:
    """列出指定日志平台的 Loki 数据源（只读，仅 AWS 海外）。

    何时调用：不知道 ``env`` 对应的数据源名称，或需要确认 aws 平台连通性时；
    返回真实 datasource 名称，不需要手工猜测或把 Grafana 页面名称写进 LogQL。
    ⚠️ 国内公有云盘古日志已迁回阿里云 SLS（prod/dev/test 全覆盖），无 cn 数据源。
    """
    region_error = _check_loki_region(region)
    if region_error:
        return region_error
    client = loki._get_client(region)
    try:
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

@mcp.tool()
def archery_query(
    sql: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
    limit: int = 100,
) -> str:
    """执行 SQL 查询（只读）。

    site=cn 国内 / aws 日本云。instance 可用别名：prod/prod-ro/dev/test。
    db 默认 srm。用户 SQL 仅允许单条基础 SELECT、EXPLAIN SELECT 或 SHOW CREATE TABLE；
    不支持其它 SHOW/DESC、WITH、多语句、注释、函数/子查询、窗口函数、集合运算或任何写入语法。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.query(sql, instance_name, db_name, max(1, min(int(limit), 5000)))
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@mcp.tool()
def archery_describe_table(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """获取当前数据库表结构（只读，底层为 SHOW CREATE TABLE）。

    何时调用：表名已确定但字段、类型、索引或注释不确定时；生成查询/修复 SQL
    前优先使用本工具确认实时 DDL。它查询真实数据库，不依赖知识库目录。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.describe_table(instance_name, db_name, table)
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@mcp.tool()
def archery_list_columns(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """获取当前数据库表的字段名列表（只读）。

    何时调用：只需快速校验字段是否存在，或生成 WHERE/JOIN/修复 SQL 前核对拼写时；
    需要完整类型、索引和注释时改用 archery_describe_table。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        columns = client.list_columns(instance_name, db_name, table)
        return _ok({"site": site, "instance": instance_name, "db": db_name, "table": table, "columns": columns}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@mcp.tool()
def archery_query_tenant(
    tenant: str = "",
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """查询租户信息（hpfm_tenant）。tenant 为空时列出前 100 个租户。

    盘古专属能力：多租户查询。
    site=cn 国内 / aws 日本云(JP-SaaS-1)。instance 可用别名：
    cn: prod/prod-ro/dev/test；aws: aws(=aws-prod, 正式环境 JP-SaaS-1-Prod-RW-8.0)。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        result = archery.query_tenant(site, tenant or None, instance_name, db_name)
        return _ok({"site": site, "instance": instance_name, "db": db_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@mcp.tool()
def archery_list_databases(
    site: str = "cn",
    instance: str | None = None,
) -> str:
    """列出指定 Archery 实例下的数据库（只读）。

    何时调用：不确定使用 ``srm``、``srm_logistics_delivery`` 等库，或跨库查询前；
    这是固定的内部发现能力，不等于 archery_query 对用户开放了任意 SHOW 语句。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        result = archery.query_db_list(site, instance_name)
        return _ok({"site": site, "instance": instance_name, **result}, "archery")
    except archery.ArcheryError as e:
        return _err("archery_query", str(e), retryable=True)


@mcp.tool()
def archery_list_instances() -> str:
    """列出 Archery 实例别名映射（短名 -> 真实实例名，按站点分组）。

    返回结构明确标注每个别名归属的 site（cn/aws），调用方据此显式传 site，
    避免「用 cn 站点查 aws 实例」导致的「未关联该实例」歧义错误。
    """
    return _ok({
        "instances_by_site": ARCHERY_INSTANCE_ALIASES,
        "default_site": "cn",
        "default_db": ARCHERY_DEFAULT_DB,
        "note": "查询实例时须同时传对应 site（如 aws 实例传 site=\"aws\"），"
                "仅传 instance 而不传 site 会按默认 site=cn 解析而报「未关联该实例」。",
    }, "archery")


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


@mcp.tool()
def choerodon_query_issue(issue_id: str, project_id: str = "") -> str:
    """查询猪齿鱼单个任务/缺陷详情（含附件列表）。

    issue_id 为工单加密 ID（来自列表结果）；project_id 可选,默认用 CHOERODON_PROJECT_ID。
    返回 issueNum/完整编号 fullIssueNum(如 prod-bug-213849)/租户编码 tenantCode/项目编码 projectCode/
    summary/状态/优先级/类型/创建人/描述(HTML)/附件。
    """
    return _choerodon_call("query_issue", issue_id=issue_id, project_id=project_id or None)


@mcp.tool()
def choerodon_list_issue(
    keyword: str = "",
    assignee: str = "",
    status: str = "",
    size: int = 20,
    project_id: str = "",
) -> str:
    """条件查询猪齿鱼任务列表。

    keyword 为概要模糊搜索；assignee 为经办人姓名（自动解析成员）；
    status 为状态名（自动解析状态 id）。返回任务摘要列表。
    """
    return _choerodon_call(
        "list_issue", keyword=keyword, assignee=assignee, status=status,
        size=size, project_id=project_id or None,
    )


@mcp.tool()
def choerodon_search_users(name: str, size: int = 50, project_id: str = "") -> str:
    """按姓名/登录名搜索猪齿鱼项目成员（只读）。

    何时调用：按经办人筛选任务前确认真实成员，或需要把用户输入转换为猪齿鱼成员
    id 时；返回的真实 id/姓名再交给任务查询工具，不要自行编造 id。
    """
    return _choerodon_call("search_users", name=name, size=size, project_id=project_id or None)


@mcp.tool()
def choerodon_get_status_map(project_id: str = "") -> str:
    """获取猪齿鱼项目状态映射（状态名 -> 加密 id），供列表过滤用。"""
    return _choerodon_call("get_status_map", project_id=project_id or None)


@mcp.tool()
def choerodon_search_tasks_by_person(name: str, size: int = 50, project_id: str = "") -> str:
    """按经办人姓名搜索其负责的猪齿鱼任务（先查成员再按经办人过滤）。"""
    return _choerodon_call("search_tasks_by_person", name=name, size=size, project_id=project_id or None)


@mcp.tool()
def choerodon_list_attachments(issue_id: str, project_id: str = "") -> str:
    """查看猪齿鱼任务的附件列表（文件名 + URL）。"""
    return _choerodon_call("list_attachments", issue_id=issue_id, project_id=project_id or None)


@mcp.tool()
def choerodon_download_attachment(file_url: str) -> str:
    """通过猪齿鱼 hfle 接口获取附件签名下载地址（只读）。

    何时调用：用户明确要求下载某个附件时；必须先用 choerodon_list_attachments
    获得真实 ``file_url``，本工具不接受凭空构造的 attachment id，也不会把文件内容
    上传或写回猪齿鱼。
    """
    return _choerodon_call("download_attachment", file_url=file_url)


@mcp.tool()
def choerodon_list_comments(issue_id: str, size: int = 100, project_id: str = "") -> str:
    """查询猪齿鱼任务的评论列表（只读）。

    issue_id 为工单加密 ID（与 choerodon_query_issue 一致）。
    返回每条评论的作者/登录名/内容/更新时间。写评论前先看现状。
    """
    return _choerodon_call("list_comments", issue_id=issue_id, size=size, project_id=project_id or None)


@mcp.tool()
def choerodon_add_comment(issue_id: str, comment: str, project_id: str = "") -> str:
    """为猪齿鱼任务新增评论（写操作，有副作用）。

    issue_id 为工单加密 ID；comment 必须是规范 Markdown（标题/列表/引用/代码块/
    加粗/行内代码等），不接受纯文本或原始 HTML；工具会将 Markdown 渲染为评论区 HTML。
    ⚠️ 写操作：会真实写入猪齿鱼，调用前必须向用户确认评论内容无误。
    建议先调用 choerodon_list_comments 查看现状，再执行写入。
    """
    return _choerodon_call("create_comment", issue_id=issue_id, comment=comment, project_id=project_id or None)


# ============================================================================
# search_repo 跨仓搜索（内置纯标准库文件遍历,无外部脚本依赖）
# ============================================================================

@mcp.tool()
def search_repo(
    keyword: str,
    mode: str = "content",
    max_results: int = 30,
    context: int = 2,
    depth: int = 4,
) -> str:
    """跨本地代码仓库搜索（内容 / 文件名 / 模块结构）。

    mode: content(内容搜索,返回命中行与上下文) / filename(文件名模糊匹配) / modules(列出服务-模块-层结构)。
    扫描根目录由 PG_ROOT 指定(默认本仓库根)。max_results 限制命中数量,
    context 为内容搜索上下文行数,depth 为递归深度。
    """
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

@mcp.tool()
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
    """检索租户二开、适配器和外部接口脚本元信息（只读，不返回脚本正文）。

    二开、客户定制、ERP/WMS/OA 对接、回调、推送、同步、报文或字段映射问题，
    应优先调用本工具，而不是只搜索本地 Java。tenant/running_service/query 至少提供一项；
    ``query`` 匹配 task_code/description。命中 ``script_id`` 后先按需调用
    search_adapter_script_source，再局部读取 get_adapter_script_source。
    """
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


@mcp.tool()
def get_adapter_script_info(
    script_id: int,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取适配器脚本轻量元信息（只读，不读取或返回 Base64 正文）。

    返回租户、运行服务、task_code、版本、优先级和缓存状态。只有源码已在缓存中
    时才附带 decoded size/hash，避免为了 info 无条件读取完整脚本。
    """
    try:
        return _ok(adapter_scripts.service.get_info(
            script_id, site=site, instance=instance, db=db,
        ), "adapter-script")
    except archery.ArcheryError as e:
        return _err("adapter_script_query", str(e), retryable=True)
    except adapter_scripts.AdapterScriptError as e:
        return _err("adapter_script", str(e), retryable=False)


@mcp.tool()
def get_adapter_script_source(
    script_id: int,
    start_line: int = 1,
    end_line: int = 0,
    full: bool = False,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """读取服务端已解码的 JavaScript 源码（只读，永不返回 Base64）。

    默认从 start_line 起返回 200 行，单次局部读取最多 500 行；同时传 start/end
    可精确读取区间。只有确实需要全局分析时才设置 ``full=true``，定位字段、函数、
    API 或错误时应先调用 search_adapter_script_source。
    """
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


@mcp.tool()
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
    """在服务端解码后的 JavaScript 中搜索并返回少量上下文（只读）。

    适合定位字段、函数、接口地址、回调、报文映射或异常文本。默认按普通字符串、
    不区分大小写搜索；除非确有需要，不要启用 regex。搜索结果只包含匹配区间，
    不返回 Base64，也不默认返回完整脚本。
    """
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
# gitlab_* 代码平台（GitLab 仓库：项目/代码/文件/目录/分支，整合自 gitlab-code-mcp）
# ============================================================================

@mcp.tool()
def gitlab_search_projects(query: str, per_page: int = 20) -> str:
    """搜索 GitLab 项目（默认禁用；仅平台明确启用搜索后注册）。

    何时调用：不知道仓库的 project_id/path，或需要先确认标准库与二开库归属时；
    返回项目 id、完整路径、默认分支和网页地址，后续交给其它 gitlab_* 工具。
    """
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


@mcp.tool()
def gitlab_search_code(query: str, per_page: int = 20) -> str:
    """GitLab 代码搜索（默认禁用；仅平台明确启用搜索后注册）。

    何时调用：知道类名、方法名、错误文本或配置键但不知道文件位置时；返回命中
    项目、路径、分支和行号，随后用 gitlab_get_file 读取完整文件核对上下文。
    """
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


@mcp.tool()
def gitlab_get_file(project_id: str, path: str, ref: str = "master") -> str:
    """读取 GitLab 仓库指定分支/引用下的完整文件（只读）。

    何时调用：用户/可靠证据已给出精确位置，或 gitlab_list_tree 已在已知项目内定位
    文件后，需要完整源码、配置或版本上下文时；``project_id``、``path``、``ref``
    必须来自真实证据，不能通过枚举模拟当前禁用的 GitLab 搜索。
    """
    try:
        content = gitlab.GitLabClient().get_file(project_id, path, ref=ref)
        return _ok({"project_id": project_id, "path": path, "ref": ref, "content": content}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


@mcp.tool()
def gitlab_list_tree(
    project_id: str,
    path: str = "",
    ref: str = "master",
    recursive: bool = False,
    per_page: int = 100,
) -> str:
    """列出 GitLab 仓库目录树（只读）。

    何时调用：已知仓库但不知道模块/文件路径，或需要确认某个 ref 下的目录结构时；
    找到目标文件后再用 gitlab_get_file，``recursive`` 用于控制扫描范围。
    """
    try:
        items = gitlab.GitLabClient().list_tree(
            project_id, path=path, ref=ref, recursive=recursive, per_page=per_page
        )
        return _ok({"count": len(items), "tree": items}, "gitlab")
    except gitlab.GitLabError as e:
        return _err("gitlab", str(e), retryable=True)


@mcp.tool()
def gitlab_list_branches(project_id: str, per_page: int = 50) -> str:
    """列出 GitLab 仓库分支及保护状态（只读）。

    何时调用：读取源码前需要选择 hotfix/release/master 等真实 ref，或需要确认
    默认/保护分支时；不要直接猜测仓库分支名。
    """
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


@mcp.tool()
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
) -> str:
    """查询国内公有云（cn）阿里云 SLS 日志：盘古 prod + 非生产 dev/test 全覆盖。

    环境路由（system="盘古"，由 MCP 完成 project/logstore/namespace 映射，调用方不接触 AK）：
      - prod → pangu-cn-saas-3-prod-shared-sls-project-0 / saas-prod
      - dev  → pangu-cn-saas-3-nonprod-shared-sls-project-0 / saas-dev-new
      - test → pangu-cn-saas-3-nonprod-shared-sls-project-0 / saas-test-new
    盘古非生产(dev/test)曾短暂迁移到 Loki，现已迁回阿里云 SLS，统一用本工具；
    obs_log_*（Loki）只保留 AWS 海外(jp-saas-1)。

    用法：
      - trace_id：按 traceId 做「ERROR/WARN + 全链路」两阶段查询（传了则优先于 keyword）。
      - keyword：SLS 查询子句（与 _namespace_ 过滤 AND 组合），如 'content: "订单不存在"'。
      - level：默认 ERROR；传空字符串表示不过滤级别。
      - time_range：最近30分钟/最近2小时/最近3天、今天/昨天/前天/本周/上周/本月/上月，
        或 30m/2h/1d，或 "YYYY-MM-DD HH:mm~HH:mm"（北京时间）。
      - auto_expand：未显式指定时间窗且 0 命中时，自动扩到最近 24h、72h 各重试一次，
        实际尝试过的窗口在 meta.attempted_windows 中返回（SLS 时间对齐偏差大时很有用）。
      - clip_len：每条日志字段截断长度（默认 2000，0 表示不裁剪）。
    """
    try:
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
                    sls_config.endpoint(), limit,
                )
                query_used = f'"{trace_id}" AND _namespace_: {target.namespace}'
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


@mcp.tool()
def obs_sls_targets() -> str:
    """列出阿里云 SLS 支持的系统/环境映射（只读，不含凭据）。

    何时调用：不确定国内盘古到底支持哪些 ``environment``（prod/dev/test）时；
    返回 system/environment → project/logstore/namespace 的真实映射，
    避免凭空猜测环境名。AWS 海外日志不走 SLS，请用 obs_log_datasources(region="aws")。
    """
    return _ok({
        "note": "国内公有云盘古 prod 与非生产 dev/test 均在阿里云 SLS；Loki(obs_log_*)仅 AWS 海外。",
        "targets": sls_config.supported_targets(),
    }, "sls")


# ============================================================================
# 知识库「认知层」能力（整合 self sql-template-mcp / knowledge-ops-mcp / table-catalog-mcp）
# 工具语义化分层：Discovery / Context / Composite / Action
# ============================================================================

# ---- Discovery：让 Agent 找东西 ----
@mcp.tool()
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
    """检索业务知识、系统机制和排查经验（只读）。

    何时调用：排障、生成 SQL 或解释字段/状态前，先查是否已有企业认知。
    ``query`` 为空时按过滤条件列出最近更新的知识；有关键词时默认混合
    语义向量和关键词检索。``knowledge_type`` 可用 business/system/technical/
    troubleshooting/data_model/configuration/experience/rule，``status`` 可用
    draft/verified/deprecated/archived；``verified_only=true`` 只返回 verified。
    ``limit`` 最大 50。结果中的 ``id`` 可交给 get_knowledge 获取完整正文。
    """
    return kb.search_knowledge(query, knowledge_type, system, module, status, verified_only, limit, use_semantic)


@mcp.tool()
def search_sql_templates(
    keyword: str = "",
    category: str = "",
    system: str = "",
    business_domain: str = "",
    verified_only: bool = False,
    limit: int = 10,
    use_semantic: bool = True,
) -> str:
    """检索可复用的 SQL/修复模板（只读，混合检索）。

    何时调用：生成查询或人工确认修复 SQL 前，先用业务关键词、表名或问题
    症状检索历史方案；优先筛选 ``verified_only=true``。可用过滤项为
    ``category``、``system``、``business_domain``；``keyword`` 为空时用于
    按过滤条件总览模板，``limit`` 最大 50。命中结果的 ``id`` 交给
    get_sql_template；复用完成后再调用 record_template_usage。
    """
    return kb.search_sql_templates(keyword, category, system, business_domain, verified_only, limit, use_semantic)


@mcp.tool()
def search_tables(
    query: str,
    domain: str = "",
    db_name: str = "",
    top_k: int = 5,
    use_semantic: bool = True,
) -> str:
    """按关键词/语义检索表目录（只读），返回候选表元数据。

    何时调用：不知道真实表名、需要从业务描述定位表时。``query`` 必填；
    ``domain``/``db_name`` 用于缩小范围，``top_k`` 最大 20。目录结果是
    候选和业务注释，不等同于当前数据库字段事实；字段存在性和完整 DDL
    必须再用 archery_describe_table/archery_list_columns 确认。
    """
    return kb.search_tables(query, domain, db_name, top_k, use_semantic)


@mcp.tool()
def search_pangu(query: str, system: str = "", module: str = "", category: str = "", top_k: int = 3) -> str:
    """统一快速发现：一次检索知识、模板、表及候选表关系（只读）。

    适合刚收到一个跨知识/数据域的问题时做第一轮定位；``system``、
    ``module``、``category`` 可缩小结果，``top_k`` 最大 5。它是关键词快速
    发现，不替代专项检索或实时 Archery 查询；拿到 id/表名后继续调用
    get_knowledge、get_sql_template、get_table 或 get_table_relations。
    """
    return kb.search_pangu(query, system, module, category, top_k)


# ---- Context：获取完整上下文 ----
@mcp.tool()
def get_knowledge(doc_id: int) -> str:
    """按 ``doc_id`` 获取单条知识的完整 Markdown 正文及元数据（只读）。

    先用 search_knowledge 命中 id，再调用本工具；适合需要引用完整规则、
    排查步骤或字段说明时使用。
    """
    return kb.get_knowledge(doc_id)


@mcp.tool()
def get_sql_template(template_id: int) -> str:
    """按 ``template_id`` 获取单条 SQL 模板及风险/验证元数据（只读）。

    先用 search_sql_templates 命中 id，再读取完整 SQL 和参数说明。模板中的
    UPDATE/DELETE/INSERT 仅表示供人工确认的方案，不代表本 MCP 会执行写入。
    """
    return kb.get_sql_template(template_id)


@mcp.tool()
def get_table(table_name: str, db_name: str = "") -> str:
    """按表名获取目录元数据详情（只读）。

    返回表注释、描述、关键字段和入口字段，适合 search_tables 命中后补全
    上下文；它不是实时 DDL，字段最终仍需 Archery 专用工具确认。
    ``db_name`` 为空时使用目录默认库（通常为 srm）。
    """
    return kb.get_table(table_name, db_name)


@mcp.tool()
def get_table_relations(table_name: str) -> str:
    """获取某张表已沉淀的关联关系（只读）。

    返回 from/to 表、join 条件、关系类型、描述、置信度（confidence）、
    是否已验证（verified）及来源（source）。用于设计 JOIN 或诊断数据链路。
    可信度指引：优先采信 ``verified=true`` 且 ``source=archery_select`` 的关系；
    未验证的关系仅作候选，执行前仍需用 archery_list_columns 确认两端字段存在，
    并用 SELECT 验证 join 结果。关系是知识库沉淀，不等于数据库约束。
    """
    return kb.get_table_relations(table_name)


# ---- Composite：组合诊断 ----
@mcp.tool()
def diagnose_context(query: str, system: str = "", module: str = "", limit: int = 3) -> str:
    """组合诊断：为一个问题汇集知识 → 模板 → 表 → 关系（只读）。

    何时调用：排障或复杂 SQL 任务尚未知道该查哪类资料时，作为第一轮
    上下文收集器；``system``/``module`` 可过滤知识，``limit`` 最大 5。
    结果用于确定下一步工具，不会查询实时日志/数据库，也不会自动生成或
    执行修复 SQL；随后按结果分别调用专项工具和 Archery。
    """
    return kb.diagnose_context(query, system, module, limit)


# ---- Action：写权限（谨慎暴露；默认需显式确认/去重） ----
@mcp.tool()
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
    """写入一条业务知识/排查经验（写操作，需用户确认内容）。

    ``content_md`` 必须是规范 Markdown；``core_tables``、``tags``、
    ``related_template_ids`` 传逗号分隔值（如 ``a,b``）。默认 ``status=draft``，
    只有经过事实核验后才设 verified；``knowledge_type`` 见 search_knowledge
    的枚举。适合沉淀本次确认过的稳定规则、排查结论或数据模型说明，不要把
    会变化的当前日志/数据写成知识。写入 knowledge_docs，不修改业务库。
    """
    return kb.save_knowledge(
        title, content_md, knowledge_type, system, module, summary,
        core_tables, related_template_ids, tags, status, source_type, created_by, skip_dup_check,
    )


@mcp.tool()
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
    """部分更新已有知识条目（写操作，需用户确认）。

    先用 get_knowledge 确认 ``doc_id``，只传需要修改的字段。适合修正
    正文/标题/归类、把核验过的知识标为 verified（verified_at 自动写入）、
    或将过时知识标记 deprecated/archived（优于直接删除）。修改正文等
    影响语义检索的字段时会自动重新生成 embedding。``core_tables``、
    ``tags``、``related_template_ids`` 传逗号分隔值。只写认知层元数据，
    不修改业务数据库。
    """
    return kb.update_knowledge(
        doc_id, title, content_md, knowledge_type, system, module, summary,
        core_tables, related_template_ids, tags, status, source_type,
    )


@mcp.tool()
def delete_knowledge(doc_id: int) -> str:
    """删除指定知识条目（破坏性写操作，必须用户明确确认）。

    仅用于清理错误、重复或已彻底作废的知识；删除前先 get_knowledge 核对
    id。若知识只是内容过时但仍有参考价值，建议改用 update_knowledge 置
    status=deprecated/archived 而非物理删除。本操作不影响业务数据库。
    """
    return kb.delete_knowledge(doc_id)


@mcp.tool()
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
    created_by: str = "",
    skip_dup_check: bool = False,
) -> str:
    """写入一条可复用 SQL/修复模板（写操作，需用户确认内容）。

    适合复杂查询或人工执行的修复方案在验证后沉淀；本工具只写模板库，
    不执行 ``sql_text``。``keywords``/``core_tables`` 传逗号分隔值，
    ``parameters`` 必须是 JSON 对象字符串（例如
    ``{"tenant_id":{"type":"bigint","required":true}}``）。
    ``status`` 可用 draft/verified/trusted/deprecated，``risk_level`` 可用
    LOW/MEDIUM/HIGH/CRITICAL；未核验模板保持 draft。
    """
    return kb.save_sql_template(
        title, category, scenario, sql_text, keywords, core_tables, verified, template_no,
        system, status, risk_level, business_domain, source_type, parameters,
        execution_policy, created_by, skip_dup_check,
    )


@mcp.tool()
def list_sql_templates(
    category: str = "",
    system: str = "",
    business_domain: str = "",
    verified_only: bool = False,
    limit: int = 50,
) -> str:
    """列出模板库总览（只读）。

    用于维护或不知道模板 id 时按分类、系统、业务域和 verified 状态浏览；
    ``limit`` 最大 200。只读查看不需要确认，变更请使用 update/delete 写工具。
    """
    return kb.list_sql_templates(category, system, business_domain, verified_only, limit)


@mcp.tool()
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
    parameters: str = "",
    execution_policy: str = "",
    source_type: str = "",
    verified: bool = False,
) -> str:
    """部分更新已有模板（写操作，需用户确认）。

    先用 get_sql_template 确认 ``template_id``；只传需要修改的字段。适合
    修正 SQL/分类/风险、补充参数或把已核验模板标为 verified。``parameters``
    仍须为 JSON 对象字符串；``verified=true`` 会将状态提升为 verified。
    不会执行模板 SQL。
    """
    return kb.update_sql_template(
        template_id, title, scenario, sql_text, category, system, status, risk_level,
        business_domain, keywords, core_tables, parameters, execution_policy,
        source_type, verified,
    )


@mcp.tool()
def delete_sql_template(template_id: int) -> str:
    """删除指定模板（破坏性写操作，必须用户明确确认）。

    仅用于清理错误、重复或已废弃模板；删除前先 get_sql_template 核对 id，
    本操作不影响业务数据库。
    """
    return kb.delete_sql_template(template_id)


@mcp.tool()
def record_template_usage(template_id: int) -> str:
    """记录一次模板复用（写入使用统计，不执行 SQL）。

    仅在模板确实被采用后调用，避免为了排序而虚增 usage_count。
    """
    return kb.record_template_usage(template_id)


@mcp.tool()
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
    """写入一条表关联关系（写操作，需用户确认，按键 upsert）。

    仅在 Archery/SELECT 验证两端字段和 join 结果后调用；``join_on`` 传可读
    的连接条件（如 ``a.order_id = b.order_id``），``confidence`` 范围 0~1，
    ``from_db``/``to_db`` 用实际库名。

    可信度属性：
      - ``verified=true``：已经 Archery/SELECT 实测验证过两端字段与 join 结果；
        仅实测通过才置 true，否则保持 false 让 SQL Agent 谨慎使用。
      - ``source``：来源枚举 archery_select(实测)/ddl(外键推断)/manual(人工)/inferred(自动推断)。
    ``join_on`` 传可读的连接条件（如 ``a.order_id = b.order_id``），该记录是
    知识库元数据，不创建数据库外键，也不执行 join。
    """
    return kb.add_table_relation(
        from_table, to_table, join_on, relation_type, description,
        confidence, from_db, to_db, verified=verified, source=source,
    )


@mcp.tool()
def record_table_usage(table_names: str) -> str:
    """记录本次实际使用过的表（写入目录使用统计）。

    ``table_names`` 传逗号分隔表名，例如 ``sodr_order,slod_asn``；仅在查询
    或诊断确实使用后调用，不修改表元数据和业务数据。
    """
    return kb.record_table_usage(table_names)


@mcp.tool()
def upsert_table_knowledge(
    table_name: str, description: str = "", tags: str = "", db_name: str = "",
) -> str:
    """修正或补录表目录描述/标签（写操作，需用户确认，按库名+表名 upsert）。

    何时调用：search_tables/get_table 未收录或描述过期，且已通过 Archery
    确认真实表后补录。至少提供 ``description``、``tags`` 或 ``db_name`` 之一；
    ``tags`` 传逗号分隔值。这里只写 table_catalog 元数据，不替代实时 DDL，
    不修改业务表。
    """
    return kb.upsert_table_knowledge(table_name, description, tags, db_name)


def main() -> None:
    mcp.run()
