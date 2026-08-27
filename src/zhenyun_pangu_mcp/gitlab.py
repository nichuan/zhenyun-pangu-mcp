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
        # GitLab REST API 统一挂在 /api/v4 下（base_url 仅到站点根，不含版本前缀）
        self.api_base = f"{self.base_url}/api/v4"
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
        url = urljoin(self.api_base + "/", path.lstrip("/"))
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
        params = {"scope": "projects", "search": search, "per_page": per_page}
        data = self._get("search", params)
        items = data or []
        # /search 返回混合类型；项目带 http_url_to_repo / path_with_namespace
        projects = [
            p for p in items if isinstance(p, dict) and (p.get("http_url_to_repo") or p.get("path_with_namespace"))
        ]
        return projects

    def search_code(self, query: str, scope: str | None = None, per_page: int = 20) -> List[Dict[str, Any]]:
        """搜索代码 blob。

        注意：自托管 GitLab（当前 open-gitlab.going-link.com 为 14.1.0）若未开启
        全局代码搜索索引（Elasticsearch / Gitaly 搜索后端），根 ``/search?scope=blobs``
        会返回 400 ``scope does not have a valid value``，``gitlab_search_code`` 因此
        **暂时无法使用**，需系统管理员开启平台代码搜索功能后方可恢复。

        兜底策略：当全局 /search 失败时，自动退化为「仓库级搜索」——
        遍历 GITLAB_SEARCH_ROOT_GROUP 下的各 project，逐个调用
        ``GET /projects/:id/repository/search?scope=blobs``（该接口不依赖全局索引，
        14.1.0 可用），从而仍可按关键词在源码中查找代码，仅速度较慢且需指定 group。
        """
        params: Dict[str, Any] = {
            "scope": scope or "blobs",
            "search": query,
            "per_page": per_page,
        }
        if GITLAB_SEARCH_ROOT_ID:
            params["project_id"] = GITLAB_SEARCH_ROOT_ID
        if GITLAB_SEARCH_ROOT_GROUP and not GITLAB_SEARCH_ROOT_ID:
            params["group_id"] = GITLAB_SEARCH_ROOT_GROUP
        try:
            data = self._get("search", params)
        except GitLabError:
            # 全局搜索不可用（平台未开启代码索引）→ 走仓库级搜索兜底
            return self._search_code_fallback(query, per_page)
        results = [r for r in (data or []) if isinstance(r, dict) and r.get("type") == "blob"]
        # 限定到搜索根目录树下（按 path_with_namespace 前缀过滤）
        if GITLAB_SEARCH_ROOT_GROUP and not GITLAB_SEARCH_ROOT_ID:
            results = [
                r for r in results
                if (r.get("path_with_namespace") or "").startswith(GITLAB_SEARCH_ROOT_GROUP)
            ]
        return results

    def _search_code_fallback(self, query: str, per_page: int = 20) -> List[Dict[str, Any]]:
        """仓库级搜索兜底：遍历搜索根 group 下的各 project，使用 repository/search。

        仅在全局 /search?scope=blobs 不可用（平台未开启代码索引）时调用。
        返回结构对齐全局搜索的 blob 结果。

        注意：当前自托管 GitLab（14.1.0）连 repository/search 接口也未提供
        （任意 scope 均 404），此时无法做按关键词检索，会抛出清晰错误提示改用
        gitlab_list_tree + gitlab_get_file 逐层定位查看代码。
        """
        if not GITLAB_SEARCH_ROOT_GROUP:
            return []
        # 1) 解析搜索根 group 的数字 id（先按数字，再按路径查）
        group_id = GITLAB_SEARCH_ROOT_GROUP
        if not group_id.isdigit():
            try:
                g = self._get(f"groups/{quote(group_id, safe='')}")
                group_id = str(g.get("id", group_id))
            except GitLabError:
                raise GitLabError(
                    "GitLab 代码搜索不可用：全局 /search 未开启，且无法解析搜索根 group "
                    f"「{GITLAB_SEARCH_ROOT_GROUP}」。请改用 gitlab_list_tree + gitlab_get_file 查看代码。"
                )
        # 2) 列出该 group 下的项目（含子 group，递归）
        try:
            projects = (
                self._get(
                    f"groups/{quote(group_id, safe='')}/projects",
                    {"per_page": 100, "include_subgroups": "true"},
                )
                or []
            )
        except GitLabError:
            projects = []
        if not projects:
            raise GitLabError(
                "GitLab 代码搜索不可用：搜索根 group 下无可见项目。请改用 gitlab_list_tree "
                "+ gitlab_get_file 查看代码。"
            )
        # 3) 探测 repository/search 在该实例是否可用（用首个项目试 blobs scope）
        first_pid = projects[0].get("id")
        try:
            probe = self._get(
                f"projects/{quote(str(first_pid), safe='')}/repository/search",
                {"scope": "blobs", "search": query, "per_page": 1},
            )
            if probe is None:
                raise GitLabError("n/a")
        except GitLabError:
            raise GitLabError(
                "GitLab 代码搜索功能暂不可用：当前实例（自托管 14.1.0）未开启全局代码搜索索引，"
                "且 repository/search 接口亦不存在（任意 scope 均 404）。此功能需系统管理员在 "
                "GitLab 平台开启代码搜索后方可使用。当前查看代码请改用方案一：gitlab_list_tree "
                "逐层定位目录 + gitlab_get_file 读取文件（或直接用 search_repo 搜本地已拉取的仓库）。"
            )
        # 4) repository/search 可用：遍历各 project 检索
        out: List[Dict[str, Any]] = []
        seen = set()
        for p in projects:
            pid = p.get("id")
            if not pid:
                continue
            try:
                blobs = self._get(
                    f"projects/{quote(str(pid), safe='')}/repository/search",
                    {"scope": "blobs", "search": query, "per_page": per_page},
                )
            except GitLabError:
                continue
            for b in blobs or []:
                key = (pid, b.get("path"), b.get("startline"), b.get("data"))
                if key in seen:
                    continue
                seen.add(key)
                out.append(
                    {
                        "project_id": pid,
                        "path_with_namespace": p.get("path_with_namespace"),
                        "path": b.get("path"),
                        "filename": b.get("filename"),
                        "startline": b.get("startline"),
                        "data": b.get("data"),
                    }
                )
        return out

    def get_file(self, project_id: str, path: str, ref: str = "master") -> str:
        """读取仓库文件原始内容（文本）。"""
        self._require_auth()
        url = (
            f"{self.api_base}/projects/{quote(str(project_id), safe='')}/repository/files/"
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
