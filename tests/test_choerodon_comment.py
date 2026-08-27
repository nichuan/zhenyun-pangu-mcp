"""猪齿鱼评论能力单元测试（list_issue_comments / create_issue_comment / _to_html_comment）。

注意：这些测试针对 choerodon.py 的纯函数与「mock 掉网络请求」后的逻辑，
不涉及真实猪齿鱼平台（真实调用需 .env 凭据，不在单测范围）。
"""
import os
import sys
from unittest import mock

import pytest

# 将 src 加入 import 路径（未在环境安装时）
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from zhenyun_pangu_mcp import choerodon  # noqa: E402


# ---------------------------------------------------------------------------
# _to_html_comment / _md_to_html：Markdown -> HTML
# ---------------------------------------------------------------------------
def test_to_html_comment_plain_text():
    html = choerodon._to_html_comment("**处理结果**：问题已修复")
    assert html == "<p><b>处理结果</b>：问题已修复</p>"


def test_to_html_comment_multiline_paragraphs():
    html = choerodon._to_html_comment("**处理结果**\n第二行")
    assert html == "<p><b>处理结果</b></p>\n<p>第二行</p>"


def test_to_html_comment_rejects_raw_html():
    with pytest.raises(choerodon.ChoerodonError, match="Markdown"):
        choerodon._to_html_comment("**结果** <b>已修复</b>")


def test_to_html_comment_rejects_plain_text():
    with pytest.raises(choerodon.ChoerodonError, match="规范 Markdown"):
        choerodon._to_html_comment("问题已修复")


def test_to_html_comment_empty_raises():
    with pytest.raises(choerodon.ChoerodonError):
        choerodon._to_html_comment("  ")


def test_md_to_html_heading():
    assert choerodon._to_html_comment("## 问题定位") == "<h2>问题定位</h2>"


def test_md_to_html_bold_and_inline_code():
    html = choerodon._to_html_comment("使用 **UPDATE** 修复 `sodr_po_header`")
    assert "<b>UPDATE</b>" in html
    assert "<code>sodr_po_header</code>" in html


def test_md_to_html_unordered_list():
    html = choerodon._to_html_comment("- 根因A\n- 根因B")
    assert "<ul>" in html
    assert "<li>根因A</li>" in html
    assert "<li>根因B</li>" in html


def test_md_to_html_code_block():
    html = choerodon._to_html_comment("```sql\nSELECT 1;\n```")
    assert '<pre><code class="language-sql">' in html
    assert "SELECT 1;" in html


def test_md_to_html_code_block_preserves_language_and_closing_boundary():
    html = choerodon._to_html_comment("```text\n```inside\nvalue\n```\n\n**完成**")
    assert '<pre><code class="language-text">```inside\nvalue</code></pre>' in html
    assert "<b>完成</b>" in html


def test_md_to_html_table():
    html = choerodon._to_html_comment(
        "| 字段 | 值 |\n| :--- | ---: |\n| 状态 | **正常** |"
    )
    assert "<table>" in html
    assert "<thead>" in html and "<tbody>" in html
    assert '<th align="left">字段</th>' in html
    assert '<th align="right">值</th>' in html
    assert '<td align="right"><b>正常</b></td>' in html


def test_md_to_html_quote():
    html = choerodon._to_html_comment("> 影响范围：3 条数据")
    assert "<blockquote>" in html
    assert "影响范围：3 条数据" in html


# ---------------------------------------------------------------------------
# create_issue_comment：写评论（mock _request 与详情校验）
# ---------------------------------------------------------------------------
def test_create_issue_comment_posts_json():
    calls = {}

    def fake_request(method, path, *, params=None, json_body=None, data=None, timeout=30):
        calls["method"] = method
        calls["path"] = path
        calls["body"] = json_body
        return None

    def fake_detail(issue_id, pid):
        return {"issueId": issue_id, "issueNum": "apaas-123", "summary": "测试任务"}

    with mock.patch.object(choerodon, "_request", side_effect=fake_request), \
         mock.patch.object(choerodon, "get_issue_detail", side_effect=fake_detail):
        res = choerodon.create_issue_comment("encrypted-id", "## 处理结果\n\n问题已修复", project_id="999")

    assert res["ok"] is True
    assert res["issueNum"] == "apaas-123"
    assert calls["method"] == "POST"
    assert "/issue_comment" in calls["path"]
    assert calls["body"]["issueId"] == "encrypted-id"
    assert calls["body"]["commentText"] == "<h2>处理结果</h2>\n<p>问题已修复</p>"


def test_create_issue_comment_target_not_found_raises():
    with mock.patch.object(choerodon, "get_issue_detail", return_value={}):
        with pytest.raises(choerodon.ChoerodonError):
            choerodon.create_issue_comment("encrypted-id", "内容", project_id="999")


# ---------------------------------------------------------------------------
# download_attachment：签名下载地址（mock get_access_token + requests）
# 覆盖 P0-1 修复：301/302 走 Location 头；200 无 Location 时走 JSON url；
# 200 且无 Location 且响应体非 dict/无 url 时应报清晰错误，而非 KeyError。
# ---------------------------------------------------------------------------
class _FakeResp:
    def __init__(self, status, headers=None, json_body=None):
        self.status_code = status
        self.headers = headers or {}
        self._json_body = json_body

    def json(self):
        if isinstance(self._json_body, Exception):
            raise self._json_body
        return self._json_body


def test_download_attachment_302_uses_location():
    resp = _FakeResp(302, headers={"Location": "https://signed.example/x"})
    with mock.patch.object(choerodon, "get_access_token", return_value="tok"), \
         mock.patch("requests.get", return_value=resp):
        out = choerodon.download_attachment("https://file/x")
    assert out["signed_url"] == "https://signed.example/x"


def test_download_attachment_200_location_priority():
    # 200 且同时带 Location 头：优先 Location（不回退 JSON url）
    resp = _FakeResp(200, headers={"Location": "https://signed.example/x"},
                     json_body={"url": "https://wrong.example"})
    with mock.patch.object(choerodon, "get_access_token", return_value="tok"), \
         mock.patch("requests.get", return_value=resp):
        out = choerodon.download_attachment("https://file/x")
    assert out["signed_url"] == "https://signed.example/x"


def test_download_attachment_200_no_location_uses_json_url():
    # 200 且无 Location：回退取 JSON 体 url 字段
    resp = _FakeResp(200, headers={}, json_body={"url": "https://signed.example/x"})
    with mock.patch.object(choerodon, "get_access_token", return_value="tok"), \
         mock.patch("requests.get", return_value=resp):
        out = choerodon.download_attachment("https://file/x")
    assert out["signed_url"] == "https://signed.example/x"


def test_download_attachment_200_no_url_raises_clear_error():
    # 200、无 Location、JSON 体又无 url：此前优先级 bug 会抛 KeyError，
    # 修复后应给出清晰 ChoerodonError，绝不抛 500。
    resp = _FakeResp(200, headers={}, json_body={"foo": "bar"})
    with mock.patch.object(choerodon, "get_access_token", return_value="tok"), \
         mock.patch("requests.get", return_value=resp):
        with pytest.raises(choerodon.ChoerodonError, match="未返回可用下载地址"):
            choerodon.download_attachment("https://file/x")


def test_download_attachment_200_non_json_body_raises_clear_error():
    # 200、无 Location、响应体非 JSON：应报清晰错误而非 KeyError
    resp = _FakeResp(200, headers={}, json_body=ValueError("not json"))
    with mock.patch.object(choerodon, "get_access_token", return_value="tok"), \
         mock.patch("requests.get", return_value=resp):
        with pytest.raises(choerodon.ChoerodonError, match="非 JSON"):
            choerodon.download_attachment("https://file/x")


# ---------------------------------------------------------------------------
# list_issue_comments：查评论（mock _request）
# ---------------------------------------------------------------------------
def test_list_issue_comments_parses():
    def fake_request(method, path, *, params=None, json_body=None, data=None, timeout=30):
        return {
            "content": [
                {"commentId": "c1", "userRealName": "张三", "commentText": "<p>已修复</p>",
                 "lastUpdateDate": "2026-08-19 10:00:00"},
            ]
        }

    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        res = choerodon.list_issue_comments("encrypted-id", project_id="999")

    assert res["total"] == 1
    assert res["comments"][0]["author"] == "张三"
    assert "已修复" in res["comments"][0]["content"]


# ---------------------------------------------------------------------------
# CHOERODON_DISPATCH：新工具已注册
# ---------------------------------------------------------------------------
def test_dispatch_has_comment_tools():
    assert "list_comments" in choerodon.CHOERODON_DISPATCH
    assert "create_comment" in choerodon.CHOERODON_DISPATCH


# ---------------------------------------------------------------------------
# 租户编码解析：issueNum 前缀 / 独立字段 / 完整编号
# ---------------------------------------------------------------------------
def test_split_issue_num_with_prefix():
    assert choerodon._split_issue_num("prod-bug-213849") == ("prod-bug", "213849")


def test_split_issue_num_plain():
    assert choerodon._split_issue_num("213849") == ("", "213849")


def test_issue_brief_parses_tenant_from_foundation_field():
    # 真·租户编码来自 foundationFieldValue.pro_code1(与 raycast 一致)
    # issueNum 前缀 prod-bug 只是编号前缀,不是租户编码
    it = {
        "issueId": "enc-1", "issueNum": "prod-bug-213849",
        "foundationFieldValue": {"pro_code1": "SRM-FLGE"},
        "summary": "测试缺陷", "statusVO": {"name": "进行中"},
        "typeCode": "bug", "issueTypeVO": {"name": "缺陷"},
        "priorityVO": {"name": "高"}, "createUser": {"name": "张三"},
    }
    brief = choerodon._issue_brief(it)
    assert brief["tenantCode"] == "SRM-FLGE"
    assert brief["fullIssueNum"] == "prod-bug-213849"
    assert brief["issueNum"] == "prod-bug-213849"


def test_issue_brief_falls_back_to_independent_field():
    # 若 API 另有独立 organizationCode 字段,优先用它
    it = {"issueId": "enc-2", "issueNum": "100", "organizationCode": "prod", "projectCode": "bug"}
    brief = choerodon._issue_brief(it)
    assert brief["tenantCode"] == "prod"
    assert brief["projectCode"] == "bug"


def test_issue_brief_no_tenant_returns_empty():
    it = {"issueId": "enc-3", "issueNum": "55"}
    brief = choerodon._issue_brief(it)
    assert brief["tenantCode"] == ""
    assert brief["fullIssueNum"] == "55"


def test_get_issue_detail_carries_tenant_fields():
    # 详情接口不返回 foundationFieldValue,应反查 work_list 回填 tenantCode
    def fake_request(method, path, *, params=None, json_body=None, data=None, timeout=30):
        if method == "GET" and "/issues/" in path:
            return {
                "issueId": "enc-9", "issueNum": "prod-bug-213849", "summary": "x",
                "statusVO": {}, "issueTypeVO": {}, "priorityVO": {},
                "issueAttachmentVOList": [],
            }
        # 反查 work_list 返回带 foundationFieldValue 的列表项
        return {
            "content": [{
                "issueId": "enc-9", "issueNum": "prod-bug-213849",
                "foundationFieldValue": {"pro_code1": "SRM-FLGE"},
            }]
        }

    with mock.patch.object(choerodon, "_request", side_effect=fake_request):
        detail = choerodon.get_issue_detail("enc-9", project_id="58")
    assert detail["tenantCode"] == "SRM-FLGE"
    assert detail["fullIssueNum"] == "prod-bug-213849"
