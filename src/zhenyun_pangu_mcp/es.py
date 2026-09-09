"""正式环境 ES 只读查询（整合自 es-prod，仅保留只读能力）。

安全铁律（任何场景不得违反，client 层强制）：
1. 严禁任何写操作：DELETE / PUT / POST 到 _doc/_update/_delete_by_query/_bulk 等；
   只允许 _search / _mget / _count / _search_template / _explain / _field_caps / _terms 等只读端点。
2. 每次查询 size 强制 <= ES_MAX_SIZE（默认 100），超出会被截断并提示。

未配置 ES_BASE_URL 时，ESClient 构造即抛 ESSafetyError，由 server 层转为
统一错误提示（提示维护者按 .env.example 的「正式环境 ES」段补齐）。
"""
from __future__ import annotations

import json
from urllib.parse import urljoin

import requests

from .config import ES_MAX_SIZE, ES_VERIFY_SSL, get_es_profile


class ESSafetyError(Exception):
    """违反只读 / 限量约束。"""


# 允许的只读端点后缀（命中即放行，其余一律拒绝）
READONLY_ENDPOINTS = (
    "_search",
    "_mget",
    "_count",
    "_search_template",
    "_explain",
    "_field_caps",
    "_terms",
    "_validate",
    "_search_shards",
    "_rank_eval",
)


def _check_readonly(path: str) -> None:
    """校验请求的 ES 端点是否为只读。

    规则：
    - 拒绝任何含 _delete / _update / _bulk / _create / _doc 写语义的路径。
    - 仅放行 READONLY_ENDPOINTS 中的只读端点。
    """
    p = path.strip().lstrip("/").lower()

    # 显式拒绝写语义关键字
    danger = (
        "_delete", "_update", "_bulk", "_create", "_doc", "_reindex",
        "_ingest", "_scripts", "_aliases?",
    )
    for d in danger:
        if d in p:
            raise ESSafetyError(
                f"⛔ 写操作被禁止：检测到路径含 '{d}'。本工具严禁删除/更新/写入 ES 数据。"
            )

    # 必须落在只读端点内
    if not any(p.endswith(e) or f"/{e}" in p or p == e for e in READONLY_ENDPOINTS):
        hit = any(e in p.split("?")[0] for e in READONLY_ENDPOINTS)
        if not hit:
            raise ESSafetyError(
                f"⛔ 仅允许只读端点（{', '.join(READONLY_ENDPOINTS)}）。"
                f"路径 '{path}' 不在允许列表，已拒绝。"
            )


def _parse_dsl(dsl: str) -> dict:
    """将 DSL 文本解析为 dict；空字符串视为 match_all。"""
    s = (dsl or "").strip()
    if not s:
        return {"query": {"match_all": {}}}
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError as e:
        raise ESSafetyError(f"DSL 不是合法 JSON：{e}")
    return parsed if isinstance(parsed, dict) else {"query": parsed}


def get_client(env: str = "prod") -> "ESClient":
    """按环境构造 ESClient；未配置该环境链接或环境名非法时抛 ESSafetyError。"""
    try:
        profile = get_es_profile(env)
    except RuntimeError as e:
        raise ESSafetyError(str(e))
    if not profile["base_url"]:
        raise ESSafetyError(
            f"未配置 ES 链接（环境 {env}）：请在 zhenyun-pangu-mcp/.env 按 "
            f".env.example 的「正式环境 ES」段补齐对应变量"
            f"（prod: ES_BASE_URL / dev: ES_DEV_BASE_URL / test: ES_TEST_BASE_URL）。"
            f"未配置时跳过 ES 核查，改用库表侧证据推理；"
            f"或生成 DSL 让用户在 Kibana Dev Tools 人工查询后贴回结果。"
        )
    auth = (profile["username"], profile["password"]) if profile["username"] else None
    return ESClient(base_url=profile["base_url"], auth=auth, verify_ssl=ES_VERIFY_SSL)


class ESClient:
    def __init__(self, base_url: str, auth=None,
                 verify_ssl: bool = ES_VERIFY_SSL, timeout: int = 30):
        if not base_url:
            raise ESSafetyError("ES_BASE_URL 为空，无法连接 ES。")
        self.base_url = base_url.rstrip("/")
        self.auth = auth
        self.verify_ssl = verify_ssl
        self.timeout = timeout
        self.session = requests.Session()

    # ---------- 只读查询 ----------
    def search(self, index: str, dsl: str = "", size: int = 10) -> dict:
        """在指定索引上执行 _search（只读，强制 size<=ES_MAX_SIZE）。

        index: 索引名（可为逗号分隔多索引或通配，如 swbh_*）
        dsl:   查询 DSL JSON 字符串（含 query / sort / aggs 等）；空则 match_all
        size:  期望返回条数，最终会被截断到 ES_MAX_SIZE
        """
        path = f"{index}/_search"
        _check_readonly(path)

        body = _parse_dsl(dsl)
        eff_size = min(max(int(size or 10), 1), ES_MAX_SIZE)
        truncated = False
        if "size" in body:
            try:
                if int(body["size"]) > ES_MAX_SIZE:
                    truncated = True
                body["size"] = min(int(body["size"]), ES_MAX_SIZE)
            except (TypeError, ValueError):
                body["size"] = eff_size
        else:
            body["size"] = eff_size

        url = urljoin(self.base_url + "/", path)
        try:
            resp = self.session.post(
                url, json=body, auth=self.auth, verify=self.verify_ssl,
                timeout=self.timeout,
            )
        except requests.RequestException as e:
            raise ESSafetyError(f"ES 查询请求失败: {e}")

        if resp.status_code >= 400:
            raise ESSafetyError(f"ES 返回 HTTP {resp.status_code}: {resp.text[:500]}")

        try:
            payload = resp.json()
        except ValueError:
            raise ESSafetyError(f"ES 返回非 JSON：{resp.text[:500]}")

        info = {
            "took_ms": payload.get("took"),
            "timed_out": payload.get("timed_out"),
            "total_shards": (payload.get("_shards", {}) or {}).get("total"),
            "returned": 0,
            "truncated_to_max": truncated,
            "hits": [],
            "aggregations": payload.get("aggregations") or payload.get("aggs"),
            "raw_error": payload.get("error"),
        }
        hits = (payload.get("hits", {}) or {}).get("hits", []) or []
        info["returned"] = len(hits)
        for h in hits:
            info["hits"].append({
                "_index": h.get("_index"),
                "_id": h.get("_id"),
                "_score": h.get("_score"),
                "_source": h.get("_source"),
            })
        return info

    def count(self, index: str, dsl: str = "") -> dict:
        """在指定索引上执行 _count（只读，不受 size 限制，仅返回数量）。"""
        path = f"{index}/_count"
        _check_readonly(path)
        body = _parse_dsl(dsl)
        url = urljoin(self.base_url + "/", path)
        try:
            resp = self.session.post(url, json=body, auth=self.auth,
                                     verify=self.verify_ssl, timeout=self.timeout)
        except requests.RequestException as e:
            raise ESSafetyError(f"ES count 请求失败: {e}")
        if resp.status_code >= 400:
            raise ESSafetyError(f"ES 返回 HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            payload = resp.json()
        except ValueError:
            raise ESSafetyError(f"ES 返回非 JSON：{resp.text[:500]}")
        return {"count": payload.get("count"), "error": payload.get("error")}

    def get(self, index: str, doc_id: str) -> dict:
        """按 _id 获取单个文档（只读，用 _source 端点规避写语义拦截）。"""
        path = f"{index}/_source/{doc_id}"
        _check_readonly(f"{index}/_search")  # 借用只读校验
        url = urljoin(self.base_url + "/", path)
        try:
            resp = self.session.get(url, auth=self.auth,
                                    verify=self.verify_ssl, timeout=self.timeout)
        except requests.RequestException as e:
            raise ESSafetyError(f"ES get 请求失败: {e}")
        if resp.status_code == 404:
            return {"found": False, "_id": doc_id}
        if resp.status_code >= 400:
            raise ESSafetyError(f"ES 返回 HTTP {resp.status_code}: {resp.text[:500]}")
        try:
            src = resp.json()
        except ValueError:
            raise ESSafetyError(f"ES 返回非 JSON：{resp.text[:500]}")
        return {"found": True, "_id": doc_id, "_source": src}


# ---------------------------------------------------------------------------
# 结果格式化（供 server 层工具拼装可读文本）
# ---------------------------------------------------------------------------

def _fmt_search(info: dict) -> str:
    lines = []
    lines.append(
        f"took={info.get('took_ms')}ms timed_out={info.get('timed_out')} "
        f"total_shards={info.get('total_shards')} returned={info.get('returned')}"
    )
    if info.get("truncated_to_max"):
        lines.append(f"⚠️ 结果已被截断到上限 {ES_MAX_SIZE} 条（原请求 size 超出）。")
    if info.get("raw_error"):
        lines.append(f"⚠️ ES error: {info['raw_error']}")
    lines.append("")

    if info.get("aggregations"):
        lines.append("aggregations:")
        lines.append(json.dumps(info["aggregations"], ensure_ascii=False, default=str))

    hits = info.get("hits") or []
    if not hits:
        lines.append("（无命中文档）")
        return "\n".join(lines)

    lines.append(f"--- 命中文档（最多 {ES_MAX_SIZE} 条）---")
    for h in hits:
        lines.append(f"[{h.get('_index')}] _id={h.get('_id')} score={h.get('_score')}")
        lines.append(json.dumps(h.get("_source") or {}, ensure_ascii=False, default=str))
        lines.append("")
    return "\n".join(lines)
