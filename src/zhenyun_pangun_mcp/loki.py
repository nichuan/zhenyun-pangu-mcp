"""Loki 日志客户端（复刻 pg-aws-log 的 GrafanaClient）。

通过 Grafana proxy 查询 Loki 日志（LogQL）。支持双平台：
  - aws：AWS 海外（jp-saas-1），URL 默认 logs.jp-saas-1.going-link.net
  - cn ：国内公有云（logs.going-link.net），非 prod 已切换至此

认证流程（Grafana v11）：
  1. POST {base}/login  body: {"user","password"} -> Set-Cookie: grafana_session
  2. 后续请求携带 Cookie: grafana_session
  3. GET /api/datasources 动态发现 Loki 数据源（UID）

性能设计（根治超时 + 最大化复用登录）：
  - 进程内模块级缓存 _CLIENTS：同一进程内多次工具调用只登录一次。
  - 跨进程持久化：登录后把 grafana_session + 到期时间写入本地缓存文件
    （~/.cache/zhenyun_pangun_mcp_grafana.json），MCP 每次启动（stdout 重新拉起）
    先加载复用，仅在 session 过期/无效时重新登录，进一步减少登录次数。
  - 失效自动重登：查询/发现数据源返回 401/403 时，清除缓存并强制重新登录后重试一次。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from pathlib import Path

import requests

from .config import LOKI_PLATFORMS, LOKI_DATASOURCES


class LokiError(Exception):
    """Loki 查询/认证错误。"""

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# 进程内缓存：platform -> GrafanaClient。进程内复用 session 与 cookie。
_CLIENTS: dict[str, "GrafanaClient"] = {}
# 登录/重登互斥锁：避免多线程同时触发重登（401/403 失效风暴）。
_LOGIN_LOCK = threading.Lock()

# 跨进程持久化 cookie 缓存文件（session 有效期内复用，避免每次启动重复登录）。
_COOKIE_CACHE = Path(
    os.getenv("LOKI_COOKIE_CACHE") or str(Path.home() / ".cache" / "zhenyun_pangun_mcp_grafana.json")
)


def _load_cached_cookie() -> dict:
    """从本地缓存文件读取持久化的登录态 {platform: {cookie, expiry}}。"""
    try:
        if _COOKIE_CACHE.exists():
            return json.loads(_COOKIE_CACHE.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        pass
    return {}


def _save_cached_cookie(data: dict) -> None:
    """把登录态写回本地缓存文件，供下次进程复用。"""
    try:
        _COOKIE_CACHE.parent.mkdir(parents=True, exist_ok=True)
        _COOKIE_CACHE.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


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
        self._expiry = 0  # epoch 秒；0 表示未知
        self._uid_cache: dict[str, str] = {}

    # ------------------------------------------------------------------
    # 登录态管理
    # ------------------------------------------------------------------
    def _restore_from_cache(self) -> bool:
        """从持久化缓存恢复 cookie；若未过期则直接复用，避免重新登录。"""
        cached = _load_cached_cookie().get(self.platform)
        if not cached:
            return False
        cookie = cached.get("cookie") or ""
        expiry = int(cached.get("expiry") or 0)
        if not cookie:
            return False
        if expiry and expiry <= int(time.time()):
            return False  # 已过期，重新登录
        self._cookie = cookie
        self._expiry = expiry
        self.session.headers.update({"Cookie": cookie})
        return True

    def login(self) -> None:
        """确保已登录：先复用进程内/持久化 cookie；仅在需要时重新登录。

        双重检查 + 全局锁：已登录时零锁开销直接返回；需要登录/重登时加锁，
        避免多线程并发触发多次 POST /login。
        """
        # 快速路径：已有未过期 cookie 直接复用
        if self._cookie and (not self._expiry or self._expiry > int(time.time())):
            return

        with _LOGIN_LOCK:
            # 进入锁后再查一次，防止持锁等待期间已被其他线程登录好
            if self._cookie and (not self._expiry or self._expiry > int(time.time())):
                return
            if self._cookie:
                # 已知过期，清除后重新登录
                self._cookie = ""
                self._expiry = 0
                self.session.headers.pop("Cookie", None)

            if self._restore_from_cache():
                return

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
            cookie_header = resp.headers.get("Set-Cookie", "")
            m = re.search(r"grafana_session=([^;]+)", cookie_header)
            if not m:
                raise LokiError("Grafana 登录后未获取到 grafana_session cookie")
            self._cookie = f"grafana_session={m.group(1)}"
            # 解析到期时间（grafana_session_expiry=<epoch>）
            em = re.search(r"grafana_session_expiry=(\d+)", cookie_header)
            self._expiry = int(em.group(1)) if em else 0
            self.session.headers.update({"Cookie": self._cookie})
            self._persist()

    def _persist(self) -> None:
        """把当前平台的登录态写入持久化缓存。"""
        data = _load_cached_cookie()
        data[self.platform] = {"cookie": self._cookie, "expiry": self._expiry}
        _save_cached_cookie(data)

    def _force_relogin(self) -> None:
        """强制清除登录态并重新登录（供 401/403 失效时调用）。"""
        self._cookie = ""
        self._expiry = 0
        self.session.headers.pop("Cookie", None)
        self._uid_cache.clear()
        data = _load_cached_cookie()
        data.pop(self.platform, None)
        _save_cached_cookie(data)
        self.login()

    # ------------------------------------------------------------------
    # 请求（带 401/403 自动重登一次）
    # ------------------------------------------------------------------
    def discover_loki_datasources(self, name: str | None = None) -> list[dict]:
        resp = self.session.get(f"{self.base_url}/api/datasources", timeout=30)
        if resp.status_code in (401, 403):
            self._force_relogin()
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

    def resolve_uid(self, ds_name: str) -> str:
        """取数据源 UID（带缓存，避免每次查询重复 /api/datasources）。"""
        if ds_name in self._uid_cache:
            return self._uid_cache[ds_name]
        ds = self.discover_loki_datasources(ds_name)
        if not ds:
            raise LokiError(f"未找到数据源 {ds_name}")
        uid = ds[0]["uid"]
        self._uid_cache[ds_name] = uid
        return uid

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
        resp = self.session.get(url, params=params, timeout=120)
        if resp.status_code in (401, 403):
            # session 失效：重登后重试一次，避免一次失败就返回给用户
            self._force_relogin()
            resp = self.session.get(url, params=params, timeout=120)
        if resp.status_code != 200:
            raise LokiError(f"Loki 查询失败: HTTP {resp.status_code}", resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise LokiError(f"Loki 非 JSON 响应: {resp.text[:200]}") from e


def _get_client(platform: str) -> GrafanaClient:
    """取（并缓存）指定平台的已登录 GrafanaClient，跨进程/调用复用，最大化减少登录。"""
    if platform not in _CLIENTS:
        _CLIENTS[platform] = GrafanaClient(platform)
    client = _CLIENTS[platform]
    client.login()
    return client


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


def _parse_streams(resp: dict) -> list[dict]:
    """把 Loki query_range 的 result 扁平化为 [{ts_ns, line}] 列表。"""
    rows: list[dict] = []
    for stream in resp.get("data", {}).get("result", []):
        for ts_ns, line in stream.get("values", []):
            rows.append({"ts_ns": int(ts_ns), "line": line})
    return rows


def query_trace(
    platform: str,
    env: str,
    trace_id: str,
    start: int,
    end: int,
    limit: int = 200,
    direction: str = "BACKWARD",
    level: str = "all",
    clip_len: int = 600,
) -> tuple[list[dict], dict]:
    """按 traceId 查 Loki 全链路日志（单查询优先，最多 2 次 HTTP，根治超时）。

    设计要点（与「用户手册」页面用法的区别）：
    - 手册是给人在 Grafana **页面**手工选 namespace/app 缩小范围；本函数直接调
      Loki HTTP API，query 就是 LogQL 字符串。
    - traceId 直接按正文子串匹配（覆盖 `[xxx]` / `traceId=xxx` / `trace_id: xxx`），
      不写死字段前缀，避免"写死格式 -> 0 结果 -> 反复试"。
    - 性能：不再单独发 ERROR/WARN 查询（full 结果里本就包含 ERROR/WARN 行，可在
      meta 里标注），一次 HTTP 拿全链路。仅在带 namespace 限定返回 0 时，才不限
      namespace 重查一次（适配 namespace 推导偏差/多租户混部）。即最多 2 次 HTTP。
    - 防 TOKEN 膨胀：`level` 支持 all/error/warn，只返回命中级别行；`clip_len`
      裁剪每条 line，避免完整 JSON 在结果里传输。

    返回 (rows, meta)，rows 已去重、按时间排序、裁剪，并可能已按 level 过滤。
    """
    ds_name = resolve_datasource(platform, env)
    client = _get_client(platform)
    uid = client.resolve_uid(ds_name)

    # 主匹配用 LogQL 的 `|=` 简单子串包含（绝对兼容 RE2，日志正文 traceId 多为
    # `[xxx]` 方括号包裹，子串匹配即可命中）。之前用 `|~` 正则（含 lookahead
    # `(?=\W|$)` 与内联标志 `(?i:...)`），RE2 引擎不支持会导致 Loki 返回 HTTP 400，
    # 表现为 nonprod 下 obs_log_trace 持续报错。这里改为纯子串匹配根治。
    tid = trace_id

    ns = _ns_from_env(platform, env)
    # 优先：带 namespace 限定（扫描范围小、快）
    scoped_full = f'{{namespace="{ns}"}} |= "{tid}"'
    # 兜底：不限定 namespace（带 ns 查询为 0 时自动降级一次，避免无限回退）
    unscoped_full = f'{{}} |= "{tid}"'

    full_rows = _parse_streams(client.loki_query_range(uid, scoped_full, start, end, limit, direction))
    fallback = False
    if not full_rows:
        fallback = True
        full_rows = _parse_streams(client.loki_query_range(uid, unscoped_full, start, end, limit, direction))

    merged = list({(r["ts_ns"], r["line"]): r for r in full_rows}.values())
    merged.sort(key=lambda r: r["ts_ns"], reverse=(direction == "BACKWARD"))

    # 全量命中行数（拉取可能因 limit 截断，故 raw_total 仅代表已拉取部分）
    raw_total = len(merged)
    # 按级别统计（基于完整行内容，裁剪前）。
    # ⚠️ 级别从日志正文的 content 里识别，不能对整行 JSON 用 \berror\b——那会把
    # `error=null`、字段名 error 等误判为 ERROR 行。真实 content 格式通常为
    # "2026-08-19 11:38:15.185  INFO 1 --- [...] msg"，级别是大写标记。
    def _line_lvl(line: str) -> str | None:
        # 从 content 中提取日志级别标记（大写，前后为空白/[]/数字等分隔）
        m = re.search(r"\b(ERROR|WARN|INFO|DEBUG|TRACE)\b", line)
        return m.group(1).upper() if m else None

    all_rows = list(merged)
    err_rows = [r for r in all_rows if _line_lvl(r["line"]) == "ERROR"]
    warn_rows = [r for r in all_rows if _line_lvl(r["line"]) in ("WARN", "ERROR")]

    # 按 level 过滤：error=ERROR级、warn=WARN+ERROR级、all=全部
    level_l = (level or "all").strip().lower()
    if level_l == "error":
        out_rows = err_rows
    elif level_l == "warn":
        out_rows = list({(r["ts_ns"], r["line"]): r for r in (err_rows + warn_rows)}.values())
    else:  # all
        out_rows = all_rows
    out_rows.sort(key=lambda r: r["ts_ns"], reverse=(direction == "BACKWARD"))

    # 行内容裁剪：避免完整 JSON 行（长 SQL/大对象）撑爆 token
    clip = max(200, min(int(clip_len), 5000))
    clipped = [{**r, "line": r["line"][:clip]} for r in out_rows]

    used_full = unscoped_full if fallback else scoped_full
    meta = {
        "platform": platform,
        "env": env,
        "datasource": ds_name,
        "trace_id": trace_id,
        "match_mode": "substring (|=)",
        "level_filter": level_l,
        "namespace_scoped": not fallback,
        "namespace_fallback": fallback,
        "full_query": used_full,
        "clip_len": clip,
        "raw_total": raw_total,
        "error_count": len(err_rows),
        "warn_count": len(warn_rows),
        "total": len(out_rows),
        "truncated": raw_total >= limit,  # 拉满 limit 说明可能还有更多
        "hint": (
            ("带 namespace 限定查询为 0，已自动降级为不限 namespace 重查并命中。" if fallback
             else "命中（按 namespace 限定）。")
            + (" 命中行数已达 limit，结果可能被截断；如需更多可调大 limit。" if raw_total >= limit else "")
            + (f" 当前只返回 {level_l.upper()} 级别日志；如需全量请用 level=all。" if level_l != "all" else "")
        ),
    }
    return clipped, meta


def _ns_from_env(platform: str, env: str) -> str:
    """由平台+环境推导 Loki 的 namespace 标签值（与 obs_log_query 约定一致）。

    关键区别（手册是页面用法，MCP 是 API 用法）：
    - 手册里「test 环境」在代码层面对应 env="nonprod" + namespace="saas-test-new"，
      并不是 Loki 的 env 取值（LOKI_DATASOURCES 的 cn 只有 prod/nonprod/ops）。
    - 故这里 env=nonprod -> saas-test-new；prod -> saas-prod；ops -> 空(不限定)。
    - aws 的 namespace 与本仓盘古不同，env 直转（nonprod/prod/ops）由调用方决定。
    仅作 trace 查询的默认 namespace；若实际 namespace 不同，用 obs_log_query 显式指定。
    """
    if platform == "cn":
        return {"nonprod": "saas-test-new", "prod": "saas-prod"}.get(env, "")
    return ""


def warn_unscoped(query: str) -> str | None:
    """检查 LogQL query 是否未限定 namespace/service_name/pod 任一维度。

    MCP 直接调 API，范围过大（跨所有 namespace）会导致查询慢或超时。
    返回告警文案；若已限定则返回 None。注意：空流选择器 `{}` 视为未限定。

    判定规则：标签必须出现在流选择器 `{}` 内（如 {namespace="x"}）才算"已限定"；
    仅正文里含 "namespace=" 字样（如 |= "namespace=saas-test-new"）不算，因为那是
    行内文本过滤而非范围缩小，仍会扫描全部数据流。
    """
    import re
    if not query or query.strip() == "{}":
        return "query 为空/空选择器，将扫描全部 namespace，范围过大、查询慢，建议加 namespace/service_name/pod 过滤"
    # 提取每个 { ... } 流选择器块，检查其中是否含标签
    selectors = re.findall(r"\{([^}]*)\}", query)
    labels = ("namespace=", "service_name=", "pod=", "container=", "app=")
    scoped = any(any(lbl in sel for lbl in labels) for sel in selectors)
    if not scoped:
        return (
            "query 未限定 namespace/service_name/pod 任一维度，将扫描全部数据流，"
            "范围过大、查询慢且易超时。建议至少加一个标签过滤，如 {namespace=\"saas-test-new\"}"
        )
    return None
