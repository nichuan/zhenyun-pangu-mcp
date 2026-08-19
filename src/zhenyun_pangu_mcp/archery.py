"""Archery 数据库客户端 + 盘古专属能力。

使用 csrftoken + sessionid 认证模型（双站点 cn/aws），
覆盖盘古专属能力：租户查询、实例列表、环境映射。

只读安全：仅放行 SELECT/SHOW/DESC/EXPLAIN/WITH 前缀，写操作拦截。
"""
from __future__ import annotations

import os

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


# 只读语句前缀
_READ_PREFIXES = ("select", "show", "explain", "with", "desc", "describe", "use ", "pragma")


def is_write_sql(sql: str) -> bool:
    s = " ".join(sql.strip().lower().split())
    return not s.startswith(_READ_PREFIXES)


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
    def query(self, sql: str, instance_name: str, db_name: str, limit_num: int = 100) -> dict:
        if is_write_sql(sql):
            raise ArcheryError(f"拒绝执行写操作 SQL（仅允许 SELECT/SHOW/DESC/EXPLAIN）: {sql[:80]}")
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
        sql = (
            f"SELECT tenant_id, tenant_num, tenant_name, enabled_flag "
            f"FROM hpfm_tenant WHERE tenant_num = '{tenant}' OR tenant_name LIKE '%{tenant}%' LIMIT 50"
        )
    else:
        sql = "SELECT tenant_id, tenant_num, tenant_name, enabled_flag FROM hpfm_tenant LIMIT 100"
    return client.query(sql, instance_name, db_name, 100)


def query_db_list(site: str, instance_name: str) -> dict:
    """查询实例下的数据库列表（SHOW DATABASES）。"""
    client = _client(site)
    return client.query("SHOW DATABASES", instance_name, "", 1000)
