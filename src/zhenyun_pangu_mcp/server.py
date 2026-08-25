"""zhenyun-pangu-mcp — 甄云盘古通用工具 MCP（完全自包含，无外部仓库依赖）。

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

from . import archery, choerodon, loki, search, sls, sls_config, gitlab
from .config import ARCHERY_INSTANCE_ALIASES, ARCHERY_DEFAULT_DB, LOKI_PLATFORMS
from .knowledge_base import service as kb

mcp = FastMCP("zhenyun-pangu-mcp")
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

    盘古专属能力：多租户查询。
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

    issue_id 为工单加密 ID；comment 支持纯文本或 HTML（纯文本自动转 <p> 段落）。
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
        return _json(search.search_repo(
            keyword, mode=mode, max_results=max_results,
            context=context, depth=depth,
        ))
    except Exception as e:  # 文件系统错误等
        return _json({"error": str(e)})


# ============================================================================
# gitlab_* 代码平台（GitLab 仓库：项目/代码/文件/目录/分支，整合自 gitlab-code-mcp）
# ============================================================================

@mcp.tool()
def gitlab_search_projects(query: str, per_page: int = 20) -> str:
    """搜索 GitLab 项目（按名称/路径关键词）。"""
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
        return _json({"count": len(slim), "projects": slim})
    except gitlab.GitLabError as e:
        return _json({"error": str(e)})


@mcp.tool()
def gitlab_search_code(query: str, per_page: int = 20) -> str:
    """GitLab 代码搜索（在配置的搜索根 group/project 下按关键词检索 blob）。

    返回命中文件路径与行号，通常再配合 gitlab_get_file 读取具体内容。
    """
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
                "ref": r.get("ref"),
            }
            for r in results
        ]
        return _json({"count": len(slim), "results": slim})
    except gitlab.GitLabError as e:
        return _json({"error": str(e)})


@mcp.tool()
def gitlab_get_file(project_id: str, path: str, ref: str = "master") -> str:
    """读取 GitLab 仓库文件的原始内容（文本）。"""
    try:
        content = gitlab.GitLabClient().get_file(project_id, path, ref=ref)
        return _json({"project_id": project_id, "path": path, "ref": ref, "content": content})
    except gitlab.GitLabError as e:
        return _json({"error": str(e)})


@mcp.tool()
def gitlab_list_tree(
    project_id: str,
    path: str = "",
    ref: str = "master",
    recursive: bool = False,
    per_page: int = 100,
) -> str:
    """列出 GitLab 仓库目录树（文件/子目录）。"""
    try:
        items = gitlab.GitLabClient().list_tree(
            project_id, path=path, ref=ref, recursive=recursive, per_page=per_page
        )
        return _json({"count": len(items), "tree": items})
    except gitlab.GitLabError as e:
        return _json({"error": str(e)})


@mcp.tool()
def gitlab_list_branches(project_id: str, per_page: int = 50) -> str:
    """列出 GitLab 仓库分支。"""
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
        return _json({"count": len(slim), "branches": slim})
    except gitlab.GitLabError as e:
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
    """检索业务知识/排查经验（混合检索：语义向量 + 关键词，综合排序）。

    排障/写 SQL 前先检索是否已有认知沉淀。可按 类型/系统/模块/状态 过滤。
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
    """检索可复用的 SQL/修复模板（混合检索）。生成 SQL 前先调用，形成闭环。"""
    return kb.search_sql_templates(keyword, category, system, business_domain, verified_only, limit, use_semantic)


@mcp.tool()
def search_tables(
    query: str,
    domain: str = "",
    db_name: str = "",
    top_k: int = 5,
    use_semantic: bool = True,
) -> str:
    """按关键词/语义检索表目录（table_catalog），返回表注释、关键字段等机器事实。"""
    return kb.search_tables(query, domain, db_name, top_k, use_semantic)


@mcp.tool()
def search_pangu(query: str, system: str = "", module: str = "", category: str = "", top_k: int = 3) -> str:
    """统一搜索：一次同时检索 知识 + 模板 + 表 + 相关关系，适合快速发现；精准检索请用专项工具。"""
    return kb.search_pangu(query, system, module, category, top_k)


# ---- Context：获取完整上下文 ----
@mcp.tool()
def get_knowledge(doc_id: int) -> str:
    """按 id 获取单条知识的完整正文。"""
    return kb.get_knowledge(doc_id)


@mcp.tool()
def get_sql_template(template_id: int) -> str:
    """按 id 获取单条 SQL 模板的完整内容（含执行过程与示例）。"""
    return kb.get_sql_template(template_id)


@mcp.tool()
def get_table(table_name: str, db_name: str = "") -> str:
    """获取单张表的元数据详情（表注释/描述/关键字段/入口字段）。"""
    return kb.get_table(table_name, db_name)


@mcp.tool()
def get_table_relations(table_name: str) -> str:
    """获取某张表已沉淀的关联关系（join 路径 + 关系语义 + 置信度）。"""
    return kb.get_table_relations(table_name)


# ---- Composite：组合诊断 ----
@mcp.tool()
def diagnose_context(query: str, system: str = "", module: str = "", limit: int = 3) -> str:
    """组合诊断：针对一个业务问题，自动汇集 认知 → 行动模板 → 相关表 → 表关系 的诊断上下文。

    内部自动完成 knowledge→template→table→relation 的多层检索，返回统一诊断上下文。
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
    """沉淀一条知识到知识库（写操作）。默认会做相似去重，跳过需 skip_dup_check=true。"""
    return kb.save_knowledge(
        title, content_md, knowledge_type, system, module, summary,
        core_tables, related_template_ids, tags, status, source_type, created_by, skip_dup_check,
    )


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
    """沉淀一条可复用 SQL/修复模板（写操作）。parameters 传 JSON 字符串存为 JSONB。"""
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
    """列出模板库中的模板（可按分类/系统/业务域/验证状态过滤），用于总览与维护。"""
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
    """更新已有模板字段（写操作；补充验证标记/修正 SQL/调整分类风险）。"""
    return kb.update_sql_template(
        template_id, title, scenario, sql_text, category, system, status, risk_level,
        business_domain, keywords, core_tables, parameters, execution_policy,
        source_type, verified,
    )


@mcp.tool()
def delete_sql_template(template_id: int) -> str:
    """删除指定模板（写操作，仅维护场景使用）。"""
    return kb.delete_sql_template(template_id)


@mcp.tool()
def record_template_usage(template_id: int) -> str:
    """模板被复用后累加使用次数。"""
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
) -> str:
    """沉淀一条经过 SQL 验证的表关联关系（写操作，upsert 去重）。"""
    return kb.add_table_relation(from_table, to_table, join_on, relation_type, description, confidence, from_db, to_db)


@mcp.tool()
def record_table_usage(table_names: str) -> str:
    """记录表被使用（自进化权重），table_names 逗号分隔。"""
    return kb.record_table_usage(table_names)


@mcp.tool()
def upsert_table_knowledge(
    table_name: str, description: str = "", tags: str = "", db_name: str = "",
) -> str:
    """修正/补录单表元数据描述与标签（写操作，upsert）。"""
    return kb.upsert_table_knowledge(table_name, description, tags, db_name)


def main() -> None:
    mcp.run()
