"""Archery Session 缓存与认证失效重试测试。"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import archery  # noqa: E402


class _FakeCookies(dict):
    pass


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {"status": 0, "data": {}}
        self.text = text

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self, query_statuses=None):
        self.cookies = _FakeCookies()
        self.calls = []
        self.login_count = 0
        self.query_count = 0
        self.query_statuses = list(query_statuses or [200])

    def get(self, url, **kwargs):
        self.calls.append(("GET", url))
        if url.endswith("/login/"):
            self.cookies["csrftoken"] = "csrf-token"
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url))
        if url.endswith("/authenticate/"):
            self.login_count += 1
            self.cookies["sessionid"] = f"session-{self.login_count}"
        return _FakeResponse()

    def request(self, method, url, **kwargs):
        self.calls.append((method, url))
        if url.endswith("/query/"):
            self.query_count += 1
            status = self.query_statuses[min(self.query_count - 1, len(self.query_statuses) - 1)]
            if status != 200:
                return _FakeResponse(status_code=status, payload={"status": 1, "msg": "expired"})
            return _FakeResponse(
                payload={
                    "status": 0,
                    "data": {"column_list": ["value"], "rows": [[1]]},
                }
            )
        return _FakeResponse()


@pytest.fixture
def fake_archery(monkeypatch):
    sessions = []

    def make_session():
        session = _FakeSession()
        sessions.append(session)
        return session

    monkeypatch.setattr(archery.requests, "Session", make_session)
    monkeypatch.setitem(archery.ARCHERY_CREDENTIALS, "cn", ("user", "password"))
    archery._CLIENTS.clear()
    yield sessions
    archery._CLIENTS.clear()


def test_client_is_cached_per_site(fake_archery):
    first = archery._client("cn")
    second = archery._client("cn")

    assert first is second
    assert len(fake_archery) == 1


def test_session_and_authentication_are_reused(fake_archery):
    client = archery._client("cn")

    client.query("SELECT 1", "instance", "srm")
    client.query("SELECT 1", "instance", "srm")

    session = fake_archery[0]
    assert session.login_count == 1
    assert session.query_count == 2


def test_401_reauthenticates_and_retries_once(monkeypatch):
    session = _FakeSession(query_statuses=[401, 200])
    monkeypatch.setattr(archery.requests, "Session", lambda: session)
    monkeypatch.setitem(archery.ARCHERY_CREDENTIALS, "cn", ("user", "password"))

    client = archery.ArcheryClient("cn")
    result = client.query("SELECT 1", "instance", "srm")

    assert result["rows"] == [{"value": 1}]
    assert session.login_count == 2
    assert session.query_count == 2

