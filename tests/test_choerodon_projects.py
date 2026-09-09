"""猪齿鱼跨项目发现与查询测试。"""
import json
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import choerodon, server  # noqa: E402


def test_list_projects_fetches_accessible_projects_and_filters_exact_match_first():
    calls = []

    def fake_request(method, path, *, params=None, json_body=None, data=None, timeout=30):
        calls.append((method, path, params, json_body))
        if path == "/iam/choerodon/v1/users/self":
            return {"id": "encrypted-user"}
        return {
            "totalElements": 3,
            "content": [
                {"id": 58, "name": "正式环境问题处理", "code": "prod-bug", "enabled": True},
                {
                    "id": "738424127719677952",
                    "name": "盘古-标准产品",
                    "code": "H-SAAS",
                    "enabled": True,
                },
                {"id": "9", "name": "其它项目", "code": "OTHER", "enabled": False},
            ],
        }

    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        result = choerodon.list_projects("盘古-标准产品")

    assert result["total"] == 1
    assert result["accessibleTotal"] == 3
    assert result["defaultProjectId"] == str(choerodon.DEFAULT_PROJECT_ID)
    assert result["items"][0] == {
        "projectId": "738424127719677952",
        "name": "盘古-标准产品",
        "code": "H-SAAS",
        "organizationId": str(choerodon.ORGANIZATION_ID),
        "category": "",
        "enabled": True,
        "projectStatus": "",
        "starFlag": False,
        "isDefault": False,
    }
    assert calls[1][0] == "POST"
    assert calls[1][1].endswith("/users/encrypted-user/projects/paging")
    assert calls[1][2] == {"page": 0, "size": 200, "params": ""}
    assert calls[1][3] == {}


def test_list_projects_supports_id_and_code_search():
    response = {
        "totalElements": 2,
        "content": [
            {"id": 58, "name": "正式环境问题处理", "code": "prod-bug"},
            {"id": "738424127719677952", "name": "盘古-标准产品", "code": "H-SAAS"},
        ],
    }

    def fake_request(method, path, **kwargs):
        if path.endswith("/users/self"):
            return {"id": "user-id"}
        return response

    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        by_id = choerodon.list_projects("58")
        by_code = choerodon.list_projects("h-saas")

    assert [item["projectId"] for item in by_id["items"]] == ["58"]
    assert by_id["items"][0]["isDefault"] is True
    assert [item["projectId"] for item in by_code["items"]] == ["738424127719677952"]


def test_list_projects_paginates_and_deduplicates():
    first_page = [{"id": str(i), "name": f"project-{i}"} for i in range(200)]
    second_page = [
        {"id": "199", "name": "duplicate"},
        {"id": "200", "name": "target-project", "code": "TARGET"},
    ]

    def fake_request(method, path, *, params=None, **kwargs):
        if path.endswith("/users/self"):
            return {"id": "user-id"}
        return {
            "totalElements": 202,
            "content": first_page if params["page"] == 0 else second_page,
        }

    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        result = choerodon.list_projects("target")

    assert result["accessibleTotal"] == 201
    assert result["total"] == 1
    assert result["items"][0]["projectId"] == "200"


def test_cross_project_issue_results_keep_project_id():
    with mock.patch.object(
        choerodon,
        "_request",
        return_value={"content": [{"issueId": "enc", "issueNum": "H-SAAS-4900"}]},
    ):
        items = choerodon.search_issues(project_id="738424127719677952")

    assert items[0]["projectId"] == "738424127719677952"


def test_unknown_assignee_or_status_does_not_return_unfiltered_tasks():
    with mock.patch.object(choerodon, "search_users", return_value=[]), \
         mock.patch.object(choerodon, "search_issues") as search_issues:
        assert choerodon.list_issue_search(assignee="不存在的人") == {"total": 0, "items": []}
        search_issues.assert_not_called()

    with mock.patch.object(choerodon, "get_status_map", return_value={}), \
         mock.patch.object(choerodon, "search_issues") as search_issues:
        assert choerodon.list_issue_search(status="不存在的状态") == {"total": 0, "items": []}
        search_issues.assert_not_called()


def test_project_tool_is_registered_and_returns_standard_envelope():
    assert "choerodon_list_projects" in server.mcp._tool_manager._tools
    with mock.patch.object(
        choerodon,
        "list_projects",
        return_value={"total": 1, "items": [{"projectId": "58"}]},
    ):
        result = json.loads(server.choerodon_list_projects("58"))

    assert result["ok"] is True
    assert result["items"][0]["projectId"] == "58"
    assert result["meta"]["source"] == "choerodon"
