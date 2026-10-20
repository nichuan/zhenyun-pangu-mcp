"""Archery 数据库客户端 + 盘古专属能力。

使用 csrftoken + sessionid 认证模型（双站点 cn/aws），
覆盖盘古专属能力：租户查询、实例列表、环境映射。

查询安全：archery_query 仅允许单条 SELECT、EXPLAIN SELECT、SHOW CREATE TABLE。
SQL 工单接口只做校验与提交，不执行工单中的写 SQL。
"""
from __future__ import annotations

import os
import re
import hashlib
import secrets
import threading
import time
from html.parser import HTMLParser

import requests
import sqlparse
from sqlparse import tokens as sql_tokens
from requests.exceptions import RequestException
from urllib.parse import urljoin, urlparse

from .config import (
    ARCHERY_BASE_URLS,
    ARCHERY_CREDENTIALS,
    ARCHERY_INSTANCE_ALIASES,
    ARCHERY_DEFAULT_DB,
)


class ArcheryError(Exception):
    """Archery 错误，带可供 Agent 处理的错误分类。"""

    def __init__(
        self,
        message: str,
        *,
        code: str = "archery_query",
        retryable: bool = True,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


class ArcheryParameterError(ArcheryError):
    """调用参数或 SQL 语法错误；修正输入前重试不会改变结果。"""

    def __init__(self, message: str) -> None:
        super().__init__(message, code="bad_param", retryable=False)


class _WorkflowGroupOptionParser(HTMLParser):
    """读取 SQL 上线表单中当前用户可选的资源组名称和 ID。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.groups: list[tuple[str, int]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() != "option":
            return
        attributes = dict(attrs)
        name = attributes.get("value")
        group_id = attributes.get("group-id") or attributes.get("data-group-id")
        if not name or not group_id:
            return
        try:
            parsed_id = int(group_id)
        except (TypeError, ValueError):
            return
        if parsed_id > 0:
            self.groups.append((name, parsed_id))


# 同一 MCP 进程内按站点复用登录态，避免每次工具调用都重新登录。
# Session 还包含 cookie jar，因此必须和站点绑定，不能在 cn/aws 之间混用。
_CLIENTS: dict[str, "ArcheryClient"] = {}
_CLIENTS_LOCK = threading.Lock()

_WORKFLOW_CHECKS: dict[str, dict] = {}
_WORKFLOW_CHECKS_LOCK = threading.Lock()
_WORKFLOW_CHECK_TTL = 600


def validate_workflow_dml(sql: str) -> str:
    """仅接受纯 INSERT/UPDATE/DELETE 语句包，保留顺序并规范末尾分号。"""
    if not isinstance(sql, str) or not sql.strip():
        raise ArcheryParameterError("工单 SQL 不能为空")
    statements = sqlparse.parse(sql)
    if not statements:
        raise ArcheryParameterError("工单 SQL 不能为空")
    result = []
    for statement in statements:
        if any(token.ttype in sql_tokens.Comment for token in statement.flatten()):
            raise ArcheryParameterError("工单 SQL 只接受写语句，不含注释或预查/核验 SELECT")
        content = str(statement).strip().rstrip(";").strip()
        if not content or not re.match(r"^(INSERT|UPDATE|DELETE)\b", content, re.IGNORECASE):
            raise ArcheryParameterError("工单 SQL 仅允许 INSERT、UPDATE、DELETE 语句")
        if statement.get_type() not in {"INSERT", "UPDATE", "DELETE"}:
            raise ArcheryParameterError("工单 SQL 包含无法识别的写语句")
        result.append(content + ";")
    return "\n".join(result)


def _remember_workflow_check(site: str, instance_id: int, db_name: str, sql: str) -> tuple[str, str]:
    token = secrets.token_urlsafe(24)
    digest = hashlib.sha256(sql.encode("utf-8")).hexdigest()
    with _WORKFLOW_CHECKS_LOCK:
        now = time.monotonic()
        for key, value in list(_WORKFLOW_CHECKS.items()):
            if value["expires_at"] <= now:
                del _WORKFLOW_CHECKS[key]
        _WORKFLOW_CHECKS[token] = {
            "site": site, "instance_id": instance_id, "db_name": db_name,
            "sql": sql, "sha256": digest, "expires_at": now + _WORKFLOW_CHECK_TTL,
        }
    return token, digest


def _consume_workflow_check(token: str) -> dict:
    with _WORKFLOW_CHECKS_LOCK:
        record = _WORKFLOW_CHECKS.pop(token, None)
    if not record or record["expires_at"] <= time.monotonic():
        raise ArcheryParameterError("校验凭证不存在、已使用或已过期；请重新校验 SQL")
    return record


# Archery 用户查询只支持单条只读语句：SELECT、EXPLAIN SELECT、
# SHOW CREATE TABLE。SELECT 可包含只读子查询、EXISTS、派生表和 UNION。
#
# 这里不使用字符串前缀白名单之外的“放行”策略：仅检查 startswith 会让
# `SELECT ...; DELETE ...`、`WITH ... DELETE ...` 等语句绕过只读边界。
_UNSUPPORTED_SQL_TOKENS = (
    "insert", "update", "delete", "replace", "merge", "upsert", "drop", "alter",
    "create", "truncate", "grant", "revoke", "call", "execute", "set", "use",
    "show", "describe", "explain", "with", "intersect", "except",
    "over", "partition", "window", "procedure", "function", "trigger", "into", "outfile",
    "dumpfile", "for", "lock",
)

# 识别函数调用时保留白名单；下面这些词后面的括号是 SQL 结构，
# 例如 EXISTS (SELECT ...)、FROM (SELECT ...) 和 AND (... OR ...)。
_SAFE_SQL_FUNCTIONS = frozenset({
    "count", "sum", "avg", "min", "max", "ifnull", "nullif",
    "concat", "concat_ws", "cast",
})
_SQL_PAREN_KEYWORDS = frozenset({
    "select", "from", "where", "and", "or", "not", "in", "exists",
    "on", "join", "having", "when", "then", "else", "case", "as",
    "union", "all", "by", "limit", "is", "between",
})


def _sql_code(sql: str) -> str:
    """返回 SQL 的非字符串部分，允许安全的前置 Mapper 标记注释。

    仅用于安全边界校验，不是完整 SQL parser；字符串中的关键字/分号不会被
    误判；可执行注释、多语句和非白名单函数会被拒绝。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ArcheryParameterError("SQL 不能为空")

    code: list[str] = []
    parens: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        # SQL 字符串和带引号标识符：跳过内容，但保留空格以避免关键字粘连。
        if ch in ("'", '"', "`"):
            quote = ch
            i += 1
            while i < n:
                if sql[i] == "\\" and quote != "`":
                    i += 2
                    continue
                if sql[i] == quote:
                    # SQL 用两个引号表示一个引号：'' / "" / ``。
                    if i + 1 < n and sql[i + 1] == quote:
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            else:
                raise ArcheryParameterError("SQL 包含未闭合的引号")
            # 保留反引号标识符的占位符，便于校验 SHOW CREATE TABLE 的表名；
            # 字符串内容仍完全抹去，避免其中的关键字参与语法判断。
            code.append("__quoted_identifier__" if quote == "`" else " ")
            continue
        # 生产 MyBatis 日志会在 SQL 开头附带 /*Mapper.id*/。仅允许
        # 不含空白和操作符的前置标记；MySQL /*!...*/ 可执行注释始终拒绝。
        if sql.startswith("/*", i):
            end = sql.find("*/", i + 2)
            marker = sql[i + 2:end] if end >= 0 else ""
            if "".join(code).strip() or not re.fullmatch(r"[A-Za-z0-9_.$:-]+", marker):
                raise ArcheryParameterError("仅允许 SQL 开头的普通 Mapper 标记注释")
            code.append(" ")
            i = end + 2
            continue
        if sql.startswith("--", i) or ch == "#":
            raise ArcheryParameterError("不允许使用 SQL 行注释")
        if ch == ";":
            # 允许一个末尾语句 terminator，除此之外均视为多语句。
            if sql[i + 1 :].strip():
                raise ArcheryParameterError("仅允许单条只读 SQL，不能包含多条语句")
            code.append(" ")
            i += 1
            continue
        if ch == "(":
            match = re.search(r"([A-Za-z_][A-Za-z0-9_$]*)\s*$", "".join(code))
            word = match.group(1).lower() if match else ""
            if word and word not in _SQL_PAREN_KEYWORDS and word not in _SAFE_SQL_FUNCTIONS:
                raise ArcheryParameterError(
                    f"不支持的 SQL 函数或括号结构：{word}"
                )
            parens.append("function" if word in _SAFE_SQL_FUNCTIONS else "expression")
            code.append(ch)
            i += 1
            continue
        if ch == ")":
            if not parens:
                raise ArcheryParameterError("SQL 括号不匹配")
            parens.pop()
            code.append(ch)
            i += 1
            continue
        code.append(ch)
        i += 1
    if parens:
        raise ArcheryParameterError("SQL 包含未闭合的括号")
    return "".join(code)


def validate_select_sql(sql: str) -> None:
    """校验用户 SQL 必须是单条只读语句。

    允许 SELECT、EXPLAIN SELECT、SHOW CREATE TABLE；SELECT 支持常见的
    JOIN、CASE、EXISTS、派生表、IN 子查询和 UNION。仍拒绝写入、CTE、
    窗口函数、可执行注释、多语句和非白名单函数。

    函数名保留为历史名称，调用方无需修改。
    """
    code = _sql_code(sql)
    normalized = " ".join(code.strip().lower().split())
    is_select = normalized == "select" or normalized.startswith("select ")
    is_explain_select = normalized == "explain select" or normalized.startswith("explain select ")
    show_prefix = "show create table"
    is_show_create = normalized.startswith(show_prefix + " ")

    if is_show_create:
        # 只允许 SHOW CREATE TABLE 加一个简单表名（可带 schema，或使用反引号标识符）。
        # _sql_code 将反引号标识符替换为占位符，避免表名中的关键字被误判。
        table_name = normalized[len(show_prefix) + 1 :]
        identifier = r"(?:[a-zA-Z_][a-zA-Z0-9_$-]*|__quoted_identifier__)"
        if not re.fullmatch(rf"{identifier}(?:\.{identifier})?", table_name):
            raise ArcheryParameterError("SHOW CREATE TABLE 后必须是单个表名")
        return

    if not is_select and not is_explain_select:
        raise ArcheryParameterError(
            "仅允许执行基础 SELECT、EXPLAIN SELECT 或 SHOW CREATE TABLE"
        )

    # EXPLAIN 只允许解释 SELECT，不能借此包装 UPDATE/DELETE 等写操作。
    statement = normalized[len("explain ") :] if is_explain_select else normalized
    for token in _UNSUPPORTED_SQL_TOKENS:
        if re.search(rf"\b{token}\b", statement):
            raise ArcheryParameterError(
                f"不支持的 SQL 语法：{token.upper()}（仅允许基础 SELECT、EXPLAIN SELECT 或 SHOW CREATE TABLE）"
            )


def is_write_sql(sql: str) -> bool:
    """兼容旧调用方：非法/非基础只读 SQL 均视为不可执行。"""
    try:
        validate_select_sql(sql)
    except ArcheryError:
        return True
    return False


class ArcheryClient:
    def __init__(self, site: str = "cn", timeout: int = 60):
        if site not in ARCHERY_BASE_URLS:
            raise ArcheryParameterError(f"未知站点: {site}（可选 cn/aws）")
        self.site = site
        self.base_url = ARCHERY_BASE_URLS[site].rstrip("/")
        self.username, self.password = ARCHERY_CREDENTIALS[site]
        self.timeout = timeout
        self.session = requests.Session()
        self.domain = urlparse(self.base_url).netloc.split(":")[0]
        self._authenticated = False
        # requests.Session 不保证并发读写安全；同一个缓存 client 的请求串行化，
        # 同时使用 RLock 允许请求辅助方法嵌套调用 authenticate/_ensure_csrf。
        self._request_lock = threading.RLock()

    # ---------- 认证 ----------
    def authenticate(self) -> None:
        with self._request_lock:
            if self._authenticated:
                return
            if not self.username or not self.password:
                raise ArcheryError(
                    f"站点「{self.site}」未配置账号密码，请在 .env 设置 "
                    f"{'ARCHERY_' if self.site == 'cn' else 'ARCHERY_AWS_'}USERNAME/PASSWORD",
                    code="config",
                    retryable=False,
                )
            login_url = urljoin(self.base_url + "/", "login/")
            auth_url = urljoin(self.base_url + "/", "authenticate/")

            # 1) GET 登录页拿 csrftoken
            try:
                self.session.get(login_url, timeout=self.timeout)
            except RequestException as e:
                raise ArcheryError(f"无法访问登录页: {e}") from e
            csrf = self.session.cookies.get("csrftoken")
            if not csrf:
                raise ArcheryError("未从登录页获取到 csrftoken，请检查登录地址")

            # 2) POST /authenticate/
            headers = {
                "X-CSRFToken": csrf,
                "X-Requested-With": "XMLHttpRequest",
                "Referer": login_url,
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            }
            data = {"username": self.username, "password": self.password}
            try:
                resp = self.session.post(auth_url, data=data, headers=headers, timeout=self.timeout)
            except RequestException as e:
                raise ArcheryError(f"登录请求失败: {e}") from e
            if not self.session.cookies.get("sessionid"):
                detail = (resp.text or "")[:200]
                raise ArcheryError(f"登录失败（未获取到 sessionid）: {detail}")
            self._authenticated = True

    def _invalidate_auth(self) -> None:
        """清除失效认证，使下一次请求重新获取 CSRF/session cookie。"""
        self._authenticated = False
        self.session.cookies.clear()

    def _request_with_reauth(
        self,
        method: str,
        url: str,
        *,
        data: dict | None = None,
        json_body: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
        csrf: bool = False,
    ):
        """发送 Archery API 请求，401/403/登录重定向时重新认证并重试一次。"""
        with self._request_lock:
            for attempt in range(2):
                self.authenticate()
                request_headers = dict(headers or {})
                if csrf:
                    request_headers["X-CSRFToken"] = self._ensure_csrf() or ""
                try:
                    response = self.session.request(
                        method,
                        url,
                        data=data,
                        json=json_body,
                        params=params,
                        headers=request_headers,
                        timeout=self.timeout,
                        allow_redirects=False,
                    )
                except RequestException as e:
                    raise ArcheryError(f"请求失败: {e}") from e
                if attempt == 0 and response.status_code in (301, 302, 401, 403):
                    self._invalidate_auth()
                    continue
                return response
        raise ArcheryError("请求失败：认证重试次数已耗尽")

    def _ensure_csrf(self) -> str | None:
        if not self.session.cookies.get("csrftoken"):
            try:
                self.session.get(urljoin(self.base_url + "/", "sqlquery/"), timeout=self.timeout)
            except RequestException:
                pass
        return self.session.cookies.get("csrftoken")

    def _parse(self, resp) -> dict:
        if resp.status_code in (301, 302, 401, 403):
            self._authenticated = False
            raise ArcheryError("认证失效，请重新登录")
        try:
            payload = resp.json()
        except ValueError:
            if "login" in resp.text.lower():
                self._authenticated = False
                raise ArcheryError("认证失效，请重新登录")
            raise ArcheryError(f"返回非 JSON（HTTP {resp.status_code}），请检查实例/库名")
        if payload.get("status") not in (0, None):
            raise ArcheryError(payload.get("msg") or "请求失败（status 非 0）")
        return payload

    def resolve_workflow_target(self, group_name: str = "", instance_name: str = "") -> dict:
        """通过 HTTP 自动发现目标实例所属的唯一审批组，不启动浏览器。"""
        group_name = (group_name or "").strip()
        instance_name = (instance_name or "").strip()
        if not instance_name:
            raise ArcheryParameterError("instance_name 必填：复用本次已核实的修复实例别名或真实名称")
        instance_name = resolve_instance(instance_name, self.site, "")

        submit_url = urljoin(self.base_url + "/", "submitsql/")
        page = self._request_with_reauth(
            "GET", submit_url,
            headers={"Referer": submit_url},
        )
        if page.status_code != 200:
            raise ArcheryError(
                f"读取 Archery SQL 上线选项失败（HTTP {page.status_code}）",
                code="archery_workflow_target", retryable=False,
            )
        parser = _WorkflowGroupOptionParser()
        parser.feed(page.text or "")
        groups: dict[str, set[int]] = {}
        for name, gid in parser.groups:
            groups.setdefault(name, set()).add(gid)
        if group_name:
            if group_name not in groups:
                raise ArcheryParameterError(
                    f"当前 Archery 账号的 SQL 上线选项中找不到审批组：{group_name}"
                )
            groups = {group_name: groups[group_name]}
        if not groups:
            raise ArcheryError(
                "当前账号未返回审批组选项，请检查 MCP 凭据、权限或接口兼容性；不要打开浏览器取参",
                code="archery_workflow_target", retryable=False,
            )
        if len(groups) > 50:
            raise ArcheryError(
                "当前账号可见审批组超过 50 个，停止自动遍历；请缩小审批组范围",
                code="archery_workflow_target", retryable=False,
            )

        instances_url = urljoin(self.base_url + "/", "group/instances/")
        candidates = []
        for name, group_ids in groups.items():
            if len(group_ids) != 1:
                raise ArcheryParameterError(f"审批组名称对应多个 group_id：{name}")
            response = self._request_with_reauth(
                "POST", instances_url,
                data={"group_name": name, "tag_code": "can_write"},
                headers={
                    "X-Requested-With": "XMLHttpRequest",
                    "Referer": submit_url,
                    "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
                },
                csrf=True,
            )
            if response.status_code != 200:
                raise ArcheryError(
                    f"读取审批组“{name}”的可写实例失败（HTTP {response.status_code}）；不要打开浏览器取参",
                    code="archery_workflow_target", retryable=False,
                )
            payload = self._parse(response)
            rows = payload.get("data")
            if not isinstance(rows, list):
                raise ArcheryError(
                    f"Archery 未返回审批组“{name}”可提交的实例列表",
                    code="archery_workflow_target", retryable=False,
                )
            matches = [
                row for row in rows
                if isinstance(row, dict) and row.get("instance_name") == instance_name
            ]
            if not matches:
                continue
            instance_ids = {
                int(row["id"]) for row in matches
                if str(row.get("id", "")).isdigit() and int(row["id"]) > 0
            }
            if len(instance_ids) != 1 or any(
                not str(row.get("id", "")).isdigit() or int(row["id"]) <= 0
                for row in matches
            ):
                raise ArcheryParameterError(
                    f"实例名称未唯一解析到数字 instance_id：{instance_name}（审批组：{name}）"
                )
            candidates.append({
                "site": self.site,
                "group_name": name,
                "group_id": next(iter(group_ids)),
                "instance_name": instance_name,
                "instance_id": instance_ids.pop(),
                "db_type": str(matches[0].get("db_type") or ""),
            })
        if not candidates:
            raise ArcheryParameterError(
                f"当前账号的审批组下找不到可提交实例：{instance_name}；请检查目标环境或账号权限，不要猜 ID 或打开浏览器取参"
            )
        if len(candidates) != 1:
            names = "、".join(item["group_name"] for item in candidates)
            raise ArcheryError(
                f"实例“{instance_name}”匹配多个审批组：{names}；仅需选择审批组名称，无需提供数字 ID 或打开浏览器",
                code="archery_workflow_target_ambiguous", retryable=False,
            )
        return candidates[0]

    def _workflow_response(self, response, operation: str, expected_status: int) -> dict:
        if response.status_code in (301, 302, 401, 403):
            self._invalidate_auth()
            raise ArcheryError("Archery 认证失效或无工单提交权限", code=operation, retryable=False)
        try:
            payload = response.json()
        except ValueError as e:
            raise ArcheryError(
                f"{operation} 返回非 JSON（HTTP {response.status_code}）",
                code=operation, retryable=False,
            ) from e
        if operation == "archery_submit_workflow" and response.status_code >= 500:
            raise ArcheryError(
                "工单提交返回服务端错误，结果可能已写入；需通过只读接口按名称查重，接口不可用则报告待核实，不回退浏览器或直接重试",
                code="archery_submit_unknown", retryable=False,
            )
        if response.status_code != expected_status or not isinstance(payload, dict):
            detail = payload.get("errors") or payload.get("detail") or payload.get("msg") if isinstance(payload, dict) else None
            raise ArcheryError(
                f"{operation} 失败（HTTP {response.status_code}）: {str(detail or payload)[:500]}",
                code=operation, retryable=False,
            )
        return payload

    def workflow_sqlcheck(self, sql: str, instance_id: int, db_name: str) -> dict:
        dml = validate_workflow_dml(sql)
        if not isinstance(instance_id, int) or isinstance(instance_id, bool) or instance_id <= 0:
            raise ArcheryParameterError("instance_id 必须为已核实的正整数")
        url = urljoin(self.base_url + "/", "api/v1/workflow/sqlcheck/")
        response = self._request_with_reauth(
            "POST", url,
            json_body={"full_sql": dml, "instance_id": instance_id, "db_name": db_name},
            headers={
                "X-Requested-With": "XMLHttpRequest",
                "Referer": urljoin(self.base_url + "/", "submitsql/"),
                "Content-Type": "application/json",
            },
            csrf=True,
        )
        payload = self._workflow_response(response, "archery_sqlcheck", 200)
        rows = payload.get("rows")
        try:
            error_count = int(payload["error_count"])
            warning_count = int(payload["warning_count"])
        except (KeyError, TypeError, ValueError) as e:
            raise ArcheryError("SQL 校验结果缺少错误/警告计数", code="archery_sqlcheck", retryable=False) from e
        row_errors = isinstance(rows, list) and any(
            not isinstance(row, dict) or str(row.get("errlevel", "")) not in {"0", "1"}
            for row in rows
        )
        checked = payload.get("checked") in (True, 1, "True", "true", "1")
        passed = (
            checked and isinstance(rows, list) and len(rows) >= len(sqlparse.parse(dml))
            and error_count == 0 and not payload.get("error")
            and not payload.get("is_critical") and not row_errors
        )
        result = {
            "site": self.site, "instance_id": instance_id,
            "db_name": db_name, "passed": passed, "error_count": error_count,
            "warning_count": warning_count, "check_result": payload,
        }
        if passed:
            token, digest = _remember_workflow_check(self.site, instance_id, db_name, dml)
            result.update(check_token=token, sql_sha256=digest, statement_count=len(sqlparse.parse(dml)))
        return result

    def submit_checked_workflow(
        self, check_token: str, workflow_name: str, group_id: int,
        demand_url: str = "", is_backup: bool = True,
    ) -> dict:
        if not workflow_name.strip() or len(workflow_name) > 50:
            raise ArcheryParameterError("工单名称不能为空且不能超过 50 字符")
        if not isinstance(group_id, int) or isinstance(group_id, bool) or group_id <= 0:
            raise ArcheryParameterError("group_id 必须为正整数")
        if len(demand_url) > 500:
            raise ArcheryParameterError("需求链接不能超过 500 字符")
        record = _consume_workflow_check(check_token)
        if record["site"] != self.site:
            raise ArcheryParameterError("校验凭证所属站点与提交站点不一致")
        url = urljoin(self.base_url + "/", "api/v1/workflow/")
        body = {
            "workflow": {
                "workflow_name": workflow_name, "demand_url": demand_url,
                "group_id": group_id, "instance": record["instance_id"],
                "db_name": record["db_name"], "is_backup": is_backup,
                "run_date_start": "", "run_date_end": "",
            },
            "sql_content": record["sql"],
        }
        with self._request_lock:
            self.authenticate()
            headers = {
                "X-Requested-With": "XMLHttpRequest",
                "X-CSRFToken": self._ensure_csrf() or "",
                "Referer": urljoin(self.base_url + "/", "submitsql/"),
                "Content-Type": "application/json",
            }
            try:
                response = self.session.request(
                    "POST", url, json=body, headers=headers,
                    timeout=self.timeout, allow_redirects=False,
                )
            except RequestException as e:
                raise ArcheryError(
                    "工单提交结果未知；需通过只读接口按名称查重，接口不可用则报告待核实，不回退浏览器或直接重试",
                    code="archery_submit_unknown", retryable=False,
                ) from e
        payload = self._workflow_response(response, "archery_submit_workflow", 201)
        workflow = payload.get("workflow")
        workflow_id = workflow.get("id") if isinstance(workflow, dict) else None
        if not workflow_id:
            raise ArcheryError(
                "提交返回 201 但缺少工单 ID；需通过只读接口查重，接口不可用则报告待核实，不回退浏览器或直接重试",
                code="archery_submit_unknown", retryable=False,
            )
        return {
            "submitted": True, "site": self.site,
            "workflow_id": workflow_id, "workflow_name": workflow_name,
            "instance_id": record["instance_id"], "db_name": record["db_name"],
            "group_id": group_id, "sql_sha256": record["sha256"],
            "workflow": workflow,
        }

    # ---------- SQL 查询 ----------
    def query(
        self,
        sql: str,
        instance_name: str,
        db_name: str,
        limit_num: int = 100,
        *,
        _internal_allow_non_select: bool = False,
    ) -> dict:
        # 只有固定的内部“列数据库”能力可以走 SHOW DATABASES；用户传入的
        # archery_query 永远使用默认 False，不能通过 SQL 文本绕过此边界。
        if not _internal_allow_non_select:
            validate_select_sql(sql)
        url = urljoin(self.base_url + "/", "query/")
        data = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": "",
            "sql_content": sql,
            "limit_num": limit_num,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        resp = self._request_with_reauth("POST", url, data=data, headers=headers, csrf=True)
        payload = self._parse(resp)
        block = payload.get("data", {})
        if isinstance(block, str):
            raise ArcheryError(block or "查询失败")
        columns = block.get("column_list") or []
        rows = []
        for row in block.get("rows") or []:
            if isinstance(row, (list, tuple)):
                rows.append(dict(zip(columns, row)))
            else:
                rows.append(row)
        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "affected_rows": block.get("affected_rows"),
            "is_masked": block.get("is_masked"),
            "query_time": block.get("query_time"),
            "error": block.get("error"),
        }

    # ---------- 表结构 ----------
    def describe_table(self, instance_name: str, db_name: str, tb_name: str) -> dict:
        url = urljoin(self.base_url + "/", "instance/describetable/")
        data = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": tb_name,
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        resp = self._request_with_reauth("POST", url, data=data, headers=headers, csrf=True)
        payload = self._parse(resp)
        block = payload.get("data", {})
        if isinstance(block, str):
            raise ArcheryError(block or "表不存在或无权限访问")
        rows = block.get("rows") or []
        create_table = ""
        table = tb_name
        if rows and isinstance(rows[0], (list, tuple)) and len(rows[0]) >= 2:
            table = rows[0][0]
            create_table = rows[0][1]
        return {"table": table, "create_table": create_table}

    def list_columns(self, instance_name: str, db_name: str, tb_name: str) -> list[str]:
        url = urljoin(self.base_url + "/", "instance/instance_resource/")
        params = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": tb_name,
            "resource_type": "column",
        }
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
        }
        resp = self._request_with_reauth("GET", url, params=params, headers=headers, csrf=True)
        payload = self._parse(resp)
        block = payload.get("data", [])
        if isinstance(block, str):
            raise ArcheryError(block or "表不存在或无权限访问")
        if not isinstance(block, list):
            return []
        return [str(c) for c in block]


def resolve_instance(name: str | None, site: str, default: str) -> str:
    """将实例别名解析为真实 Archery 实例名。

    name 为空 -> 返回 default。
    name 命中当前 site 的别名 -> 返回真实实例名。
    name 命中其它 site 的别名（如用 cn 站点查 aws 别名）-> 明确报错，提示正确 site。
    name 非别名 -> 视为直接传入的真实实例名（兼容性）。
    """
    if not name:
        return default
    name = name.strip()
    site_aliases = ARCHERY_INSTANCE_ALIASES.get(site, {})
    if name in site_aliases:
        return site_aliases[name]
    # 命中了其它站点的别名，但不在当前 site -> 明确提示，避免「未关联该实例」的歧义
    for other_site, aliases in ARCHERY_INSTANCE_ALIASES.items():
        if other_site == site:
            continue
        if name in aliases:
            raise ArcheryParameterError(
                f"实例别名「{name}」属于 {other_site} 站点，但当前 site 为「{site}」。"
                f"请显式指定 site=\"{other_site}\"（而非仅在 instance 传 \"{name}\"）。"
            )
    # 非别名：视为真实实例名，由后续查询实际鉴权决定是否有权限
    return name


def _client(site: str) -> ArcheryClient:
    with _CLIENTS_LOCK:
        client = _CLIENTS.get(site)
        if client is None:
            client = ArcheryClient(site)
            _CLIENTS[site] = client
        return client


# ============================================================================
# 盘古专属能力（租户查询、实例/库列表、环境映射）
# ============================================================================

def query_tenant(site: str, tenant: str | None, instance_name: str, db_name: str) -> dict:
    """查询 hpfm_tenant 租户信息。"""
    if not tenant or not tenant.strip():
        raise ArcheryParameterError("tenant 必填：请传租户编码或名称，不支持空参列举租户")
    client = _client(site)
    tenant = tenant.strip()
    # 该能力的 SQL 由服务端内部拼接，租户号/名称只接受常见业务编码字符，
    # 避免引号、注释和控制字符改变查询语义。
    if not re.fullmatch(r"[\w .:/-]{1,100}", tenant, flags=re.UNICODE):
        raise ArcheryParameterError("tenant 仅允许字母、数字、空格及 . : / - _ 字符")
    escaped = tenant.replace("\\", "\\\\").replace("'", "''")
    sql = (
        f"SELECT tenant_id, tenant_num, tenant_name, enabled_flag "
        f"FROM hpfm_tenant WHERE tenant_num = '{escaped}' OR tenant_name LIKE '%{escaped}%' LIMIT 50"
    )
    return client.query(sql, instance_name, db_name, 100)


def query_db_list(site: str, instance_name: str) -> dict:
    """查询实例下的数据库列表（SHOW DATABASES）。"""
    client = _client(site)
    # 这是 MCP 内部固定能力，不暴露为用户可执行 SQL；用户传入的
    # archery_query 仍然只能执行基础 SELECT / EXPLAIN SELECT / SHOW CREATE TABLE。
    return client.query(
        "SHOW DATABASES", instance_name, "", 1000,
        _internal_allow_non_select=True,
    )
