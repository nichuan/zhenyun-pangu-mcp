"""GitLab 代码平台客户端（整合自 gitlab-code-mcp，纯客户端，无 MCP 依赖）。

提供：项目搜索、代码搜索、文件读取、目录树、分支列表。
认证：优先 GITLAB_TOKEN；缺失时回退 GITLAB_USERNAME/GITLAB_PASSWORD 走 OAuth2 密码授权。
配置（从 .env 读取）：GITLAB_BASE_URL / GITLAB_TOKEN / GITLAB_USERNAME / GITLAB_PASSWORD /
GITLAB_SEARCH_ROOT_ID / GITLAB_SEARCH_ROOT_GROUP / GITLAB_SEARCH_DEFAULT_SCOPE
"""
from __future__ import annotations

from typing import Any, Dict, List

import requests
from requests.exceptions import RequestException
from urllib.parse import quote, urljoin

from .config import (
    GITLAB_BASE_URL,
    GITLAB_TOKEN,
    GITLAB_USERNAME,
    GITLAB_PASSWORD,
    GITLAB_SEARCH_ROOT_ID,
    GITLAB_SEARCH_ROOT_GROUP,
)


class GitLabError(Exception):
    """GitLab 客户端错误。"""


class GitLabClient:
    def __init__(self, timeout: int = 30):
        self.base_url = GITLAB_BASE_URL.rstrip("/")
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {"Accept": "application/json", "User-Agent": "zhenyun-pangu-mcp/1.0"}
        )
        self._token: str | None = None
        if GITLAB_TOKEN:
            self._token = GITLAB_TOKEN
            self.session.headers["PRIVATE-TOKEN"] = GITLAB_TOKEN
        elif GITLAB_USERNAME and GITLAB_PASSWORD:
            self._token = self._oauth_login()

    # ---------- 认证（仅用户名/密码时） ----------
    def _oauth_login(self) -> str:
        url = urljoin(self.base_url + "/", "oauth/token")
        try:
            resp = self.session.post(
                url,
                data={
                    "grant_type": "password",
                    "username": GITLAB_USERNAME,
                    "password": GITLAB_PASSWORD,
                },
                timeout=self.timeout,
            )
            resp.raise_for_status()
            data = resp.json()
        except RequestException as e:
            raise GitLabError(f"GitLab 登录失败: {e}") from e
        except ValueError as e:
            raise GitLabError(f"GitLab 登录响应解析失败: {e}") from e
        token = data.get("access_token")
        if not token:
            raise GitLabError("GitLab 登录未返回 access_token")
        self.session.headers["PRIVATE-TOKEN"] = token
        return token

    def _require_auth(self) -> None:
        if not self._token:
            raise GitLabError(
                "未配置 GitLab 凭据，请在 .env 设置 GITLAB_TOKEN 或 "
                "GITLAB_USERNAME/GITLAB_PASSWORD"
            )

    # ---------- API 封装 ----------
    def _get(self, path: str, params: Dict[str, Any] | None = None) -> Any:
        self._require_auth()
        url = urljoin(self.base_url + "/", path.lstrip("/"))
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except RequestException as e:
            raise GitLabError(f"请求 GitLab 失败: {e}") from e
        if resp.status_code == 404:
            raise GitLabError("GitLab 资源不存在 (404)")
        if resp.status_code == 401:
            raise GitLabError("GitLab 认证失败 (401)，请检查 token/账号密码")
        if not resp.ok:
            raise GitLabError(f"GitLab 返回 {resp.status_code}: {resp.text[:500]}")
        if not resp.text.strip():
            return None
        try:
            return resp.json()
        except ValueError as e:
            raise GitLabError(f"GitLab 响应非 JSON: {e}") from e

    # ---------- 公共方法 ----------
    def list_projects(self, search: str, per_page: int = 20) -> List[Dict[str, Any]]:
        """按关键词搜索项目（含 namespace/path）。"""
        params = {"search": search, "per_page": per_page}
        data = self._get("search", params)
        items = data or []
        # /search 返回混合类型；项目带 http_url_to_repo / path_with_namespace
        projects = [
            p for p in items if isinstance(p, dict) and (p.get("http_url_to_repo") or p.get("path_with_namespace"))
        ]
        return projects

    def search_code(self, query: str, scope: str | None = None, per_page: int = 20) -> List[Dict[str, Any]]:
        """搜索代码 blob。project_id 缺失时按配置限定到搜索根 group 下。"""
        params: Dict[str, Any] = {"search": query, "per_page": per_page}
        if GITLAB_SEARCH_ROOT_ID:
            params["project_id"] = GITLAB_SEARCH_ROOT_ID
        if GITLAB_SEARCH_ROOT_GROUP and not GITLAB_SEARCH_ROOT_ID:
            params["group_id"] = GITLAB_SEARCH_ROOT_GROUP
        data = self._get("search", params)
        results = [r for r in (data or []) if isinstance(r, dict) and r.get("type") == "blob"]
        # 限定到搜索根目录树下（按 path_with_namespace 前缀过滤）
        if GITLAB_SEARCH_ROOT_GROUP and not GITLAB_SEARCH_ROOT_ID:
            results = [
                r for r in results
                if (r.get("path_with_namespace") or "").startswith(GITLAB_SEARCH_ROOT_GROUP)
            ]
        return results

    def get_file(self, project_id: str, path: str, ref: str = "master") -> str:
        """读取仓库文件原始内容（文本）。"""
        self._require_auth()
        url = (
            f"{self.base_url}/projects/{quote(str(project_id), safe='')}/repository/files/"
            f"{quote(path, safe='')}/raw"
        )
        try:
            resp = self.session.get(url, params={"ref": ref}, timeout=self.timeout)
        except RequestException as e:
            raise GitLabError(f"请求 GitLab 文件失败: {e}") from e
        if resp.status_code == 404:
            raise GitLabError("文件不存在 (404)")
        if not resp.ok:
            raise GitLabError(f"GitLab 返回 {resp.status_code}: {resp.text[:300]}")
        return resp.text

    def list_tree(
        self,
        project_id: str,
        path: str = "",
        ref: str = "master",
        recursive: bool = False,
        per_page: int = 100,
    ) -> List[Dict[str, Any]]:
        """列出仓库目录树。"""
        params = {
            "path": path,
            "ref": ref,
            "recursive": str(bool(recursive)).lower(),
            "per_page": per_page,
        }
        return (
            self._get(
                f"projects/{quote(str(project_id), safe='')}/repository/tree", params
            )
            or []
        )

    def list_branches(self, project_id: str, per_page: int = 50) -> List[Dict[str, Any]]:
        """列出仓库分支。"""
        return (
            self._get(
                f"projects/{quote(str(project_id), safe='')}/repository/branches",
                {"per_page": per_page},
            )
            or []
        )
