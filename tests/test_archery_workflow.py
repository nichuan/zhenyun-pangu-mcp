"""Archery 数据修复工单校验与提交边界。"""
import json

import pytest
from requests.exceptions import Timeout

from zhenyun_pangu_mcp import archery, server


class Response:
    def __init__(self, status_code, payload, text=""):
        self.status_code = status_code
        self.payload = payload
        self.text = text

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
        self.group_options = '<option value="production" group-id="20">production</option>'
        self.group_instances = {
            "production": [{"id": 62, "instance_name": "SAAS-SRM-PROD数据库", "db_type": "mysql"}],
        }

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
        if url.endswith("/submitsql/"):
            return Response(200, {}, self.group_options)
        if url.endswith("/group/instances/"):
            result = self.group_instances[kwargs["data"]["group_name"]]
            return result if isinstance(result, Response) else Response(200, {"status": 0, "data": result})
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


def test_auto_resolve_then_check_and_submit_without_group_input(workflow_session):
    workflow_session.group_options += '<option value="testing" data-group-id="21">testing</option>'
    workflow_session.group_instances["testing"] = [
        {"id": 63, "instance_name": "SAAS-SRM-TEST数据库"},
        {"id": 64, "instance_name": "SAAS-SRM-PROD数据库-archive"},
    ]
    target = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert target["ok"] is True
    assert target["group_id"] == 20
    assert target["group_name"] == "production"
    assert target["instance_id"] == 62
    assert target["instance_name"] == "SAAS-SRM-PROD数据库"
    assert target["db_type"] == "mysql"
    assert [call[0] for call in workflow_session.calls] == ["GET", "POST", "POST"]
    assert all(call[2]["data"]["tag_code"] == "can_write" for call in workflow_session.calls[1:])
    assert not any("/api/v1/workflow/" in call[1] for call in workflow_session.calls)

    checked = json.loads(server.archery_workflow_sqlcheck(
        "UPDATE repair SET value = 1;", target["instance_id"], "srm", target["site"],
    ))
    submitted = json.loads(server.archery_submit_workflow(
        checked["check_token"], "prod-bug-901", target["group_id"], target["site"],
    ))
    assert submitted["submitted"] is True
    assert submitted["workflow_id"] == 901


def test_explicit_group_remains_exact_and_does_not_discover_other_groups(workflow_session):
    workflow_session.group_options += '<option value="testing" group-id="21">testing</option>'
    target = json.loads(server.archery_resolve_workflow_target(
        instance_name="SAAS-SRM-PROD数据库", group_name="production",
    ))
    assert target["group_id"] == 20
    assert [call[2]["data"]["group_name"] for call in workflow_session.calls[1:]] == ["production"]


@pytest.mark.parametrize("group_name", ["missing", "prod"])
def test_unknown_explicit_group_never_falls_back(workflow_session, group_name):
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod", group_name=group_name))
    assert result["ok"] is False
    assert len(workflow_session.calls) == 1


def test_multiple_groups_for_same_instance_do_not_select_first(workflow_session):
    workflow_session.group_options += '<option value="other" group-id="21">other</option>'
    workflow_session.group_instances["other"] = workflow_session.group_instances["production"]
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert result["error"]["code"] == "archery_workflow_target_ambiguous"
    assert result["error"]["retryable"] is False
    assert "production" in result["error"]["message"]
    assert "other" in result["error"]["message"]
    assert not any("/api/v1/workflow/" in call[1] for call in workflow_session.calls)


@pytest.mark.parametrize("instance_name", ["", "  ", "aws"])
def test_missing_or_cross_site_instance_is_rejected_before_network(workflow_session, instance_name):
    result = json.loads(server.archery_resolve_workflow_target(instance_name=instance_name, site="cn"))
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"
    assert workflow_session.calls == []


def test_aws_alias_uses_aws_options_and_returns_aws_site(workflow_session, monkeypatch):
    monkeypatch.setitem(archery.ARCHERY_CREDENTIALS, "aws", ("user", "password"))
    workflow_session.group_instances["production"] = [
        {"id": 91, "instance_name": archery.ARCHERY_INSTANCE_ALIASES["aws"]["aws"]},
    ]
    result = json.loads(server.archery_resolve_workflow_target(instance_name="aws", site="aws"))
    assert result["ok"] is True
    assert result["site"] == "aws"
    assert result["instance_id"] == 91
    assert all(call[1].startswith(archery.ARCHERY_BASE_URLS["aws"]) for call in workflow_session.calls)


@pytest.mark.parametrize("instance_name", ["SAAS-SRM-PROD", "prod-ro"])
def test_instance_must_exactly_match_a_writable_option(workflow_session, instance_name):
    result = json.loads(server.archery_resolve_workflow_target(instance_name=instance_name))
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"


@pytest.mark.parametrize("ids", [[0], [-1], [True], ["invalid"], [62, 63], [62, None]])
def test_invalid_or_ambiguous_instance_ids_fail_closed(workflow_session, ids):
    workflow_session.group_instances["production"] = [
        {"id": value, "instance_name": "SAAS-SRM-PROD数据库"} for value in ids
    ]
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert result["error"]["code"] == "bad_param"


def test_duplicate_options_with_same_id_are_deduplicated(workflow_session):
    workflow_session.group_options *= 2
    workflow_session.group_instances["production"] *= 2
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is True
    assert len(workflow_session.calls) == 2


def test_group_name_with_multiple_ids_fails_before_instance_discovery(workflow_session):
    workflow_session.group_options += '<option value="production" group-id="21">production</option>'
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert len(workflow_session.calls) == 1


def test_no_group_options_is_a_capability_error(workflow_session):
    workflow_session.group_options = "<html>login or unsupported form</html>"
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert result["error"]["code"] == "archery_workflow_target"
    assert result["error"]["retryable"] is False
    assert len(workflow_session.calls) == 1


@pytest.mark.parametrize("response", [Response(500, {}), Response(200, {"data": {}})])
def test_incomplete_discovery_never_returns_a_partial_match(workflow_session, response):
    workflow_session.group_options += '<option value="other" group-id="21">other</option>'
    workflow_session.group_instances["other"] = response
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert result["error"]["code"] == "archery_workflow_target"
    assert result["error"]["retryable"] is False


def test_group_discovery_is_bounded(workflow_session):
    workflow_session.group_options = "".join(
        f'<option value="group-{index}" group-id="{index + 1}">group</option>'
        for index in range(51)
    )
    result = json.loads(server.archery_resolve_workflow_target(instance_name="prod"))
    assert result["ok"] is False
    assert len(workflow_session.calls) == 1
