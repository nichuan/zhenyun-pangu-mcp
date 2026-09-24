"""SLS 路由与时间解析单元测试。

覆盖：
- 国内公有云盘古 prod + 非生产 dev/test 的 SLS(project/logstore/namespace)映射；
- 系统/环境别名归一化与非法环境的错误提示；
- Loki 仅剩 aws（cn 已下线的回归保护）；
- 统一时间解析（中英文相对时间 / 自然语言 / 绝对时间）；
- obs_sls_query 的 0 命中自动扩窗（mock 掉 SLS HTTP 与凭据）。
"""
import json
import os
import sys
import time

import pytest

# 将 src 加入 import 路径（未在环境安装时）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import config, server, sls, sls_config  # noqa: E402


# ---------------------------------------------------------------------------
# 盘古环境 -> SLS 映射
# ---------------------------------------------------------------------------
def test_pangu_nonprod_targets():
    dev = sls_config.resolve_target("盘古", "dev")
    assert dev.project == "pangu-cn-saas-3-nonprod-shared-sls-project-0"
    assert dev.logstore == "sls-store-0-pangu-nonprod"
    assert dev.namespace == "saas-dev-new"
    assert dev.key_env_prefix == "PANGU_NONPROD"

    test = sls_config.resolve_target("盘古", "test")
    assert test.project == dev.project
    assert test.namespace == "saas-test-new"
    assert test.key_env_prefix == "PANGU_NONPROD"


def test_pangu_prod_target():
    prod = sls_config.resolve_target("盘古", "prod")
    assert prod.project == "pangu-cn-saas-3-prod-shared-sls-project-0"
    assert prod.logstore == "sls-store-0-pangu-prod"
    assert prod.namespace == "saas-prod"
    assert prod.key_env_prefix == "PANGU_PROD"


def test_system_and_env_aliases():
    # 系统别名
    assert sls_config.resolve_target("pangu", "test").system == "盘古"
    assert sls_config.resolve_target("盘古系统", "prod").system == "盘古"
    # 环境别名：口语化写法归一到真实环境键
    assert sls_config.resolve_target("盘古", "生产").environment == "prod"
    assert sls_config.resolve_target("盘古", "正式").environment == "prod"
    assert sls_config.resolve_target("盘古", "测试").environment == "test"
    assert sls_config.resolve_target("盘古", "DEV").environment == "dev"


def test_unsupported_env_lists_options():
    with pytest.raises(ValueError) as exc:
        sls_config.resolve_target("盘古", "no-such-env")
    message = str(exc.value)
    assert "盘古/dev" in message and "盘古/test" in message and "盘古/prod" in message


def test_supported_targets_covers_pangu_all_envs():
    combos = {(t["system"], t["environment"]) for t in sls_config.supported_targets()}
    assert {("盘古", "dev"), ("盘古", "test"), ("盘古", "prod")} <= combos
    # 不得泄露凭据前缀
    assert all("ACCESS_KEY" not in str(t) for t in sls_config.supported_targets())


# ---------------------------------------------------------------------------
# Loki 平台裁剪：仅 aws
# ---------------------------------------------------------------------------
def test_loki_platforms_only_aws():
    assert set(config.LOKI_PLATFORMS) == {"aws"}
    assert set(config.LOKI_DATASOURCES) == {"aws"}


def test_check_loki_region_rejects_cn_with_sls_hint():
    assert server._check_loki_region("aws") is None
    cn_err = server._check_loki_region("cn")
    assert cn_err and "obs_sls_query" in cn_err
    assert "cn" not in config.LOKI_PLATFORMS


# ---------------------------------------------------------------------------
# 统一时间解析（Loki 与 SLS 共用）
# ---------------------------------------------------------------------------
def test_time_bounds_relative_and_natural():
    now = int(time.time())
    start, end = server._time_bounds(None, None, "2h")
    assert end - start == 2 * 3600
    assert abs(end - now) <= 5

    start, end = server._time_bounds(None, None, "最近30分钟")
    assert end - start == 30 * 60

    start, end = server._time_bounds(None, None, "3d")
    assert end - start == 3 * 86400


def test_time_bounds_natural_day_ranges():
    start, end = server._time_bounds(None, None, "今天")
    now = int(time.time())
    assert start <= now <= end
    assert end - start <= 86400

    start, end = server._time_bounds(None, None, "昨天")
    assert end < int(time.time())
    assert end - start == 86400 - 1


def test_time_bounds_absolute_and_explicit():
    start, end = server._time_bounds(None, None, "2026-08-01 00:00~2026-08-01 12:00")
    assert end - start == 12 * 3600
    # 显式 from/to 优先：to 缺省时补 now，from 缺省时按 end-2h 补
    assert server._time_bounds(1000, 2000, "最近2小时") == (1000, 2000)
    start, end = server._time_bounds(1000, None, "")
    assert start == 1000 and abs(end - int(time.time())) <= 5
    start, end = server._time_bounds(None, 2000, "")
    assert end == 2000 and start == 2000 - 7200


def test_time_bounds_rejects_invalid_or_oversized_windows():
    with pytest.raises(ValueError, match="必须晚于"):
        server._validate_time_bounds(2000, 1000)
    with pytest.raises(ValueError, match="最多支持 31 天"):
        server._validate_time_bounds(0, server.MAX_LOG_QUERY_SPAN + 1)


def test_time_bounds_rejects_malformed_absolute_range():
    with pytest.raises(ValueError, match="绝对时间格式错误"):
        server._time_bounds(None, None, "2026-08-01 00:00~2026-08-01 01:00~extra")


# ---------------------------------------------------------------------------
# obs_sls_query：0 命中自动扩窗（mock 凭据与 SLS HTTP）
# ---------------------------------------------------------------------------
def test_obs_sls_query_auto_expand(monkeypatch):
    calls = []

    monkeypatch.setattr(sls_config, "credentials", lambda target: ("ak-id", "ak-secret"))

    def fake_query_sls(project, logstore, ak_id, ak_secret, query, from_time, to_time, endpoint, line):
        calls.append((from_time, to_time, query))
        # 前两次（2h / 24h）0 命中，第三次（72h）命中
        if len(calls) < 3:
            return [], "Complete"
        return [{"__time__": str(to_time), "content": "boom"}], "Complete"

    monkeypatch.setattr(sls, "query_sls", fake_query_sls)

    raw = server.obs_sls_query(environment="test", keyword='content: "boom"', limit=10)
    assert '"ok": true' in raw
    assert len(calls) == 3
    # 非生产环境：namespace 必须是 saas-test-new
    assert "_namespace_: saas-test-new" in calls[0][2]
    assert "level: ERROR" in calls[0][2]
    assert "saas-test-new" in raw
    # 自动扩窗：窗口逐步放大
    assert calls[0][1] - calls[0][0] < calls[1][1] - calls[1][0] < calls[2][1] - calls[2][0]


def test_obs_sls_query_respects_explicit_window(monkeypatch):
    calls = []
    monkeypatch.setattr(sls_config, "credentials", lambda target: ("ak-id", "ak-secret"))

    def fake_query_sls(project, logstore, ak_id, ak_secret, query, from_time, to_time, endpoint, line):
        calls.append((from_time, to_time))
        return [], "Complete"

    monkeypatch.setattr(sls, "query_sls", fake_query_sls)
    server.obs_sls_query(environment="dev", time_range="最近3天", limit=10)
    # 显式时间窗：不扩窗，只查一次
    assert len(calls) == 1
    assert calls[0][1] - calls[0][0] == 3 * 86400


def test_obs_sls_query_scopes_script_keyword_to_container(monkeypatch):
    calls = []
    monkeypatch.setattr(sls_config, "credentials", lambda target: ("ak-id", "ak-secret"))

    def fake_query_sls(project, logstore, ak_id, ak_secret, query, from_time, to_time, endpoint, line):
        calls.append(query)
        return [{"__time__": str(to_time), "content": "script failed"}], "Complete"

    monkeypatch.setattr(sls, "query_sls", fake_query_sls)
    raw = server.obs_sls_query(
        environment="test",
        keyword='"script failed"',
        level="",
        container_name="srm-script-container",
        limit=10,
    )

    assert '"ok": true' in raw
    assert calls == [
        '_namespace_: saas-test-new AND _container_name_: srm-script-container AND "script failed"'
    ]


def test_obs_sls_query_scopes_script_trace_to_container(monkeypatch):
    calls = []
    monkeypatch.setattr(sls_config, "credentials", lambda target: ("ak-id", "ak-secret"))

    def fake_query_trace(*args):
        calls.append(args)
        return [{"__time__": str(args[7]), "content": "trace hit"}], "Complete"

    monkeypatch.setattr(sls, "query_trace", fake_query_trace)
    raw = server.obs_sls_query(
        environment="prod",
        trace_id="trace-1",
        container_name="srm-script-container",
        limit=10,
    )
    result = json.loads(raw)

    assert result["ok"] is True
    assert calls[0][-1] == "srm-script-container"
    assert result["meta"]["query"] == (
        '"trace-1" AND _namespace_: saas-prod AND _container_name_: srm-script-container'
    )


def test_sls_trace_applies_container_to_error_and_full_queries(monkeypatch):
    queries = []

    def fake_query_sls(project, logstore, ak_id, ak_secret, query, from_time, to_time, endpoint, line):
        queries.append(query)
        return [], "Complete"

    monkeypatch.setattr(sls, "query_sls", fake_query_sls)
    sls.query_trace(
        "project", "logstore", "ak-id", "ak-secret", "trace-1", "saas-test-new",
        1, 2, "endpoint", 10, "srm-script-container",
    )

    assert len(queries) == 2
    assert all("_container_name_: srm-script-container" in query for query in queries)


def test_obs_sls_query_rejects_unsafe_container_name():
    result = json.loads(server.obs_sls_query(container_name='srm-script-container" OR *'))

    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"
    assert result["error"]["retryable"] is False
    assert "container_name 只能包含" in result["error"]["message"]


def test_obs_sls_query_unknown_env_returns_error():
    result = json.loads(server.obs_sls_query(environment="no-such-env"))

    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"
    assert result["error"]["retryable"] is False
    assert "不支持的系统/环境" in result["error"]["message"]


def test_obs_sls_query_missing_credentials_is_not_retryable(monkeypatch):
    def missing_credentials(_target):
        raise RuntimeError("missing key")

    monkeypatch.setattr(sls_config, "credentials", missing_credentials)

    result = json.loads(server.obs_sls_query(environment="dev"))

    assert result["ok"] is False
    assert result["error"]["code"] == "config"
    assert result["error"]["retryable"] is False


def test_obs_sls_query_transient_backend_error_is_retryable(monkeypatch):
    def unavailable_backend(*_args):
        raise RuntimeError("temporary failure")

    monkeypatch.setattr(sls_config, "credentials", lambda target: ("ak-id", "ak-secret"))
    monkeypatch.setattr(sls, "query_sls", unavailable_backend)

    result = json.loads(server.obs_sls_query(environment="dev"))

    assert result["ok"] is False
    assert result["error"]["code"] == "sls_query"
    assert result["error"]["retryable"] is True
