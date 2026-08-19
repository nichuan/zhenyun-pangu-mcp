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

from zhenyun_pangun_mcp import choerodon  # noqa: E402


# ---------------------------------------------------------------------------
# _to_html_comment：纯文本 -> HTML 段落
# ---------------------------------------------------------------------------
def test_to_html_comment_plain_text():
    html = choerodon._to_html_comment("你好，问题已修复")
    assert html == "<p>你好，问题已修复</p>"


def test_to_html_comment_multiline():
    html = choerodon._to_html_comment("第一行\n第二行")
    assert html == "<p>第一行</p><p>第二行</p>"


def test_to_html_comment_escapes_html_special():
    html = choerodon._to_html_comment("a<b> & c>d")
    assert "<b>" not in html  # 尖括号被转义，不会作为标签注入


def test_to_html_comment_passthrough_html():
    html = choerodon._to_html_comment("<p>已转 HTML</p>")
    assert html == "<p>已转 HTML</p>"


def test_to_html_comment_empty_raises():
    with pytest.raises(choerodon.ChoerodonError):
        choerodon._to_html_comment("  ")


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
        res = choerodon.create_issue_comment("encrypted-id", "你好，已处理", project_id="999")

    assert res["ok"] is True
    assert res["issueNum"] == "apaas-123"
    assert calls["method"] == "POST"
    assert "/issue_comment" in calls["path"]
    assert calls["body"]["issueId"] == "encrypted-id"
    assert calls["body"]["commentText"] == "<p>你好，已处理</p>"


def test_create_issue_comment_target_not_found_raises():
    with mock.patch.object(choerodon, "get_issue_detail", return_value={}):
        with pytest.raises(choerodon.ChoerodonError):
            choerodon.create_issue_comment("encrypted-id", "内容", project_id="999")


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
