"""zhenyun-pangu-mcp — 甄云盘古通用工具 MCP（完全自包含，无外部仓库依赖）。

工具按前缀分组：
  - obs_*       日志查询（Loki 双平台 aws/cn + 阿里云 SLS 仅 cn 盘古 prod）
  - archery_*   数据库查询（Archery 双站点 cn/aws + 盘古专属租户/实例/库列表）
  - choerodon_* 猪齿鱼协作（内置 Python 客户端，OAuth 账号密码登录）
  - search_repo 跨仓代码搜索（内置纯标准库文件遍历，零外部依赖）
  - gitlab_*    GitLab 项目/代码/文件/目录/分支查询
  - search/get/save_* 认知层知识、SQL 模板、表目录和关联关系检索/维护

工具选择原则：先用认知层工具发现稳定规则、历史方案和候选表，再用日志/Archery/
GitLab/猪齿鱼获取当前事实；认知层写工具只沉淀用户确认后的元数据，不执行业务写 SQL。
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
    """列出指定日志平台的 Loki 数据源（只读）。

    何时调用：不知道 ``env`` 对应的数据源名称，或需要确认 cn/aws 平台连通性时；
    返回真实 datasource 名称，不需要手工猜测或把 Grafana 页面名称写进 LogQL。
    """
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
    db 默认 srm。用户 SQL 仅允许单条基础 SELECT、EXPLAIN SELECT 或 SHOW CREATE TABLE；
    不支持其它 SHOW/DESC、WITH、多语句、注释、函数/子查询、窗口函数、集合运算或任何写入语法。
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
    """获取当前数据库表结构（只读，底层为 SHOW CREATE TABLE）。

    何时调用：表名已确定但字段、类型、索引或注释不确定时；生成查询/修复 SQL
    前优先使用本工具确认实时 DDL。它查询真实数据库，不依赖知识库目录。
    """
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
    """获取当前数据库表的字段名列表（只读）。

    何时调用：只需快速校验字段是否存在，或生成 WHERE/JOIN/修复 SQL 前核对拼写时；
    需要完整类型、索引和注释时改用 archery_describe_table。
    """
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
    """列出指定 Archery 实例下的数据库（只读）。

    何时调用：不确定使用 ``srm``、``srm_logistics_delivery`` 等库，或跨库查询前；
    这是固定的内部发现能力，不等于 archery_query 对用户开放了任意 SHOW 语句。
    """
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
    """搜索 GitLab 项目（只读）。

    何时调用：不知道仓库的 project_id/path，或需要先确认标准库与二开库归属时；
    返回项目 id、完整路径、默认分支和网页地址，后续交给其它 gitlab_* 工具。
    """
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

    何时调用：知道类名、方法名、错误文本或配置键但不知道文件位置时；返回命中
    项目、路径、分支和行号，随后用 gitlab_get_file 读取完整文件核对上下文。
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
    """读取 GitLab 仓库指定分支/引用下的完整文件（只读）。

    何时调用：gitlab_search_code 或 gitlab_list_tree 已定位文件后，需要完整源码、
    配置或版本上下文时；``project_id``、``path``、``ref`` 必须来自真实 GitLab 返回。
    """
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
    """列出 GitLab 仓库目录树（只读）。

    何时调用：已知仓库但不知道模块/文件路径，或需要确认某个 ref 下的目录结构时；
    找到目标文件后再用 gitlab_get_file，``recursive`` 用于控制扫描范围。
    """
    try:
        items = gitlab.GitLabClient().list_tree(
            project_id, path=path, ref=ref, recursive=recursive, per_page=per_page
        )
        return _json({"count": len(items), "tree": items})
    except gitlab.GitLabError as e:
        return _json({"error": str(e)})


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

    返回 from/to 表、join 条件、关系类型、描述和置信度；用于设计 JOIN 或
    诊断数据链路。关系是知识库沉淀，不等于数据库约束，执行前仍应确认两端
    字段存在并用 SELECT 验证结果。
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
) -> str:
    """写入一条已验证的表关联关系（写操作，需用户确认，按键 upsert）。

    仅在 Archery/SELECT 验证两端字段和 join 结果后调用；``join_on`` 传可读
    的连接条件（如 ``a.order_id = b.order_id``），``confidence`` 范围 0~1，
    ``from_db``/``to_db`` 用实际库名。该记录是知识库元数据，不创建数据库
    外键，也不执行 join。
    """
    return kb.add_table_relation(from_table, to_table, join_on, relation_type, description, confidence, from_db, to_db)


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
