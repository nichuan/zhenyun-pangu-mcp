"""Archery 数据库客户端 + 盘古专属能力。

使用 csrftoken + sessionid 认证模型（双站点 cn/aws），
覆盖盘古专属能力：租户查询、实例列表、环境映射。

只读安全：用户可执行的 SQL 仅允许单条基础 SELECT、EXPLAIN SELECT、SHOW CREATE TABLE，
写操作及高级语法拦截。
"""
from __future__ import annotations

import os
import re

import requests
from requests.exceptions import RequestException
from urllib.parse import urljoin, urlparse

from .config import (
    ARCHERY_BASE_URLS,
    ARCHERY_CREDENTIALS,
    ARCHERY_INSTANCE_ALIASES,
    ARCHERY_DEFAULT_DB,
)


class ArcheryError(Exception):
    """Archery 查询/认证错误。"""


# Archery 用户查询只支持最基本的单条只读语句：SELECT、EXPLAIN SELECT、
# SHOW CREATE TABLE。
#
# 这里不使用字符串前缀白名单之外的“放行”策略：仅检查 startswith 会让
# `SELECT ...; DELETE ...`、`WITH ... DELETE ...` 等语句绕过只读边界。
_UNSUPPORTED_SQL_TOKENS = (
    "insert", "update", "delete", "replace", "merge", "upsert", "drop", "alter",
    "create", "truncate", "grant", "revoke", "call", "execute", "set", "use",
    "show", "describe", "explain", "with", "union", "intersect", "except",
    "over", "partition", "window", "procedure", "function", "trigger", "into", "outfile",
    "dumpfile", "for", "lock", "case", "when", "then", "else", "end",
)


def _sql_code(sql: str) -> str:
    """返回 SQL 的非字符串部分，同时拒绝注释和多语句。

    仅用于安全边界校验，不是完整 SQL parser；字符串中的关键字/分号不会被
    误判，字符串外的注释、括号和语句分隔符则一律拒绝。
    """
    if not isinstance(sql, str) or not sql.strip():
        raise ArcheryError("SQL 不能为空")

    code: list[str] = []
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
                raise ArcheryError("SQL 包含未闭合的引号")
            # 保留反引号标识符的占位符，便于校验 SHOW CREATE TABLE 的表名；
            # 字符串内容仍完全抹去，避免其中的关键字参与语法判断。
            code.append("__quoted_identifier__" if quote == "`" else " ")
            continue
        # 注释会隐藏后续语句或改变 WHERE 语义，用户 SQL 一律不允许。
        if sql.startswith("--", i) or ch == "#" or sql.startswith("/*", i):
            raise ArcheryError("不允许使用 SQL 注释")
        if ch == ";":
            # 允许一个末尾语句 terminator，除此之外均视为多语句。
            if sql[i + 1 :].strip():
                raise ArcheryError("仅允许单条只读 SQL，不能包含多条语句")
            code.append(" ")
            i += 1
            continue
        if ch in "()":
            raise ArcheryError("仅支持基础只读 SQL，不支持函数、子查询或窗口表达式")
        code.append(ch)
        i += 1
    return "".join(code)


def validate_select_sql(sql: str) -> None:
    """校验用户 SQL 必须是单条基础只读语句。

    允许基础 SELECT、EXPLAIN SELECT、SHOW CREATE TABLE；SELECT 支持常见的
    列/表/WHERE/ORDER BY/GROUP BY/LIMIT 等语法。不允许 CTE、集合运算、窗口函数、
    函数/子查询括号、DDL/DML、注释和多语句。

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
            raise ArcheryError("SHOW CREATE TABLE 后必须是单个表名")
        return

    if not is_select and not is_explain_select:
        raise ArcheryError(
            "仅允许执行基础 SELECT、EXPLAIN SELECT 或 SHOW CREATE TABLE"
        )

    # EXPLAIN 只允许解释 SELECT，不能借此包装 UPDATE/DELETE 等写操作。
    statement = normalized[len("explain ") :] if is_explain_select else normalized
    for token in _UNSUPPORTED_SQL_TOKENS:
        if re.search(rf"\b{token}\b", statement):
            raise ArcheryError(
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
            raise ArcheryError(f"未知站点: {site}（可选 cn/aws）")
        self.site = site
        self.base_url = ARCHERY_BASE_URLS[site].rstrip("/")
        self.username, self.password = ARCHERY_CREDENTIALS[site]
        self.timeout = timeout
        self.session = requests.Session()
        self.domain = urlparse(self.base_url).netloc.split(":")[0]
        self._authenticated = False

    # ---------- 认证 ----------
    def authenticate(self) -> None:
        if self._authenticated:
            return
        if not self.username or not self.password:
            raise ArcheryError(
                f"站点「{self.site}」未配置账号密码，请在 .env 设置 "
                f"{'ARCHERY_' if self.site == 'cn' else 'ARCHERY_AWS_'}USERNAME/PASSWORD"
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

    def _ensure_csrf(self) -> str | None:
        if not self.session.cookies.get("csrftoken"):
            try:
                self.session.get(urljoin(self.base_url + "/", "sqlquery/"), timeout=self.timeout)
            except RequestException:
                pass
        return self.session.cookies.get("csrftoken")

    def _parse(self, resp) -> dict:
        try:
            payload = resp.json()
        except ValueError:
            if resp.status_code in (302, 403) or "login" in resp.text.lower():
                self._authenticated = False
                raise ArcheryError("认证失效，请重新登录")
            raise ArcheryError(f"返回非 JSON（HTTP {resp.status_code}），请检查实例/库名")
        if payload.get("status") not in (0, None):
            raise ArcheryError(payload.get("msg") or "请求失败（status 非 0）")
        return payload

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
        self.authenticate()
        url = urljoin(self.base_url + "/", "query/")
        data = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": "",
            "sql_content": sql,
            "limit_num": limit_num,
        }
        csrf = self._ensure_csrf()
        headers = {
            "X-CSRFToken": csrf or "",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            resp = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
        except RequestException as e:
            raise ArcheryError(f"查询请求失败: {e}") from e
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
        self.authenticate()
        url = urljoin(self.base_url + "/", "instance/describetable/")
        data = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": tb_name,
        }
        csrf = self._ensure_csrf()
        headers = {
            "X-CSRFToken": csrf or "",
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        try:
            resp = self.session.post(url, data=data, headers=headers, timeout=self.timeout)
        except RequestException as e:
            raise ArcheryError(f"获取表结构失败: {e}") from e
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
        self.authenticate()
        url = urljoin(self.base_url + "/", "instance/instance_resource/")
        params = {
            "instance_name": instance_name,
            "db_name": db_name,
            "schema_name": "",
            "tb_name": tb_name,
            "resource_type": "column",
        }
        self._ensure_csrf()
        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "Referer": urljoin(self.base_url + "/", "sqlquery/"),
        }
        try:
            resp = self.session.get(url, params=params, headers=headers, timeout=self.timeout)
        except RequestException as e:
            raise ArcheryError(f"获取字段列表失败: {e}") from e
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
            raise ArcheryError(
                f"实例别名「{name}」属于 {other_site} 站点，但当前 site 为「{site}」。"
                f"请显式指定 site=\"{other_site}\"（而非仅在 instance 传 \"{name}\"）。"
            )
    # 非别名：视为真实实例名，由后续查询实际鉴权决定是否有权限
    return name


def _client(site: str) -> ArcheryClient:
    return ArcheryClient(site)


# ============================================================================
# 盘古专属能力（租户查询、实例/库列表、环境映射）
# ============================================================================

def query_tenant(site: str, tenant: str | None, instance_name: str, db_name: str) -> dict:
    """查询 hpfm_tenant 租户信息。"""
    client = _client(site)
    if tenant:
        tenant = tenant.strip()
        # 该能力的 SQL 由服务端内部拼接，租户号/名称只接受常见业务编码字符，
        # 避免引号、注释和控制字符改变查询语义。
        if not re.fullmatch(r"[\w .:/-]{1,100}", tenant, flags=re.UNICODE):
            raise ArcheryError("tenant 仅允许字母、数字、空格及 . : / - _ 字符")
        escaped = tenant.replace("\\", "\\\\").replace("'", "''")
        sql = (
            f"SELECT tenant_id, tenant_num, tenant_name, enabled_flag "
            f"FROM hpfm_tenant WHERE tenant_num = '{escaped}' OR tenant_name LIKE '%{escaped}%' LIMIT 50"
        )
    else:
        sql = "SELECT tenant_id, tenant_num, tenant_name, enabled_flag FROM hpfm_tenant LIMIT 100"
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
