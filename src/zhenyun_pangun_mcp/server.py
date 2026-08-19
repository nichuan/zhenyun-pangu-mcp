"""zhenyun-pangun-mcp — 甄云盘古通用工具 MCP（完全自包含，无外部仓库依赖）。

工具按前缀分组：
  - obs_*       日志查询（Loki 双平台 aws/cn + 阿里云 SLS 仅 cn 盘古 prod）
  - archery_*   数据库查询（Archery 双站点 cn/aws + 盘古专属租户/实例/库列表）
  - choerodon_* 猪齿鱼协作（内置 Python 客户端，OAuth 账号密码登录）
  - search_repo 跨仓代码搜索（内置纯标准库文件遍历，零外部依赖）
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import requests
from mcp.server.fastmcp import FastMCP

from . import archery, choerodon, loki, search, sls, sls_config
from .config import ARCHERY_INSTANCE_ALIASES, ARCHERY_DEFAULT_DB, LOKI_PLATFORMS

mcp = FastMCP("zhenyun-pangun-mcp")
BJ = timezone(timedelta(hours=8))


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


# ============================================================================
# 时间解析（北京时间）
# ============================================================================

def _time_bounds(from_time: int | None, to_time: int | None, time_range: str) -> tuple[int, int]:
    now = int(time.time())
    if from_time is not None or to_time is not None:
        end = int(to_time if to_time is not None else now)
        return int(from_time if from_time is not None else end - 7200), end

    text = (time_range or "2h").strip().lower()
    current = datetime.now(BJ)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if text == "today":
        return int(today.timestamp()), now
    if text == "yesterday":
        return int((today - timedelta(days=1)).timestamp()), int(today.timestamp() - 1)
    import re

    rel = re.match(r"^(\d+)(m|h|d)$", text)
    if rel:
        n = int(rel.group(1))
        seconds = {"m": 60, "h": 3600, "d": 86400}[rel.group(2)]
        return now - n * seconds, now
    # 绝对时间 "YYYY-MM-DD HH:mm~HH:mm"
    if "~" in text:
        parts = text.split("~")
        start = int(datetime.strptime(parts[0].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BJ).timestamp())
        end = int(datetime.strptime(parts[1].strip(), "%Y-%m-%d %H:%M").replace(tzinfo=BJ).timestamp())
        return start, end
    return now - 7200, now


def _timeout_hint(start: int, end: int) -> str:
    """Loki 查询失败/超时时的处置建议（基于时间窗宽窄给出可执行提示）。"""
    span = max(0, end - start)
    if span > 2 * 3600:
        return "（时间窗较宽导致查询慢/超时，请缩小时间范围后重试）"
    return "（查询失败，请确认 query 是否正确、env 是否来自 obs_log_datasources）"


# ============================================================================
# obs_* 日志工具
# ============================================================================

@mcp.tool()
def obs_log_query(
    region: str = "cn",
    env: str = "nonprod",
    query: str = "",
    time_range: str = "2h",
    from_time: int | None = None,
    to_time: int | None = None,
    limit: int = 50,
    direction: str = "BACKWARD",
) -> str:
    """查询日志（Loki）。

    区分两套平台：region=aws 查 AWS 海外(jp-saas-1)，region=cn 查国内公有云。
    国内非 prod 已切换至 Loki 风格，地址 logs.going-link.net。
    query 为 LogQL 表达式，如 '{app="srm-gateway"} |= "403"'。
    time_range 支持 30m/2h/1d/today/yesterday 或 "YYYY-MM-DD HH:mm~HH:mm"（北京时间）。

    注：MCP 直接调 Loki HTTP API，query 即 LogQL 字符串；与「用户手册」在
    Grafana 页面手工选 namespace/app 缩小范围不同，这里必须在 query 中显式写
    标签过滤（如 {namespace="saas-test-new"}），否则将扫描全部数据流（见 warning）。
    """
    if region not in LOKI_PLATFORMS:
        return _json({"error": f"未知 region: {region}（可选 {list(LOKI_PLATFORMS)}）"})
    if not query or not query.strip():
        return _json({"error": "query 不能为空，必须传入 LogQL 表达式，如 '{namespace=\"saas-test-new\"} |= \"xxx\"'"})
    ds_name = loki.resolve_datasource(region, env)
    client = loki._get_client(region)
    uid = client.resolve_uid(ds_name)

    warning = loki.warn_unscoped(query)

    start, end = _time_bounds(from_time, to_time, time_range)
    limit = max(1, min(int(limit), 5000))
    try:
        resp = client.loki_query_range(uid, query, start, end, limit, direction)
    except loki.LokiError as e:
        hint = _timeout_hint(start, end)
        return _json({"error": f"{e}{hint}"})

    if resp.get("status") != "success":
        return _json({"error": f"Loki 查询失败: {json.dumps(resp)[:500]}"})

    rows = []
    for stream in resp.get("data", {}).get("result", []):
        for ns, line in stream.get("values", []):
            rows.append({"ts": int(ns) // 1_000_000_000, "line": line})

    def fmt(sec: int) -> str:
        return datetime.fromtimestamp(sec, BJ).strftime("%Y-%m-%d %H:%M:%S")

    rows.sort(key=lambda r: r["ts"], reverse=(direction == "BACKWARD"))
    top = rows[:limit]
    return _json({
        "region": region,
        "env": env,
        "datasource": ds_name,
        "query": query,
        "start": fmt(start),
        "end": fmt(end),
        "total": len(rows),
        **({"warning": warning} if warning else {}),
        "results": [{"time": fmt(r["ts"]), "line": r["line"][:400]} for r in top],
    })


@mcp.tool()
def obs_log_trace(
    trace_id: str,
    region: str = "cn",
    env: str = "nonprod",
    time_range: str = "2h",
    from_time: int | None = None,
    to_time: int | None = None,
    limit: int = 200,
    direction: str = "BACKWARD",
    level: str = "all",
    clip_len: int = 600,
) -> str:
    """按 traceId 查整条调用链日志（Loki，单查询优先、最多 2 次 HTTP，根治超时）。

    推荐优先用本工具替代 obs_log_query+手写 query 来追链路，自动处理：
    按正文子串匹配 traceId（覆盖 `[xxx]` / `traceId=xxx` / `trace_id: xxx`，
    不写死字段前缀），带 namespace 限定取全链路按时间排序还原调用链；带 ns 查
    为 0 时自动降级为不限 namespace 重查一次。ERROR/WARN 行已包含在结果中，
    meta.error_count / warn_count 给出数量，无需单独再查。
    防 TOKEN 膨胀（本次新增）：
    - level：all（默认，全量）/ error（仅 ERROR 级）/ warn（WARN+ERROR 级）。
      排障时建议先用 level=error 或 level=warn 只取异常行，能省 80%+ token。
    - clip_len：每条日志行内容截断长度（默认 600，可调小到 200 更省 token）。
    - meta.truncated：命中行数达到 limit 时为 true，提示结果可能被截断，可调大 limit。
    默认 region=cn, env=nonprod（对应「test 环境」= namespace=saas-test-new）。
    注意：手册里「test 环境」在 API 层面对应 env=nonprod（LOKI_DATASOURCES 的 cn
    只有 prod/nonprod/ops），namespace 由 env 推导：nonprod->saas-test-new、prod->saas-prod。
    若实际 namespace 不同，请改用 obs_log_query 显式指定 {namespace="..."}。
    direction：BACKWARD（默认，从最近往回，先看最新）| FORWARD（从最早开始）。
    """
    if region not in LOKI_PLATFORMS:
        return _json({"error": f"未知 region: {region}（可选 {list(LOKI_PLATFORMS)}）"})
    try:
        loki.resolve_datasource(region, env)
    except loki.LokiError as e:
        return _json({"error": str(e)})

    start, end = _time_bounds(from_time, to_time, time_range)
    limit = max(1, min(int(limit), 5000))
    try:
        rows, meta = loki.query_trace(
            region, env, trace_id, start, end, limit, direction,
            level=level, clip_len=clip_len,
        )
    except loki.LokiError as e:
        return _json({"error": f"{e}{_timeout_hint(start, end)}"})

    def fmt(sec: int) -> str:
        return datetime.fromtimestamp(sec, BJ).strftime("%Y-%m-%d %H:%M:%S")

    return _json({
        **meta,
        "start": fmt(start),
        "end": fmt(end),
        "total": len(rows),
        "results": [{"time": fmt(r["ts_ns"] // 1_000_000_000), "line": r["line"]} for r in rows],
    })


@mcp.tool()
def obs_log_datasources(region: str) -> str:
    """列出指定日志平台的 Loki 数据源（用于确认环境名与数据源名映射）。"""
    if region not in LOKI_PLATFORMS:
        return _json({"error": f"未知 region: {region}（可选 {list(LOKI_PLATFORMS)}）"})
    client = loki._get_client(region)
    ds_list = client.discover_loki_datasources()
    return _json({
        "region": region,
        "label": LOKI_PLATFORMS[region]["label"],
        "count": len(ds_list),
        "datasources": [
            {"id": d.get("id"), "uid": d.get("uid"), "name": d.get("name"), "isDefault": d.get("isDefault")}
            for d in ds_list
        ],
    })


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
    db 默认 srm。仅允许 SELECT/SHOW/DESC/EXPLAIN/WITH 前缀。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.query(sql, instance_name, db_name, max(1, min(int(limit), 5000)))
        return _json({"site": site, "instance": instance_name, "db": db_name, **result})
    except archery.ArcheryError as e:
        return _json({"error": str(e)})


@mcp.tool()
def archery_describe_table(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """获取表结构（SHOW CREATE TABLE）。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        result = client.describe_table(instance_name, db_name, table)
        return _json({"site": site, "instance": instance_name, "db": db_name, **result})
    except archery.ArcheryError as e:
        return _json({"error": str(e)})


@mcp.tool()
def archery_list_columns(
    table: str,
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """获取表的字段列表。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        client = archery.ArcheryClient(site)
        columns = client.list_columns(instance_name, db_name, table)
        return _json({"site": site, "instance": instance_name, "db": db_name, "table": table, "columns": columns})
    except archery.ArcheryError as e:
        return _json({"error": str(e)})


@mcp.tool()
def archery_query_tenant(
    tenant: str = "",
    site: str = "cn",
    instance: str | None = None,
    db: str | None = None,
) -> str:
    """查询租户信息（hpfm_tenant）。tenant 为空时列出前 100 个租户。

    盘古专属能力：sql-ops-mcp 未覆盖的多租户查询。
    site=cn 国内 / aws 日本云(JP-SaaS-1)。instance 可用别名：
    cn: prod/prod-ro/dev/test；aws: aws(=aws-prod, 正式环境 JP-SaaS-1-Prod-RW-8.0)。
    """
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        db_name = db or ARCHERY_DEFAULT_DB
        result = archery.query_tenant(site, tenant or None, instance_name, db_name)
        return _json({"site": site, "instance": instance_name, "db": db_name, **result})
    except archery.ArcheryError as e:
        return _json({"error": str(e)})


@mcp.tool()
def archery_list_databases(
    site: str = "cn",
    instance: str | None = None,
) -> str:
    """列出实例下的数据库列表（SHOW DATABASES）。"""
    try:
        instance_name = archery.resolve_instance(instance, site, "SAAS-SRM-PROD数据库")
        result = archery.query_db_list(site, instance_name)
        return _json({"site": site, "instance": instance_name, **result})
    except archery.ArcheryError as e:
        return _json({"error": str(e)})


@mcp.tool()
def archery_list_instances() -> str:
    """列出 Archery 实例别名映射（短名 -> 真实实例名，按站点分组）。

    返回结构明确标注每个别名归属的 site（cn/aws），调用方据此显式传 site，
    避免「用 cn 站点查 aws 实例」导致的「未关联该实例」歧义错误。
    """
    return _json({
        "instances_by_site": ARCHERY_INSTANCE_ALIASES,
        "default_site": "cn",
        "default_db": ARCHERY_DEFAULT_DB,
        "note": "查询实例时须同时传对应 site（如 aws 实例传 site=\"aws\"），"
                "仅传 instance 而不传 site 会按默认 site=cn 解析而报「未关联该实例」。",
    })


# ============================================================================
# choerodon_* 猪齿鱼工具（内置 Python 客户端,无外部脚本依赖）
# ============================================================================

def _choerodon_call(dispatch_name: str, **kwargs) -> str:
    try:
        fn = choerodon.CHOERODON_DISPATCH[dispatch_name]
        return _json(fn(**kwargs))
    except Exception as e:  # 认证/网络/解析等一律优雅返回,不抛 500
        return _json({"error": f"{type(e).__name__}: {e}"})


@mcp.tool()
def choerodon_query_issue(issue_id: str, project_id: str = "") -> str:
    """查询猪齿鱼单个任务/缺陷详情（含附件列表）。

    issue_id 为工单加密 ID（来自列表结果）；project_id 可选,默认用 CHOERODON_PROJECT_ID。
    返回 summary/状态/优先级/类型/创建人/描述(HTML)/附件。
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
    """按姓名模糊搜索猪齿鱼项目成员（返回加密 id / 真实名 / 登录名）。"""
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
    """通过猪齿鱼 hfle 接口将附件 URL 解析为可下载的签名地址。"""
    return _choerodon_call("download_attachment", file_url=file_url)


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
        return _json(search.search_repo(
            keyword, mode=mode, max_results=max_results,
            context=context, depth=depth,
        ))
    except Exception as e:  # 文件系统错误等
        return _json({"error": str(e)})


# ============================================================================
# obs_sls_* 阿里云 SLS 日志（仅 cn 国内盘古 prod；非生产走 Loki）
# ============================================================================

def _time_bounds_sls(from_time: int | None, to_time: int | None, time_range: str) -> tuple[int, int]:
    """解析 SLS 时间范围,支持 from/to(秒时间戳)或自然语言(分钟/小时/天)。"""
    now = int(time.time())
    if from_time is not None or to_time is not None:
        end = int(to_time if to_time is not None else now)
        return int(from_time if from_time is not None else end - 7200), end
    text = (time_range or "最近2小时").strip().lower().replace(" ", "")
    current = datetime.now(BJ)
    today = current.replace(hour=0, minute=0, second=0, microsecond=0)
    if text in {"今天", "今日"}:
        return int(today.timestamp()), now
    if text == "昨天":
        return int((today - timedelta(days=1)).timestamp()), int(today.timestamp() - 1)
    for unit, seconds in (("分钟", 60), ("小时", 3600), ("天", 86400)):
        if unit in text:
            number = "".join(c for c in text.split(unit)[0] if c.isdigit())
            if number:
                return now - int(number) * seconds, now
    return now - 7200, now


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
) -> str:
    """查询阿里云 SLS 日志（仅 cn 国内盘古 prod 用阿里云日志；非生产请走 Loki）。

    仅 environment=prod 适用（project=pangu-cn-saas-3-prod-shared-sls-project-0）。
    盘古 dev/test 等非生产环境日志在 Loki，应使用 obs_log_query(region="cn")。
    传入 trace_id 做「ERROR/WARN + 全链路」两阶段查询;否则用 keyword,
    自动加上 _namespace_ 过滤。level 默认 ERROR(传空则不过滤级别)。
    """
    try:
        target = sls_config.resolve_target(system, environment)
        ak_id, ak_secret = sls_config.credentials(target)
        limit = max(1, min(int(limit), 500))
        if from_time or to_time:
            start, end = _time_bounds_sls(from_time or None, to_time or None, "")
        else:
            start, end = _time_bounds_sls(None, None, time_range)
        clauses = [f"_namespace_: {target.namespace}"]
        if level:
            clauses.append(f"level: {level}")
        if keyword:
            clauses.append(keyword)
        query = " AND ".join(clauses)
        if trace_id:
            logs, progress = sls.query_trace(
                target.project, target.logstore, ak_id, ak_secret,
                trace_id, target.namespace, start, end, sls_config.endpoint(), limit,
            )
            query_used = f'"{trace_id}" AND _namespace_: {target.namespace}'
        else:
            logs, progress = sls.query_sls(
                target.project, target.logstore, ak_id, ak_secret,
                query, start, end, sls_config.endpoint(), limit,
            )
            query_used = query
        return _json({
            "meta": {
                "system": target.system, "environment": target.environment,
                "project": target.project, "logstore": target.logstore,
                "namespace": target.namespace, "from_time": start, "to_time": end,
                "query": query_used, "progress": progress, "count": len(logs),
            },
            "logs": logs,
        })
    except (ValueError, RuntimeError) as e:
        return _json({"error": str(e)})


def main() -> None:
    mcp.run()
