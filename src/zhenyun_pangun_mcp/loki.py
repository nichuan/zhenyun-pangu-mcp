"""Loki 日志客户端（复刻 pg-aws-log 的 GrafanaClient）。

通过 Grafana proxy 查询 Loki 日志（LogQL）。支持双平台：
  - aws：AWS 海外（jp-saas-1），URL 默认 logs.jp-saas-1.going-link.net
  - cn ：国内公有云（logs.going-link.net），非 prod 已切换至此

认证流程（Grafana v11）：
  1. POST {base}/login  body: {"user","password"} -> Set-Cookie: grafana_session
  2. 后续请求携带 Cookie: grafana_session
  3. GET /api/datasources 动态发现 Loki 数据源（UID）
"""
from __future__ import annotations

from urllib.parse import urlencode

import requests

from .config import LOKI_PLATFORMS, LOKI_DATASOURCES


class LokiError(Exception):
    """Loki 查询/认证错误。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class GrafanaClient:
    def __init__(self, platform: str):
        meta = LOKI_PLATFORMS.get(platform)
        if not meta:
            raise LokiError(f"未知日志平台: {platform}（可选 aws/cn）")
        self.platform = platform
        self.base_url = meta["url"].rstrip("/")
        self.username = meta["username"]
        self.password = meta["password"]
        self.session = requests.Session()
        self._cookie = ""

    def login(self) -> None:
        if not self.username or not self.password:
            raise LokiError(
                f"日志平台「{self.platform}」未配置账号密码，请在 .env 设置 "
                f"{'AWS' if self.platform == 'aws' else 'CN'}_LOG_USERNAME/PASSWORD"
            )
        resp = self.session.post(
            f"{self.base_url}/login",
            json={"user": self.username, "password": self.password},
            allow_redirects=False,
            timeout=30,
        )
        if resp.status_code not in (200, 302):
            raise LokiError(f"Grafana 登录失败: HTTP {resp.status_code}", resp.status_code)
        cookie = resp.headers.get("Set-Cookie", "")
        if "grafana_session" not in cookie:
            raise LokiError("Grafana 登录后未获取到 grafana_session cookie")
        # 提取 grafana_session 的值
        import re

        m = re.search(r"grafana_session=([^;]+)", cookie)
        if not m:
            raise LokiError("无法解析 grafana_session cookie")
        self._cookie = f"grafana_session={m.group(1)}"
        self.session.headers.update({"Cookie": self._cookie})

    def discover_loki_datasources(self, name: str | None = None) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/api/datasources", timeout=30)
        if resp.status_code != 200:
            raise LokiError(f"获取数据源列表失败: HTTP {resp.status_code}", resp.status_code)
        try:
            ds_list = resp.json()
        except ValueError as e:
            raise LokiError(f"数据源列表非 JSON 响应: {resp.text[:200]}") from e
        loki = [d for d in ds_list if d.get("type") == "loki"]
        if name is None:
            return loki
        hit = [d for d in loki if d.get("name") == name]
        if not hit:
            names = ", ".join(d.get("name", "?") for d in loki) or "(无)"
            raise LokiError(f"未找到 Loki 数据源「{name}」。可用: {names}")
        return hit

    def loki_query_range(
        self,
        uid: str,
        query: str,
        start: int,
        end: int,
        limit: int,
        direction: str,
    ) -> dict:
        params = {
            "query": query,
            "start": str(start),
            "end": str(end),
            "limit": str(limit),
            "direction": direction,
        }
        url = f"{self.base_url}/api/datasources/proxy/uid/{uid}/loki/api/v1/query_range"
        resp = self.session.get(url, params=params, timeout=60)
        if resp.status_code != 200:
            raise LokiError(f"Loki 查询失败: HTTP {resp.status_code}", resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise LokiError(f"Loki 非 JSON 响应: {resp.text[:200]}") from e


def resolve_datasource(platform: str, env: str) -> str:
    """将平台+环境解析为 Loki 数据源名。"""
    mapping = LOKI_DATASOURCES.get(platform, {})
    if env not in mapping:
        raise LokiError(f"未知环境「{env}」。可用: {', '.join(mapping.keys()) or '(无)'}")
    name = mapping[env]
    if not name:
        raise LokiError(
            f"「{platform}」平台的环境「{env}」数据源名未配置，请在 .env 设置 "
            f"{platform.upper()}_LOG_DS_{env.upper()}"
        )
    return name
