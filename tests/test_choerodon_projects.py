"""猪齿鱼跨项目发现与查询测试。"""
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import choerodon, server  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_project_cache():
    choerodon._PROJECT_CACHE.clear()
    yield
    choerodon._PROJECT_CACHE.clear()


@pytest.fixture
def project_inventory():
    projects = [choerodon._project_brief(project) for project in [
        {"id": "58", "name": "正式环境问题处理", "code": "prod-bug"},
        {"id": "678199993236914176", "name": "客户远程运维", "code": "cro"},
        {"id": "655098477554888704", "name": "客户提升优化", "code": "ced"},
        {"id": "336153683604279296", "name": "寻源套件", "code": "src"},
        {"id": "226341605569249280", "name": "盘古-远程服务中心", "code": "cdp"},
        {"id": "738424127719677952", "name": "盘古-标准产品", "code": "H-SAAS"},
        {"id": "335018747535142912", "name": "甄云SaaS产品项目群", "code": "saas"},
        {"id": "551359059057360896", "name": "天工-标准产品", "code": "T-SAAS"},
        {"id": "616401801445310464", "name": "茶百道", "code": "SRM-CHAPANDA"},
    ]]
    with mock.patch.object(choerodon, "_fetch_accessible_projects", return_value=projects) as fetch:
        yield projects, fetch


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
    fake_list_projects = mock.Mock(
        return_value={"total": 1, "items": [{"projectId": "58"}]}
    )
    with mock.patch.dict(
        choerodon.CHOERODON_DISPATCH,
        {"list_projects": fake_list_projects},
    ):
        result = json.loads(server.choerodon_list_projects("58"))

    fake_list_projects.assert_called_once_with(keyword="58", size=100)
    assert result["ok"] is True
    assert result["items"][0]["projectId"] == "58"
    assert result["meta"]["source"] == "choerodon"


@pytest.mark.parametrize("input_num,canonical,pid", [
    (" prod-bug-213849 ", "prod-bug-213849", "58"),
    ("CRO-123", "cro-123", "678199993236914176"),
    ("CED-17640", "ced-17640", "655098477554888704"),
    ("SRC-65224", "src-65224", "336153683604279296"),
    ("CDP-189256", "cdp-189256", "226341605569249280"),
    ("h-saas-4900", "H-SAAS-4900", "738424127719677952"),
    ("saas-123", "saas-123", "335018747535142912"),
    ("SASS-123", "saas-123", "335018747535142912"),
    ("t-saas-123", "T-SAAS-123", "551359059057360896"),
    ("srm-chapanda-123", "SRM-CHAPANDA-123", "616401801445310464"),
])
def test_complete_number_routes_to_one_project_and_excludes_fuzzy_hits(project_inventory, input_num, canonical, pid):
    _, fetch = project_inventory
    with mock.patch.object(choerodon, "_request", return_value={"content": [
        {"issueId": "wrong", "issueNum": canonical + "0"},
        {"issueId": "exact", "issueNum": canonical},
        {"issueId": "summary-only", "issueNum": "other-999", "summary": canonical},
    ]}) as request:
        result = choerodon.list_issue_search(input_num)

    fetch.assert_called_once_with()
    request.assert_called_once()
    assert f"/projects/{pid}/issues/work_list" in request.call_args.args[1]
    conditions = request.call_args.kwargs["json_body"]["conditions"]
    assert conditions[0]["value"]["valueStr"] == canonical
    assert result["projectId"] == pid
    assert result["resolution"]["issueNum"] == canonical
    assert [item["issueId"] for item in result["items"]] == ["exact"]
    assert result["items"][0]["projectId"] == pid
    assert ("warning" in result["resolution"]) == input_num.lower().startswith("sass-")


def test_numeric_api_issue_number_can_be_matched_with_resolved_project(project_inventory):
    with mock.patch.object(choerodon, "_request", return_value={"content": [
        {"issueId": "exact", "issueNum": 4900},
        {"issueId": "wrong", "issueNum": 49001},
    ]}):
        result = choerodon.list_issue_search("H-SAAS-4900")
    assert result["total"] == 1
    assert result["items"][0]["fullIssueNum"] == "H-SAAS-4900"


def test_history_lookups_keep_each_projects_context_and_completed_issues(project_inventory):
    references = [
        ("ced-17640", "655098477554888704", "已完成"),
        ("src-65224", "336153683604279296", "已发布"),
        ("cdp-189256", "226341605569249280", "已关闭"),
    ]
    results = []
    for number, pid, status in references:
        with mock.patch.object(choerodon, "_request", return_value={"content": [
            {"issueId": "history", "issueNum": number, "statusVO": {"name": status}},
        ]}) as request:
            result = choerodon.list_issue_search(number)
        assert f"/projects/{pid}/issues/work_list" in request.call_args.args[1]
        conditions = request.call_args.kwargs["json_body"]["conditions"]
        assert [condition["field"]["fieldCode"] for condition in conditions] == ["content"]
        assert result["items"][0]["statusName"] == status
        results.append(result)
    assert [result["resolution"]["projectId"] for result in results] == [ref[1] for ref in references]
    project_inventory[1].assert_called_once_with()


def test_real_sass_code_takes_precedence_over_alias(project_inventory):
    projects, _ = project_inventory
    projects.append(choerodon._project_brief({"id": "actual-sass", "code": "sass"}))
    with mock.patch.object(choerodon, "search_issues", return_value=[]):
        result = choerodon.list_issue_search("sass-123")
    assert result["projectId"] == "actual-sass"
    assert result["resolution"]["matchedBy"] == "project_code"
    assert "warning" not in result["resolution"]


def test_unknown_prefix_does_not_query_default_or_other_projects(project_inventory):
    _, fetch = project_inventory
    with mock.patch.object(choerodon, "search_issues") as search:
        result = json.loads(server.choerodon_list_issue(keyword="unknown-123"))
    fetch.assert_called_once_with()
    search.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["retryable"] is False
    assert "unknown" in result["error"]["message"]


def test_explicit_project_conflict_fails_before_issue_query(project_inventory):
    with mock.patch.object(choerodon, "search_issues") as search:
        result = json.loads(server.choerodon_list_issue(keyword="H-SAAS-4900", project_id="58"))
    search.assert_not_called()
    assert result["ok"] is False
    assert result["error"]["retryable"] is False
    assert "冲突" in result["error"]["message"]


def test_duplicate_prefix_requires_project_context_and_disabled_project_is_rejected(project_inventory):
    projects, _ = project_inventory
    projects.append(choerodon._project_brief({"id": "other-cro", "code": "cro"}))
    with mock.patch.object(choerodon, "search_issues", return_value=[]) as search:
        with pytest.raises(ValueError, match="多个可访问项目"):
            choerodon.list_issue_search("cro-123")
        search.assert_not_called()
        assert choerodon.list_issue_search("cro-123", project_id="other-cro")["projectId"] == "other-cro"
        search.reset_mock()
        projects[-1]["enabled"] = False
        with pytest.raises(ValueError, match="已停用"):
            choerodon.list_issue_search("cro-123", project_id="other-cro")
        search.assert_not_called()


def test_project_cache_reuses_inventory_but_not_tasks_and_expires(project_inventory):
    _, fetch = project_inventory
    with mock.patch.object(choerodon.time, "monotonic", return_value=100) as clock, \
         mock.patch.object(choerodon, "search_issues", return_value=[]) as search:
        choerodon.list_issue_search("cro-1")
        choerodon.list_issue_search("H-SAAS-4900")
        assert fetch.call_count == 1
        assert search.call_count == 2
        clock.return_value = 100 + choerodon.PROJECT_CACHE_TTL
        choerodon.list_issue_search("cro-2")
        assert fetch.call_count == 2
        assert search.call_count == 3


def test_cached_unknown_prefix_refreshes_once_and_finds_new_project(project_inventory):
    projects, fetch = project_inventory
    with mock.patch.object(choerodon, "search_issues", return_value=[]):
        choerodon.list_issue_search("cro-1")
        fetch.return_value = projects + [choerodon._project_brief({"id": "new-id", "code": "new"})]
        assert choerodon.list_issue_search("new-123")["projectId"] == "new-id"
        assert fetch.call_count == 2
        with pytest.raises(ValueError, match="未匹配"):
            choerodon.list_issue_search("missing-123")
        assert fetch.call_count == 3


def test_project_cache_is_scoped_to_account_and_explicit_discovery_refreshes(project_inventory):
    _, fetch = project_inventory
    with mock.patch.object(choerodon, "search_issues", return_value=[]):
        choerodon.list_issue_search("cro-1")
        with mock.patch.object(choerodon.config, "CHOERODON_USERNAME", "another-account"):
            choerodon.list_issue_search("cro-2")
        assert fetch.call_count == 2
        result = choerodon.list_projects("h-saas-4900")
        assert fetch.call_count == 3
    assert [item["code"] for item in result["items"]] == ["H-SAAS"]


def test_filters_and_empty_results_keep_resolved_project(project_inventory):
    with mock.patch.object(choerodon, "search_users", return_value=[{"id": "user"}]) as users, \
         mock.patch.object(choerodon, "get_status_map", return_value={"开发中": "status"}) as statuses, \
         mock.patch.object(choerodon, "search_issues", return_value=[]) as search:
        result = choerodon.list_issue_search("h-saas-4900", assignee="倪川", status="开发中")
    pid = "738424127719677952"
    users.assert_called_once_with("倪川", project_id=pid)
    statuses.assert_called_once_with(pid)
    search.assert_called_once_with(keyword="H-SAAS-4900", size=20, project_id=pid,
                                   assignee_ids=["user"], status_ids=["status"])
    assert result["total"] == 0
    assert result["resolution"]["projectId"] == pid


@pytest.mark.parametrize("keyword,pid", [("4900", None), ("报价错误", None), ("", "other")])
def test_plain_search_preserves_project_defaults_without_project_discovery(project_inventory, keyword, pid):
    _, fetch = project_inventory
    with mock.patch.object(choerodon, "search_issues", return_value=[]) as search:
        assert choerodon.list_issue_search(keyword, project_id=pid) == {"total": 0, "items": []}
    fetch.assert_not_called()
    assert search.call_args.kwargs["project_id"] == pid


def test_partial_project_inventory_is_not_cached_or_used_for_routing():
    def fake_request(method, path, **kwargs):
        if path.endswith("/users/self"):
            return {"id": "user-id"}
        return {"totalElements": 201, "content": [{"id": "58", "code": "prod-bug"}]}
    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        with pytest.raises(choerodon.ChoerodonError, match="分页提前结束"):
            choerodon.list_issue_search("prod-bug-123")
    assert not choerodon._PROJECT_CACHE
