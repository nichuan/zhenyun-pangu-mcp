"""阿里云 SLS(日志服务)签名查询客户端 —— 纯标准库,零依赖。

复刻自 log-ops-mcp,用于查询 cn 国内盘古 prod(阿里云日志)的日志;非生产环境日志在 Loki。
官方对接方式:HTTP 请求带 LOG 签名头(x-log-apiversion=0.6.0, hmac-sha1)。
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import json
import re
import urllib.error
import urllib.parse
import urllib.request


def _date_header() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%a, %d %b %Y %H:%M:%S GMT")


def _sign(method: str, uri: str, params: dict[str, str], headers: dict[str, str], secret: str) -> str:
    signed_headers = sorted(
        (k.lower(), str(v).strip()) for k, v in headers.items()
        if k.lower().startswith("x-log-") or k.lower().startswith("x-acs-")
    )
    header_text = "\n".join(f"{k}:{v}" for k, v in signed_headers)
    query = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
    message = "\n".join([
        method,
        headers.get("Content-MD5", ""),
        headers.get("Content-Type", ""),
        headers.get("Date", ""),
        header_text,
        f"{uri}?{query}" if query else uri,
    ])
    return base64.b64encode(hmac.new(secret.encode(), message.encode(), hashlib.sha1).digest()).decode()


def _clean(value):
    if not isinstance(value, str):
        return value
    return re.sub(r"[\x00-\x08\x0b-\x0c\x0e-\x1f\x7f]", " ", value)


def normalize_log(log: dict) -> dict:
    contents = log.get("contents", [])
    if contents:
        return {_clean(item.get("Key", "")): _clean(item.get("Value", "")) for item in contents}
    return {key: _clean(value) for key, value in log.items()}


def query_sls(project: str, logstore: str, ak_id: str, ak_secret: str, query: str,
              from_time: int, to_time: int, endpoint: str, line: int = 200) -> tuple[list[dict], str]:
    uri = f"/logstores/{logstore}"
    params = {
        "type": "log", "query": query, "from": str(from_time), "to": str(to_time),
        "line": str(max(1, min(line, 500))), "offset": "0", "reverse": "false", "powerSql": "false",
    }
    headers = {
        "Host": f"{project}.{endpoint}", "Date": _date_header(),
        "x-log-apiversion": "0.6.0", "x-log-signaturemethod": "hmac-sha1",
        "x-log-bodyrawsize": "0", "Accept": "application/json",
    }
    headers["Authorization"] = f"LOG {ak_id}:{_sign('GET', uri, params, headers, ak_secret)}"
    url = f"https://{project}.{endpoint}{uri}?{urllib.parse.urlencode(params)}"
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
            logs = payload if isinstance(payload, list) else payload.get("logs", [])
            return [normalize_log(log) for log in logs], response.headers.get("x-log-progress", "Complete")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"SLS HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        raise RuntimeError(f"SLS 查询失败: {exc}") from exc


def _key(log: dict) -> tuple:
    return (log.get("__time__", ""), log.get("_container_name_", ""), str(log.get("content", ""))[:100])


def query_trace(project: str, logstore: str, ak_id: str, ak_secret: str, trace_id: str,
                namespace: str, from_time: int, to_time: int, endpoint: str, line: int = 200) -> tuple[list[dict], str]:
    error_query = f'"{trace_id}" AND _namespace_: {namespace} AND (level: ERROR OR level: WARN)'
    full_query = f'"{trace_id}" AND _namespace_: {namespace}'
    errors, p1 = query_sls(project, logstore, ak_id, ak_secret, error_query, from_time, to_time, endpoint, line)
    full, p2 = query_sls(project, logstore, ak_id, ak_secret, full_query, from_time, to_time, endpoint, line)
    merged, seen = [], set()
    for log in errors + full:
        key = _key(log)
        if key not in seen:
            seen.add(key)
            merged.append(log)
    def time_key(item: dict) -> int:
        try:
            return int(item.get("__time__", 0))
        except (TypeError, ValueError):
            return 0

    merged.sort(key=time_key)
    return merged, ("Incomplete" if "Incomplete" in (p1, p2) else "Complete")
