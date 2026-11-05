"""Nexus 中心仓库（Maven 私服等）只读访问客户端。

安全约定（与 es.py 对齐的只读铁律）：
1. 仅调用 Nexus REST 只读端点（/service/rest/v1/repositories、/search、/search/assets）
   以及 GET 下载构件；严禁任何 POST/PUT/DELETE/上传/删除仓库/修改配置操作。
2. 下载 URL 必须落在已配置的 NEXUS_BASE_URL 之下（防 SSRF），路径参数做字符白名单与
   目录穿越校验；下载流式落盘并强制大小上限 NEXUS_MAX_DOWNLOAD_MB。

未配置 NEXUS_BASE_URL 时，get_client() 抛 NexusError，由 server 层转为统一错误提示
（提示维护者按 .env.example 的「Nexus 中心仓库」段补齐地址与账号密码）。
"""
from __future__ import annotations

import hashlib
import re
import warnings
from pathlib import Path
from urllib.parse import unquote, urlparse

import requests
import urllib3

from .config import (
    NEXUS_BASE_URL,
    NEXUS_DEFAULT_REPOSITORY,
    NEXUS_DOWNLOAD_DIR,
    NEXUS_MAX_DOWNLOAD_MB,
    NEXUS_PASSWORD,
    NEXUS_TIMEOUT,
    NEXUS_USERNAME,
    NEXUS_VERIFY_SSL,
)


class NexusError(Exception):
    """连接、认证或安全约束失败。status_code 为 HTTP 状态码（连接类错误为 None）。"""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


# 仓库名 / 相对路径白名单（防注入与目录穿越）
_REPO_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_ASSET_PATH_RE = re.compile(r"^[A-Za-z0-9._/@+-]+$")
# 下载文件名允许的扩展名（可执行脚本等一律拒绝；如需扩展再显式追加）
_ALLOWED_DOWNLOAD_SUFFIXES = {
    ".jar", ".war", ".ear", ".pom", ".xml", ".json", ".txt", ".md", ".asc",
    ".sha1", ".sha256", ".sha512", ".md5", ".module", ".zip", ".tar", ".gz",
    ".tgz", ".bz2", ".xz", ".gem", ".whl", ".tar.gz", ".tar.bz2", ".tgz.asc",
    ".yml", ".yaml", ".properties", ".html", ".css", ".js", ".ts", ".vue",
    ".maven-metadata.xml",
}


def get_client() -> "NexusClient":
    """构造 NexusClient；未配置地址时抛 NexusError 并给出补齐提示。"""
    if not NEXUS_BASE_URL:
        raise NexusError(
            "未配置 Nexus 地址：请在 zhenyun-pangu-mcp/.env 按 .env.example 的"
            "「Nexus 中心仓库」段补齐 NEXUS_BASE_URL / NEXUS_USERNAME / NEXUS_PASSWORD。"
            "未配置时请改用本地 Maven 仓库（如 ~/maven/Repository）或让用户人工查询。"
        )
    auth = (NEXUS_USERNAME, NEXUS_PASSWORD) if NEXUS_USERNAME else None
    return NexusClient(
        base_url=NEXUS_BASE_URL, auth=auth,
        verify_ssl=NEXUS_VERIFY_SSL, timeout=NEXUS_TIMEOUT,
    )


def default_repository() -> str:
    """返回 .env 配置的默认仓库名（可为空，由调用方决定是否必填）。"""
    return NEXUS_DEFAULT_REPOSITORY


class NexusClient:
    def __init__(self, base_url: str, auth=None,
                 verify_ssl: bool = True, timeout: int = 30):
        if not base_url:
            raise NexusError("NEXUS_BASE_URL 为空，无法连接 Nexus。")
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        if not verify_ssl:
            # 自签名证书内网场景：verify_ssl=false 时静默告警，避免刷屏
            warnings.filterwarnings(
                "ignore", category=urllib3.exceptions.InsecureRequestWarning
            )
        self.session = requests.Session()
        # 容错：用户常把 NEXUS_BASE_URL 填成仓库路径（…/repository/<name>），
        # /service/rest/v1 端点必须挂在 Nexus 服务根地址上，这里自动归一化；
        # 同时把该仓库名推导为默认仓库（显式 NEXUS_DEFAULT_REPOSITORY 优先）。
        self.service_root = self._service_root(self.base_url)
        match = re.match(r"^.*/repository/([^/]+)/?$", urlparse(self.base_url).path)
        self.base_repo = match.group(1) if match else ""

    def default_repository(self) -> str:
        """默认仓库：显式 NEXUS_DEFAULT_REPOSITORY 优先，其次取 NEXUS_BASE_URL 中的仓库名。"""
        return NEXUS_DEFAULT_REPOSITORY or self.base_repo

    @staticmethod
    def _service_root(base_url: str) -> str:
        parsed = urlparse(base_url)
        match = re.match(r"^(?P<prefix>.*)/repository/[^/]+/?$", parsed.path)
        prefix = match.group("prefix") if match else parsed.path
        return f"{parsed.scheme}://{parsed.netloc}{prefix.rstrip('/')}"

    # ---------- 底层 GET ----------
    def _request(self, method: str, path: str,
                 params: dict | None = None, **kwargs) -> requests.Response:
        url = f"{self.service_root}{path}"
        try:
            resp = self.session.request(
                method, url, params=params, auth=self.auth,
                verify=self.verify_ssl, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise NexusError(f"Nexus 请求失败: {e}") from e
        if resp.status_code in (401, 403):
            raise NexusError(
                f"Nexus 认证/授权失败（HTTP {resp.status_code}）：请检查 "
                f"NEXUS_USERNAME / NEXUS_PASSWORD 是否正确、账号是否有该仓库读权限。",
                status_code=resp.status_code,
            )
        if resp.status_code == 404:
            raise NexusError(f"Nexus 资源不存在（404）：{path}", status_code=404)
        if resp.status_code >= 400:
            raise NexusError(
                f"Nexus 返回 HTTP {resp.status_code}: {resp.text[:500]}",
                status_code=resp.status_code,
            )
        return resp

    def _get(self, path: str, params: dict | None = None) -> requests.Response:
        return self._request("GET", path, params)

    def _get_json(self, path: str, params: dict | None = None) -> dict | list:
        resp = self._get(path, params)
        try:
            return resp.json()
        except ValueError as e:
            raise NexusError(f"Nexus 返回非 JSON：{resp.text[:500]}") from e

    # ---------- 只读能力 ----------
    def list_repositories(self) -> list[dict]:
        """列出所有仓库及类型/格式。"""
        payload = self._get_json("/service/rest/v1/repositories")
        if not isinstance(payload, list):
            raise NexusError("Nexus /repositories 返回结构异常")
        return [
            {
                "name": r.get("name"),
                "format": r.get("format"),
                "type": r.get("type"),
                "url": r.get("url"),
            }
            for r in payload
        ]

    @staticmethod
    def _search_params(
        q: str, repository: str, group: str, name: str, version: str,
        extension: str, classifier: str, continuation_token: str,
    ) -> dict:
        params: dict = {}
        if q:
            params["q"] = q
        if repository:
            params["repository"] = repository
        if group:
            params["group"] = group
        if name:
            params["name"] = name
        if version:
            params["version"] = version
        if extension:
            params["maven.extension"] = extension
        if classifier:
            params["maven.classifier"] = classifier
        if continuation_token:
            params["continuationToken"] = continuation_token
        return params

    @staticmethod
    def _slim_component(item: dict) -> dict:
        assets = [
            {
                "id": a.get("id"),
                "path": a.get("path"),
                "download_url": a.get("downloadUrl"),
                "size": a.get("size"),
                "last_modified": a.get("lastModified"),
            }
            for a in (item.get("assets") or [])
        ]
        return {
            "id": item.get("id"),
            "group": item.get("group"),
            "name": item.get("name"),
            "version": item.get("version"),
            "repository": item.get("repository"),
            "assets": assets,
        }

    @staticmethod
    def _is_access_denied(e: "NexusError") -> bool:
        """账号对 /search、/search/assets 无 nx-search-read 权限时（401/403）允许回退。"""
        return e.status_code in (401, 403)

    def _fallback_repository(self, repository: str) -> str:
        repo = (repository or "").strip() or self.default_repository()
        if not repo:
            raise NexusError(
                "当前账号无 /search、/search/assets 权限，已尝试回退 /components、/assets 端点；"
                "该回退必须显式指定 repository（或配置 NEXUS_DEFAULT_REPOSITORY）。"
            )
        return repo

    def search(
        self,
        q: str = "",
        repository: str = "",
        group: str = "",
        name: str = "",
        version: str = "",
        extension: str = "",
        classifier: str = "",
        limit: int = 50,
        continuation_token: str = "",
    ) -> dict:
        """搜索组件（分页聚合到 limit 条；返回 items 与下一页 continuation_token）。

        账号对 /search 端点无权限（401/403）时自动回退 /components 端点 + 客户端过滤。
        """
        items: list[dict] = []
        token = continuation_token
        try:
            for _ in range(10):  # 单次最多翻 10 页，防失控
                params = self._search_params(
                    q, repository, group, name, version, extension,
                    classifier, token,
                )
                payload = self._get_json("/service/rest/v1/search", params)
                if not isinstance(payload, dict):
                    raise NexusError("Nexus /search 返回结构异常")
                for item in payload.get("items") or []:
                    items.append(self._slim_component(item))
                token = payload.get("continuationToken") or ""
                if len(items) >= int(limit) or not token:
                    break
        except NexusError as e:
            if not self._is_access_denied(e):
                raise
            return self._search_components_fallback(
                q, repository, group, name, version, limit, continuation_token,
            )
        items = items[: int(limit)]
        return {"items": items, "count": len(items),
                "continuation_token": token or None}

    def _search_components_fallback(
        self,
        q: str, repository: str, group: str, name: str, version: str,
        limit: int, continuation_token: str,
    ) -> dict:
        """无 /search 权限时的回退：/components 分页 + 客户端过滤。"""
        repo = self._fallback_repository(repository)
        params: dict = {"repository": repo}
        keyword = (q or name or "").strip()
        if keyword:
            params["q"] = keyword
        items: list[dict] = []
        token = continuation_token
        for _ in range(10):
            if token:
                params["continuationToken"] = token
            payload = self._get_json("/service/rest/v1/components", params)
            for item in payload.get("items") or []:
                if group and (item.get("group") or "") != group:
                    continue
                if name and (item.get("name") or "") != name:
                    continue
                if version and (item.get("version") or "") != version:
                    continue
                items.append(self._slim_component(item))
            token = payload.get("continuationToken") or ""
            if len(items) >= int(limit) or not token:
                break
        items = items[: int(limit)]
        return {
            "items": items, "count": len(items),
            "continuation_token": token or None,
            "endpoint_fallback": "components",
        }

    def search_assets(
        self,
        q: str = "",
        repository: str = "",
        group: str = "",
        name: str = "",
        version: str = "",
        extension: str = "",
        classifier: str = "",
        limit: int = 50,
        continuation_token: str = "",
    ) -> dict:
        """按资产（文件）粒度搜索，返回可下载的 download_url。

        账号对 /search/assets 端点无权限（401/403）时自动回退 /assets 端点 + 客户端过滤。
        """
        assets: list[dict] = []
        token = continuation_token
        try:
            for _ in range(10):
                params = self._search_params(
                    q, repository, group, name, version, extension,
                    classifier, token,
                )
                payload = self._get_json("/service/rest/v1/search/assets", params)
                if not isinstance(payload, dict):
                    raise NexusError("Nexus /search/assets 返回结构异常")
                for item in payload.get("items") or []:
                    comp = item.get("component") or {}
                    assets.append({
                        "id": item.get("id"),
                        "path": item.get("path"),
                        "download_url": item.get("downloadUrl"),
                        "repository": item.get("repository"),
                        "size": item.get("size"),
                        "last_modified": item.get("lastModified"),
                        "group": comp.get("group"),
                        "name": comp.get("name"),
                        "version": comp.get("version"),
                    })
                token = payload.get("continuationToken") or ""
                if len(assets) >= int(limit) or not token:
                    break
        except NexusError as e:
            if not self._is_access_denied(e):
                raise
            return self._search_assets_fallback(
                q, repository, name, version, extension, limit, continuation_token,
            )
        assets = assets[: int(limit)]
        return {"assets": assets, "count": len(assets),
                "continuation_token": token or None}

    def _search_assets_fallback(
        self,
        q: str, repository: str, name: str, version: str, extension: str,
        limit: int, continuation_token: str,
    ) -> dict:
        """无 /search/assets 权限时的回退：/assets 分页 + 客户端过滤。"""
        repo = self._fallback_repository(repository)
        params: dict = {"repository": repo}
        keyword = (q or name or "").strip()
        if keyword:
            params["q"] = keyword
        assets: list[dict] = []
        token = continuation_token
        for _ in range(10):
            if token:
                params["continuationToken"] = token
            payload = self._get_json("/service/rest/v1/assets", params)
            for item in payload.get("items") or []:
                path = item.get("path") or ""
                if name and name not in path:
                    continue
                if version and version not in path:
                    continue
                if extension and not path.endswith(f".{extension.lstrip('.')}"):
                    continue
                assets.append({
                    "id": item.get("id"),
                    "path": path,
                    "download_url": item.get("downloadUrl"),
                    "repository": item.get("repository"),
                    "size": item.get("size"),
                    "last_modified": item.get("lastModified"),
                    "group": None,
                    "name": None,
                    "version": None,
                })
            token = payload.get("continuationToken") or ""
            if len(assets) >= int(limit) or not token:
                break
        assets = assets[: int(limit)]
        return {
            "assets": assets, "count": len(assets),
            "continuation_token": token or None,
            "endpoint_fallback": "assets",
        }

    # ---------- Maven GAV 直读（无需浏览权限，按坐标拼路径探测） ----------
    @staticmethod
    def _maven_rel_path(group: str, name: str, version: str,
                        extension: str = "pom", classifier: str = "") -> str:
        group_path = group.strip().replace(".", "/")
        name = name.strip()
        version = version.strip()
        extension = extension.strip().lstrip(".")
        classifier = classifier.strip()
        rel = f"{group_path}/{name}/{version}/{name}-{version}"
        if classifier:
            rel += f"-{classifier}"
        rel += f".{extension}"
        if ".." in rel or not _ASSET_PATH_RE.fullmatch(rel):
            raise NexusError("group/name/version 拼出的路径包含非法字符")
        return rel

    def check_maven_artifact(
        self,
        group: str,
        name: str,
        version: str,
        repository: str = "",
        extensions: str = "pom,jar",
        classifier: str = "",
    ) -> dict:
        """按 Maven GAV 直读探测各扩展名构件是否存在（HEAD，不落盘）。

        适用账号只有仓库直读权限、无 REST 浏览权限的场景。
        """
        repo = (repository or "").strip() or self.default_repository()
        if not _REPO_NAME_RE.fullmatch(repo):
            raise NexusError("repository 名包含非法字符")
        snapshot = None
        if version.strip().upper().endswith("-SNAPSHOT"):
            snapshot = self._resolve_snapshot(repo, group, name, version)
        results = {}
        for ext in [e.strip().lstrip(".") for e in extensions.split(",") if e.strip()]:
            rel = self._maven_rel_path(group, name, version, ext, classifier)
            actual, resolved_via = rel, "direct"
            if snapshot:
                entry = snapshot["values"].get((classifier or "", ext))
                if entry:
                    actual = f"{snapshot['dir']}/{entry}"
                    resolved_via = "snapshot-metadata"
            try:
                resp = self._request("HEAD", f"/repository/{repo}/{actual}")
                status, headers = resp.status_code, resp.headers
            except NexusError as e:
                if e.status_code != 404:
                    raise
                status, headers = 404, {}
            results[ext] = {
                "exists": status == 200,
                "path": actual,
                "resolved_via": resolved_via,
                "content_length": headers.get("content-length"),
                "last_modified": headers.get("last-modified"),
            }
        return {"repository": repo, "group": group.strip(), "name": name.strip(),
                "version": version.strip(), "classifier": classifier or None,
                "snapshot_metadata_used": bool(snapshot),
                "artifacts": results}

    def _resolve_snapshot(self, repo: str, group: str, name: str, version: str) -> dict | None:
        """解析 -SNAPSHOT 版本目录的 maven-metadata.xml，返回真实带时间戳文件名映射。

        返回 {"dir": 版本目录相对路径, "values": {(classifier, extension): 真实文件名}}；
        元数据不存在（404）时返回 None，调用方回退直读文件名探测。
        """
        group_path = group.strip().replace(".", "/")
        rel_dir = f"{group_path}/{name.strip()}/{version.strip()}"
        try:
            resp = self._get(f"/repository/{repo}/{rel_dir}/maven-metadata.xml")
        except NexusError as e:
            if e.status_code == 404:
                return None
            raise
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise NexusError(f"snapshot maven-metadata.xml 解析失败: {e}") from e
        values: dict[tuple[str, str], str] = {}
        for sv in root.findall("./versioning/snapshotVersions/snapshotVersion"):
            value = sv.findtext("value")
            extension = (sv.findtext("extension") or "jar").strip()
            classifier = (sv.findtext("classifier") or "").strip()
            if value:
                filename = f"{name.strip()}-{value}"
                if classifier:
                    filename += f"-{classifier}"
                values[(classifier, extension)] = f"{filename}.{extension}"
        return {"dir": rel_dir, "values": values}

    def get_maven_metadata(self, group: str, name: str, repository: str = "") -> dict:
        """直读 maven-metadata.xml，返回可用版本列表/latest/release（无需浏览权限）。"""
        repo = (repository or "").strip() or self.default_repository()
        if not _REPO_NAME_RE.fullmatch(repo):
            raise NexusError("repository 名包含非法字符")
        group_path = group.strip().replace(".", "/")
        rel = f"{group_path}/{name.strip()}/maven-metadata.xml"
        if ".." in rel or not _ASSET_PATH_RE.fullmatch(rel):
            raise NexusError("group/name 拼出的路径包含非法字符")
        resp = self._get(f"/repository/{repo}/{rel}")
        import xml.etree.ElementTree as ET
        try:
            root = ET.fromstring(resp.text)
        except ET.ParseError as e:
            raise NexusError(f"maven-metadata.xml 解析失败: {e}") from e
        versioning = root.find("versioning") or {}
        versions = [v.text for v in root.findall("./versioning/versions/version") if v.text]
        return {
            "repository": repo,
            "group": group.strip(),
            "name": name.strip(),
            "metadata_path": rel,
            "latest": root.findtext("./versioning/latest"),
            "release": root.findtext("./versioning/release"),
            "last_updated": root.findtext("./versioning/lastUpdated"),
            "version_count": len(versions),
            "versions": versions,
        }

    # ---------- 受控下载（仅落本地，不写仓库） ----------
    def download(
        self,
        asset_url: str = "",
        repository: str = "",
        asset_path: str = "",
        dest_dir: str = "",
        filename: str = "",
    ) -> dict:
        """下载单个构件到本地目录；asset_url 必须在配置的 Nexus 域名之下。"""
        if asset_url:
            parsed = urlparse(asset_url)
            base = urlparse(self.service_root)
            if parsed.netloc.lower() != base.netloc.lower() or (
                base.path and not parsed.path.startswith(base.path)
            ):
                raise NexusError(
                    "⛔ 拒绝下载：asset_url 不在已配置的 NEXUS_BASE_URL 域名/路径之下（防 SSRF）。"
                )
            if parsed.scheme.lower() != base.scheme.lower():
                # Nexus 常返回 http:// 的 downloadUrl；同域名时统一升级为服务根协议
                asset_url = (
                    f"{base.scheme}://{parsed.netloc}{parsed.path}"
                    + (f"?{parsed.query}" if parsed.query else "")
                )
            remote_path = unquote(urlparse(asset_url).path)
        else:
            repo = (repository or "").strip() or self.default_repository()
            rel = (asset_path or "").strip()
            if not _REPO_NAME_RE.fullmatch(repo):
                raise NexusError("repository 名包含非法字符")
            if not rel or ".." in rel or not _ASSET_PATH_RE.fullmatch(rel):
                raise NexusError("asset_path 非法：不能为空、含 .. 或白名单外字符")
            remote_path = f"/repository/{repo}/{rel}"
            asset_url = f"{self.service_root}{remote_path}"

        name = (filename or "").strip() or unquote(remote_path.rsplit("/", 1)[-1])
        if not name or ".." in name or "/" in name:
            raise NexusError("filename 非法")
        suffix = Path(name).suffix.lower()
        if suffix == ".xml" and name.endswith("maven-metadata.xml"):
            suffix = ".maven-metadata.xml"
        if suffix not in _ALLOWED_DOWNLOAD_SUFFIXES:
            raise NexusError(
                f"⛔ 拒绝下载：扩展名 '{suffix or '(无)'}' 不在允许列表"
                f"（防下载可执行/脚本类文件），如确有需要请在 nexus.py 显式追加。"
            )

        target_dir = Path(dest_dir).expanduser() if dest_dir else Path(NEXUS_DOWNLOAD_DIR)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / name
        if not target.resolve().parent == target_dir.resolve():
            raise NexusError("下载目标路径解析异常，已拒绝")

        max_bytes = NEXUS_MAX_DOWNLOAD_MB * 1024 * 1024
        total = 0
        sha = hashlib.sha256()
        try:
            with self.session.get(
                asset_url, auth=self.auth, verify=self.verify_ssl,
                timeout=self.timeout, stream=True,
            ) as resp:
                if resp.status_code in (401, 403):
                    raise NexusError(
                        f"Nexus 认证/授权失败（HTTP {resp.status_code}）："
                        f"请检查账号对该仓库的读权限。"
                    )
                if resp.status_code == 404:
                    raise NexusError(f"构件不存在（404）：{remote_path}")
                if resp.status_code >= 400:
                    raise NexusError(
                        f"Nexus 返回 HTTP {resp.status_code}: {resp.text[:300]}"
                    )
                with open(target, "wb") as fh:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > max_bytes:
                            raise NexusError(
                                f"下载超过大小上限 {NEXUS_MAX_DOWNLOAD_MB}MB，已中止。"
                                "如确需下载更大构件，请调大 .env 中 NEXUS_MAX_DOWNLOAD_MB。"
                            )
                        sha.update(chunk)
                        fh.write(chunk)
        except NexusError:
            target.unlink(missing_ok=True)
            raise
        except (requests.RequestException, OSError) as e:
            target.unlink(missing_ok=True)
            raise NexusError(f"Nexus 下载失败: {e}") from e

        return {
            "file": str(target),
            "size_bytes": total,
            "sha256": sha.hexdigest(),
            "asset_url": asset_url,
        }
