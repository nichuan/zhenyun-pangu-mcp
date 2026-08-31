"""Loki 查询增强单元测试（query_trace / 范围告警 / namespace 推导 / 登录缓存与失效重登）。

注意：这些测试针对 loki.py 的纯函数与「mock 掉 Grafana 网络请求」后的逻辑，
不涉及真实 Loki/Grafana（真实查日志需 .env 凭据，不在单测范围）。
"""
import os
import sys

import pytest

# 将 src 加入 import 路径（未在环境安装时）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import loki  # noqa: E402


# ---------------------------------------------------------------------------
# warn_unscoped：范围过大告警
# ---------------------------------------------------------------------------
def test_warn_unscoped_empty():
    assert loki.warn_unscoped("") is not None
    assert loki.warn_unscoped("{}") is not None


def test_warn_unscoped_no_label():
    # 正文中含 namespace 字样但不作为标签 -> 仍判为未限定
    msg = loki.warn_unscoped('|= "namespace=saas-test-new"')
    assert msg is not None


def test_warn_unscoped_with_label_ok():
    assert loki.warn_unscoped('{namespace="saas-test-new"}') is None
    assert loki.warn_unscoped('{service_name="srm-order"} |= "x"') is None
    assert loki.warn_unscoped('{pod="abc-123"}') is None
    assert loki.warn_unscoped('{app="srm-gateway"}') is None


# ---------------------------------------------------------------------------
# _ns_from_env：平台+环境 -> namespace 标签值
# ---------------------------------------------------------------------------
def test_ns_from_env_aws_passthrough():
    # Loki 现仅服务 AWS 海外：其 namespace 无法由 env 推导（返回空 -> 不限定查询）。
    # 国内盘古已迁回阿里云 SLS，namespace 由 sls_config 按环境给出（见 test_sls_routing.py）。
    assert loki._ns_from_env("aws", "nonprod") == ""
    assert loki._ns_from_env("aws", "prod") == ""
    assert loki._ns_from_env("aws", "ops") == ""


# ---------------------------------------------------------------------------
# _parse_streams：Loki result -> rows
# ---------------------------------------------------------------------------
def test_parse_streams():
    resp = {
        "data": {
            "result": [
                {"values": [("1700000000000000000", "lineA"), ("1700000001000000000", "lineB")]},
                {"values": [("1700000002000000000", "lineC")]},
            ]
        }
    }
    rows = loki._parse_streams(resp)
    assert [r["line"] for r in rows] == ["lineA", "lineB", "lineC"]
    assert rows[0]["ts_ns"] == 1700000000000000000


# ---------------------------------------------------------------------------
# query_trace：单查询 + 去重 + error_count + namespace 降级（mock 客户端）
# ---------------------------------------------------------------------------
def _monkeypatch_client(monkeypatch, handler):
    """把 loki._get_client 替换为返回 handler 的替身（handler 拦截 loki_query_range）。

    handler(uid, query, start, end, limit, direction) -> list[(ts, line)]
    """
    class FakeClient:
        def __init__(self):
            self._handler = handler
            self._queries = []
        def login(self):
            pass
        def resolve_uid(self, ds_name):
            return "ds-uid"
        def loki_query_range(self, uid, query, start, end, limit, direction):
            self._queries.append(query)
            lines = self._handler(query)
            return {"status": "success", "data": {"result": [{"values": [(str(ts), ln) for ts, ln in lines]}]}}

    fake = FakeClient()
    monkeypatch.setattr(loki, "_get_client", lambda platform: fake)
    return fake


def test_query_trace_single_query_dedup_sort(monkeypatch):
    # 单查询全量 3 条（含 1 条 ERROR），不再单独发 error 查询
    def handler(query):
        return [
            (1000, "traceId=ABC service=gateway"),
            (2000, "traceId=ABC ERROR something failed"),
            (3000, "traceId=ABC service=order"),
        ]

    fake = _monkeypatch_client(monkeypatch, handler)
    rows, meta = loki.query_trace("aws", "nonprod", "ABC", 0, 9_999_999_999, limit=50, direction="BACKWARD")

    # 只发 1 次 HTTP（单查询），去重后 3 条；BACKWARD 按时间倒序
    assert len(fake._queries) == 1
    assert len(rows) == 3
    assert meta["error_count"] == 1
    assert meta["total"] == 3
    assert rows[0]["ts_ns"] == 3000  # 倒序第一条时间最大
    assert all("traceId=ABC" in r["line"] for r in rows)


def test_query_trace_no_namespace_filter_for_aws(monkeypatch):
    # aws 无默认 namespace：只发 1 次不限定查询，不空跑带 ns 的那一次
    captured = []
    def handler(query):
        captured.append(query)
        return []
    fake = _monkeypatch_client(monkeypatch, handler)
    loki.query_trace("aws", "nonprod", "XYZ", 0, 1, limit=10)
    assert len(fake._queries) == 1
    assert "namespace=" not in captured[0]
    assert "XYZ" in captured[0]


def test_query_trace_fallback_unscoped_when_empty(monkeypatch):
    # 带 ns 查询为 0 时，自动降级为不限 namespace 重查一次（共 2 次 HTTP）
    # （用 monkeypatch 造出一个「有默认 ns」的平台，验证降级分支本身）
    monkeypatch.setattr(loki, "_ns_from_env", lambda platform, env: "saas-test-new")
    calls = []
    def handler(query):
        calls.append(query)
        if 'namespace="saas-test-new"' in query:
            return []
        return [(1000, "traceId=EFG found-unscoped")]
    fake = _monkeypatch_client(monkeypatch, handler)
    rows, meta = loki.query_trace("aws", "nonprod", "EFG", 0, 9_999_999_999, limit=50)
    assert len(calls) == 2  # 最多 2 次 HTTP
    assert meta["namespace_fallback"] is True
    assert len(rows) == 1
    assert "found-unscoped" in rows[0]["line"]


def test_query_trace_resolve_error():
    # 未知环境 -> 抛 LokiError（不发起网络请求）。LOKI_DATASOURCES 现仅剩 aws 的键。
    for env in ("no-such-env", "cn", ""):
        with pytest.raises(loki.LokiError):
            loki.query_trace("aws", env, "ABC", 0, 1)


def test_query_any_stream_skips_rejected_selector(monkeypatch):
    # Loki 拒绝 `{}`（400）时，自动换下一个候选选择器（如 {app=~".+"}），不整体失败
    queries, rejections = [], {"n": 0}

    class RejectingClient:
        def resolve_uid(self, ds_name):
            return "ds-uid"

        def loki_query_range(self, uid, query, start, end, limit, direction):
            queries.append(query)
            if query.startswith("{}"):
                rejections["n"] += 1
                raise loki.LokiError("empty stream selector", status=400)
            return {"status": "success", "data": {"result": [{"values": [(str(1000), "traceId=K found")]}]}}

    monkeypatch.setattr(loki, "_get_client", lambda platform: RejectingClient())
    rows, meta = loki.query_trace("aws", "nonprod", "K", 0, 9_999_999_999, limit=50)
    assert rejections["n"] == 1  # `{}` 被拒一次
    assert len(rows) == 1 and "found" in rows[0]["line"]
    assert meta["unscoped_fallback"] is True
    assert meta["full_query"].startswith('{app=~".+"}')  # 实际生效的选择器


def test_query_any_stream_non400_raises(monkeypatch):
    # 非 400（如 502 后端故障）不换选择器，直接上抛
    class BrokenClient:
        def resolve_uid(self, ds_name):
            return "ds-uid"

        def loki_query_range(self, uid, query, start, end, limit, direction):
            raise loki.LokiError("bad gateway", status=502)

    monkeypatch.setattr(loki, "_get_client", lambda platform: BrokenClient())
    with pytest.raises(loki.LokiError) as exc:
        loki.query_trace("aws", "nonprod", "K", 0, 1)
    assert exc.value.status == 502


def test_query_trace_level_filter(monkeypatch):
    # level=error 只返回 ERROR 级；error=null 等字段名不应误判为 ERROR 行
    lines = [
        (1000, "2026-08-19 11:00:00.000  INFO 1 --- [a] [tid] msg info error=null"),
        (2000, "2026-08-19 11:00:01.000  WARN 1 --- [a] [tid] something warn"),
        (3000, "2026-08-19 11:00:02.000 ERROR 1 --- [a] [tid] boom"),
        (4000, "2026-08-19 11:00:03.000 DEBUG 1 --- [a] [tid] trace error=null"),
    ]
    def handler(query):
        return lines
    fake = _monkeypatch_client(monkeypatch, handler)

    rows_e, meta_e = loki.query_trace("aws", "nonprod", "tid", 0, 9_999_999_999, limit=50, level="error")
    assert meta_e["total"] == 1
    assert meta_e["error_count"] == 1
    assert "boom" in rows_e[0]["line"]

    rows_w, meta_w = loki.query_trace("aws", "nonprod", "tid", 0, 9_999_999_999, limit=50, level="warn")
    assert meta_w["total"] == 2  # WARN + ERROR
    assert meta_w["warn_count"] == 2


def test_query_trace_clip_len(monkeypatch):
    # clip_len 裁剪每条 line；error=null 不是 ERROR 行
    def handler(query):
        return [(1000, "x" * 5000)]
    _monkeypatch_client(monkeypatch, handler)
    rows, meta = loki.query_trace("aws", "nonprod", "tid", 0, 1, limit=50, clip_len=300)
    assert len(rows[0]["line"]) == 300
    assert meta["clip_len"] == 300


def test_query_trace_truncated_flag(monkeypatch):
    # 命中行数达到 limit 时 truncated=True
    def handler(query):
        return [(1000 + i, f"line {i}") for i in range(50)]
    _monkeypatch_client(monkeypatch, handler)
    _, meta = loki.query_trace("aws", "nonprod", "tid", 0, 1, limit=50)
    assert meta["truncated"] is True
    assert meta["raw_total"] == 50


# ---------------------------------------------------------------------------
# 登录缓存：持久化往返 / 过期重登 / 401 自动重登
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status_code, headers=None, body=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._body = body

    def json(self):
        return self._body


class _FakeSession:
    """可编程的 Session 替身，记录请求并返回预设响应。"""

    def __init__(self, login_resp, api_sequence):
        self.login_resp = login_resp
        self.api_sequence = list(api_sequence)  # 每次 /api/datasources 消费一个
        self.headers = {}
        self.login_calls = 0

    def post(self, url, json=None, allow_redirects=True, timeout=None):
        self.login_calls += 1
        return self.login_resp

    def get(self, url, params=None, timeout=None):
        if url.endswith("/api/datasources"):
            if self.api_sequence:
                return self.api_sequence.pop(0)
            return _FakeResp(401)
        return _FakeResp(401)


def _make_client(monkeypatch, login_resp, api_seq, username="u", password="p"):
    c = loki.GrafanaClient("aws")
    c.username = username
    c.password = password
    c.session = _FakeSession(login_resp, api_seq)
    return c


def test_login_persists_cookie_and_restores(monkeypatch):
    # 持久化缓存读写用内存 dict 模拟
    store = {}
    monkeypatch.setattr(loki, "_load_cached_cookie", lambda: store)
    monkeypatch.setattr(loki, "_save_cached_cookie", lambda d: store.update(d))

    login_resp = _FakeResp(
        200,
        headers={
            "Set-Cookie": "grafana_session=abc123; grafana_session_expiry=9999999999; Path=/"
        },
    )
    c = _make_client(monkeypatch, login_resp, [])
    c.login()
    assert c.session.login_calls == 1
    assert c._cookie == "grafana_session=abc123"
    assert store.get("aws", {}).get("cookie") == "grafana_session=abc123"

    # 再次 login 不重新 POST（复用已缓存 cookie）
    c.login()
    assert c.session.login_calls == 1

    # 新进程：新建 client，从 store 恢复 cookie，不再 POST /login
    c2 = _make_client(monkeypatch, login_resp, [])
    c2.login()
    assert c2._cookie == "grafana_session=abc123"
    assert c2.session.login_calls == 0  # 未发 /login


def test_login_relogin_when_cookie_expired(monkeypatch):
    import time
    store = {}
    monkeypatch.setattr(loki, "_load_cached_cookie", lambda: store)
    monkeypatch.setattr(loki, "_save_cached_cookie", lambda d: store.update(d))

    # 过期 cookie：expiry 已过
    store["aws"] = {"cookie": "grafana_session=old", "expiry": int(time.time()) - 100}
    login_resp = _FakeResp(
        200,
        headers={"Set-Cookie": "grafana_session=new123; grafana_session_expiry=9999999999; Path=/"},
    )
    c = _make_client(monkeypatch, login_resp, [])
    c.login()
    # 过期 -> 重新登录 -> 新 cookie
    assert c._cookie == "grafana_session=new123"
    assert c.session.login_calls == 1


def test_query_range_relogin_on_401(monkeypatch):
    import time
    store = {}
    monkeypatch.setattr(loki, "_load_cached_cookie", lambda: store)
    monkeypatch.setattr(loki, "_save_cached_cookie", lambda d: store.update(d))

    login_resp = _FakeResp(
        200,
        headers={"Set-Cookie": "grafana_session=abc; grafana_session_expiry=9999999999; Path=/"},
    )
    # 第一次查询返回 401（session 失效），重登后第二次返回成功
    c = _make_client(monkeypatch, login_resp, [])
    c.session._uid_ok = True

    class S(_FakeSession):
        def __init__(self, login_resp):
            super().__init__(login_resp, [])
            self.query_hits = []
        def get(self, url, params=None, timeout=None):
            if url.endswith("/loki/api/v1/query_range"):
                self.query_hits.append(params["query"])
                if len(self.query_hits) == 1:
                    return _FakeResp(401)  # 首次失效
                return _FakeResp(200, body={"status": "success", "data": {"result": []}})
            return _FakeResp(401)

    c.session = S(login_resp)
    c.login()
    assert c.session.login_calls == 1

    # 手动走 loki_query_range 校验 401 自动重登
    c._uid_cache["nonprod"] = "uid-1"
    resp = c.loki_query_range("uid-1", '{namespace="saas-test-new"} |= "x"', 0, 1, 10, "BACKWARD")
    assert resp["status"] == "success"
    # 401 触发了一次重登（login 从 1 -> 2）
    assert c.session.login_calls == 2
