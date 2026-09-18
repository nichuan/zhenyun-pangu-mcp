"""Loki 客户端构造/登录失败的统一错误响应回归（H1）。

背景：loki._get_client() 内部会 login()，凭据缺失或登录失败时抛 LokiError。
旧实现把 _get_client 放在 try 之外，异常会逃逸，绕过 {ok:false,error:{...}} 契约。
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import loki, server  # noqa: E402


def _boom(region):
    raise loki.LokiError("未配置 AWS_LOG_USERNAME/PASSWORD")


def test_obs_log_datasources_returns_error_when_login_fails(monkeypatch):
    monkeypatch.setattr(loki, "_get_client", _boom)

    out = json.loads(server.obs_log_datasources(region="aws"))

    assert out["ok"] is False
    assert out["error"]["retryable"] is True
    assert "AWS_LOG_USERNAME" in out["error"]["message"]


def test_obs_log_query_returns_error_when_login_fails(monkeypatch):
    monkeypatch.setattr(loki, "resolve_datasource", lambda region, env: "loki-aws-prod")
    monkeypatch.setattr(loki, "_get_client", _boom)

    out = json.loads(server.obs_log_query(query='{app="srm-gateway"}', region="aws"))

    assert out["ok"] is False
    assert out["error"]["retryable"] is True
