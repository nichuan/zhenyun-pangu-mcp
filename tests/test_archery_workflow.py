"""Archery 数据修复工单校验与提交边界。"""
import json

import pytest
from requests.exceptions import Timeout

from zhenyun_pangu_mcp import archery, server


class Response:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self.payload = payload
        self.text = ""

    def json(self):
        return self.payload


class WorkflowSession:
    def __init__(self, check_result=None, submit_response=None):
        self.cookies = {}
        self.calls = []
        self.check_result = check_result or {
            "checked": "True", "error_count": 0, "warning_count": 0,
            "error": None, "is_critical": False,
            "rows": [{"errlevel": 0, "sql": "UPDATE repair SET value = 1"}],
        }
        self.submit_response = submit_response or Response(201, {"workflow": {"id": 901}})

    def get(self, url, **kwargs):
        if url.endswith("/login/"):
            self.cookies["csrftoken"] = "test-csrf"
        return Response(200, {})

    def post(self, url, **kwargs):
        if url.endswith("/authenticate/"):
            self.cookies["sessionid"] = "test-session"
        return Response(200, {})

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        if url.endswith("/api/v1/workflow/sqlcheck/"):
            return Response(200, self.check_result)
        if url.endswith("/api/v1/workflow/"):
            if isinstance(self.submit_response, Exception):
                raise self.submit_response
            return self.submit_response
        raise AssertionError(url)


@pytest.fixture
def workflow_session(monkeypatch):
    session = WorkflowSession()
    monkeypatch.setattr(archery.requests, "Session", lambda: session)
    monkeypatch.setitem(archery.ARCHERY_CREDENTIALS, "cn", ("user", "password"))
    archery._CLIENTS.clear()
    archery._WORKFLOW_CHECKS.clear()
    yield session
    archery._CLIENTS.clear()
    archery._WORKFLOW_CHECKS.clear()


def test_only_pure_dml_reaches_sqlcheck(workflow_session):
    sql = "UPDATE repair SET value = 'a;b' WHERE id = 1;\nDELETE FROM repair WHERE id = 2;"
    workflow_session.check_result["rows"].append({"errlevel": 0, "sql": "DELETE FROM repair WHERE id = 2"})
    checked = json.loads(server.archery_workflow_sqlcheck(sql, 62, "srm"))
    assert checked["ok"] is True
    assert checked["passed"] is True
    assert checked["instance_id"] == 62
    assert checked["statement_count"] == 2
    check_call = next(call for call in workflow_session.calls if call[1].endswith("/sqlcheck/"))
    assert check_call[2]["json"]["full_sql"] == sql
    assert check_call[2]["json"]["instance_id"] == 62
    assert check_call[2]["json"]["db_name"] == "srm"

    submitted = json.loads(server.archery_submit_workflow(checked["check_token"], "repair-901", 20))
    assert submitted["submitted"] is True
    assert submitted["workflow_id"] == 901
    submit_call = next(call for call in workflow_session.calls if call[1].endswith("/api/v1/workflow/"))
    assert submit_call[2]["json"]["sql_content"] == check_call[2]["json"]["full_sql"]
    assert submit_call[2]["json"]["workflow"]["instance"] == 62
    assert submit_call[2]["json"]["workflow"]["group_id"] == 20

    again = json.loads(server.archery_submit_workflow(checked["check_token"], "repair-901", 20))
    assert again["ok"] is False
    assert len([call for call in workflow_session.calls if call[1].endswith("/api/v1/workflow/")]) == 1


@pytest.mark.parametrize("sql", [
    "SELECT * FROM repair; UPDATE repair SET value = 1;",
    "UPDATE repair SET value = 1; DROP TABLE repair;",
    "-- note\nUPDATE repair SET value = 1;",
    "BEGIN; UPDATE repair SET value = 1; COMMIT;",
])
def test_non_dml_is_rejected_before_network(workflow_session, sql):
    result = json.loads(server.archery_workflow_sqlcheck(sql, 62, "srm"))
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"
    assert workflow_session.calls == []


def test_failed_check_cannot_be_submitted(workflow_session):
    workflow_session.check_result["error_count"] = 1
    workflow_session.check_result["rows"][0]["errlevel"] = 2
    result = json.loads(server.archery_workflow_sqlcheck("UPDATE repair SET value = 1;", 62, "srm"))
    assert result["ok"] is True
    assert result["passed"] is False
    assert "check_token" not in result
    assert json.loads(server.archery_submit_workflow("unknown", "repair-901", 20))["ok"] is False
    assert not any(call[1].endswith("/api/v1/workflow/") for call in workflow_session.calls)


def test_submit_timeout_is_not_retried(workflow_session):
    checked = json.loads(server.archery_workflow_sqlcheck("UPDATE repair SET value = 1;", 62, "srm"))
    workflow_session.submit_response = Timeout("timeout")
    result = json.loads(server.archery_submit_workflow(checked["check_token"], "repair-901", 20))
    assert result["ok"] is False
    assert result["error"]["code"] == "archery_submit_unknown"
    assert result["error"]["retryable"] is False
    assert len([call for call in workflow_session.calls if call[1].endswith("/api/v1/workflow/")]) == 1
