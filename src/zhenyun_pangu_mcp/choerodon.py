"""猪齿鱼(Choerodon)集成 —— 完全自包含、真实可用。

登录采用四步纯 HTTP 流程(移植自 raycast choerodon 插件,无需 Playwright/验证码):
  1. GET  {base}/oauth/choerodon/login  → 提取 RSA 公钥(data-publicKey)+ JSESSIONID cookie
  2. RSA-PKCS1v15 加密密码
  3. POST {base}/oauth/login(表单)       → 302 Location
  4. 手动跟随重定向链,从 Location 提取 access_token

API 采用真实网关 open-gateway.going-link.com 的真实路径:
  - 工作列表:POST /agile/v2/projects/{pid}/issues/work_list
  - 工单详情:GET  /agile/v1/projects/{pid}/issues/{issueId}
  - 用户搜索:POST /agile/v1/projects/{pid}/issues/users
  - 状态映射:GET  /agile/v1/projects/{pid}/schemes/query_status_by_project_id
  - 附件下载:GET  /hfle/v1/files/redirect-url

配置(从 .env 读取):CHOERODON_BASE_URL / CHOERODON_ORG_ID / CHOERODON_TENANT_ID /
CHOERODON_PROJECT_ID / CHOERODON_USERNAME / CHOERODON_PASSWORD / CHOERODON_TOKEN_CACHE
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from typing import Any, Optional

import requests
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives.asymmetric import padding
from cryptography.hazmat.primitives.serialization import load_der_public_key

from . import config

BASE_URL = config.CHOERODON_BASE_URL.rstrip("/")
ORGANIZATION_ID = config.CHOERODON_ORG_ID
TENANT_ID = config.CHOERODON_TENANT_ID
DEFAULT_PROJECT_ID = config.CHOERODON_PROJECT_ID
TOKEN_CACHE = config.CHOERODON_TOKEN_CACHE

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# 登录 & Token 管理
# ---------------------------------------------------------------------------


class ChoerodonError(Exception):
    pass


# 缓存 token 的有效期(秒)。猪齿鱼主键加密依赖会话一致性:
# 若频繁重新登录换 token,会导致列表/详情用不同会话的 issueId 解密失败。
# 因此缓存有效期设 8 小时,期间稳定复用同一 token。
TOKEN_TTL = 8 * 3600


def _load_cached_token() -> Optional[str]:
    try:
        with open(TOKEN_CACHE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if time.time() < data.get("expires_at", 0):
            return data.get("access_token")
    except Exception:
        return None
    return None


def _save_token(token: str, ttl: int = TOKEN_TTL):
    try:
        os.makedirs(os.path.dirname(TOKEN_CACHE) or ".", exist_ok=True)
        with open(TOKEN_CACHE, "w", encoding="utf-8") as f:
            json.dump({"access_token": token, "expires_at": time.time() + ttl}, f)
    except Exception:
        pass


def _clear_token_cache():
    try:
        if os.path.exists(TOKEN_CACHE):
            os.remove(TOKEN_CACHE)
    except Exception:
        pass


def _login() -> str:
    """四步纯 HTTP 登录,返回 access_token。"""
    username = config.CHOERODON_USERNAME
    password = config.CHOERODON_PASSWORD
    if not username or not password:
        raise ChoerodonError(
            "未配置猪齿鱼凭据:请在 .env 设置 CHOERODON_USERNAME / CHOERODON_PASSWORD"
        )
    s = requests.Session()
    try:
        # Step1: 获取登录页,RSA 公钥 + cookie
        r1 = s.get(f"{BASE_URL}/oauth/choerodon/login",
                   headers={"User-Agent": UA}, allow_redirects=False, timeout=30)
        r1.raise_for_status()
        m = re.search(r'data-publicKey="([^"]*)"', r1.text)
        if not m or not m.group(1):
            raise ChoerodonError("登录失败:未从登录页获取到 RSA 公钥,请检查 CHOERODON_BASE_URL")
        public_key = m.group(1)

        # Step2: RSA-PKCS1v15 加密密码
        try:
            pub = load_der_public_key(base64.b64decode(public_key), default_backend())
            encrypted = base64.b64encode(
                pub.encrypt(password.encode("utf-8"), padding.PKCS1v15())
            ).decode()
        except Exception as e:
            raise ChoerodonError(f"登录失败:密码 RSA 加密异常 ({e})") from e

        # Step3: POST /oauth/login
        r3 = s.post(
            f"{BASE_URL}/oauth/login",
            data={"username": username, "password": encrypted},
            headers={
                "User-Agent": UA,
                "Content-Type": "application/x-www-form-urlencoded",
                "Origin": BASE_URL,
                "Referer": f"{BASE_URL}/oauth/choerodon/login",
            },
            allow_redirects=False,
            timeout=30,
        )
        location = r3.headers.get("Location")
        if not location:
            raise ChoerodonError(
                f"登录失败:未收到 302 Location(HTTP {r3.status_code})。账密错误或平台不可用"
            )

        # Step4: 跟随重定向链,提取 access_token
        cur = location
        for _ in range(6):
            m = re.search(r"access_token=([^&#]+)", cur)
            if m:
                return m.group(1)
            cur = cur if cur.startswith("http") else f"{BASE_URL}{cur}"
            r4 = s.get(cur, headers={"User-Agent": UA}, allow_redirects=False, timeout=30)
            cur = r4.headers.get("Location") or ""
        raise ChoerodonError("登录失败:未能从重定向链中提取 access_token")
    except requests.RequestException as e:
        raise ChoerodonError(f"登录失败:网络错误 ({e})") from e


def _verify_token(token: str) -> bool:
    """轻量校验 token 是否真的可用(避免缓存到登录返回但实际失效的 token)。"""
    try:
        r = requests.get(
            f"{BASE_URL}/iam/choerodon/v1/users/self",
            headers=_headers(token, content_type=False),
            timeout=15, allow_redirects=False,
        )
        return r.status_code == 200
    except Exception:
        return False


def get_access_token(force_refresh: bool = False) -> str:
    """获取 token:优先复用缓存,仅当不存在/过期/校验失效时才重新登录。

    缓存策略:
      - 缓存文件存在且未超 TOKEN_TTL(8h) → 直接复用,不发起任何登录请求
      - 缓存不存在或已过期 → 走 _login()
      - 新登录拿到的 token 会做一次轻量连通性校验,确认可用后才写入缓存
      - 运行时若服务端返回 401(被动失效),由 _request 清缓存并重登
    """
    if not force_refresh:
        cached = _load_cached_token()
        if cached and _verify_token(cached):
            return cached
    token = _login()
    if not _verify_token(token):
        raise ChoerodonError("登录成功但 token 校验失败,猪齿鱼平台可能暂不可用")
    _save_token(token)
    return token


def _headers(token: str, content_type: bool = False) -> dict:
    h = {
        "Accept": "application/json",
        "Authorization": f"bearer {token}",
        "H-Menu-Id": "0",
        "H-Tenant-Id": TENANT_ID,
        "User-Agent": UA,
    }
    if content_type:
        h["Content-Type"] = "application/json"
    return h


def _request(method: str, path: str, *, params: Optional[dict] = None,
             json_body: Optional[dict] = None, data: Optional[dict] = None,
             timeout: int = 30) -> Any:
    """统一请求,401 自动刷新 token 重试一次。"""
    def do(token):
        return requests.request(
            method, f"{BASE_URL}{path}",
            params=params, json=json_body, data=data,
            headers=_headers(token, content_type=(json_body is not None)),
            timeout=timeout, allow_redirects=False,
        )
    resp = do(get_access_token())
    if resp.status_code == 401:
        _clear_token_cache()
        resp = do(get_access_token())
    if resp.status_code != 200:
        raise ChoerodonError(
            f"API 请求失败 {resp.status_code} {method} {path}: {resp.text[:300]}"
        )
    try:
        return resp.json()
    except ValueError as e:
        raise ChoerodonError(f"API 响应解析失败 {path}: {e}") from e


def _list(data: Any) -> list:
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        c = data.get("content")
        if isinstance(c, list):
            return c
    return []


# ---------------------------------------------------------------------------
# 业务能力(移植 raycast api.ts 的真实 API 语义)
# ---------------------------------------------------------------------------


def _issue_brief(it: dict) -> dict:
    status = (it.get("statusVO") or {})
    itype = (it.get("issueTypeVO") or {})
    pri = (it.get("priorityVO") or {})
    creator = (it.get("createUser") or {}).get("name") or it.get("reporterName") or ""
    return {
        "issueId": str(it.get("issueId") or ""),
        "issueNum": str(it.get("issueNum") or ""),
        "summary": str(it.get("summary") or ""),
        "statusName": str(status.get("name") or ""),
        "typeCode": str(it.get("typeCode") or ""),
        "typeName": str(itype.get("name") or ""),
        "priority": str(pri.get("name") or ""),
        "creator": str(creator),
    }


def search_issues(keyword: str = "", size: int = 20, project_id: str | None = None,
                  assignee_ids: Optional[list] = None, status_ids: Optional[list] = None) -> list:
    """条件搜索工单(work_list)。支持 keyword(概要模糊) / assignee / status。"""
    pid = project_id or DEFAULT_PROJECT_ID
    path = f"/agile/v2/projects/{pid}/issues/work_list?page=0&size={size}"
    conditions: list[dict] = []
    if keyword and keyword.strip():
        conditions.append({
            "field": {"fieldCode": "content", "predefined": True, "fieldType": "input", "name": "概要"},
            "relationship": "AND", "operation": "LIKE", "value": {"valueStr": keyword.strip()},
        })
    if assignee_ids:
        conditions.append({
            "field": {"fieldCode": "assignee", "fieldType": "member", "predefined": True, "name": "经办人"},
            "relationship": "AND", "operation": "IN", "value": {"valueIdList": assignee_ids},
        })
    if status_ids:
        conditions.append({
            "field": {"fieldCode": "status", "fieldType": "multiple", "predefined": True, "name": "状态"},
            "relationship": "AND", "operation": "IN", "value": {"valueIdList": status_ids},
        })
    body = {"conditions": conditions, "treeFlag": True, "withSubIssues": False}
    data = _request("POST", path, json_body=body)
    return [_issue_brief(it) for it in _list(data)]


def get_issue_detail(issue_id: str, project_id: str | None = None) -> dict:
    """工单详情(含附件列表;描述保留原始 HTML)。

    issue_id 为加密 id(base64,含 '=' 填充符),不能 URL 编码,否则服务端解密失败。
    """
    pid = project_id or DEFAULT_PROJECT_ID
    path = f"/agile/v1/projects/{pid}/issues/{issue_id}" \
           f"?organizationId={ORGANIZATION_ID}&instanceProjectId={pid}"
    data = _request("GET", path)
    if not isinstance(data, dict):
        raise ChoerodonError(f"工单详情响应格式异常: {data}")
    if data.get("failed"):
        raise ChoerodonError(f"工单详情查询失败: {data.get('message') or data.get('code')}")
    status = (data.get("statusVO") or {})
    itype = (data.get("issueTypeVO") or {})
    pri = (data.get("priorityVO") or {})
    creator = (data.get("createUser") or {}).get("name") or data.get("reporterName") or ""
    attachments = [
        {"fileName": str(a.get("fileName") or ""), "url": str(a.get("url") or "")}
        for a in (data.get("issueAttachmentVOList") or [])
    ]
    return {
        "issueId": str(data.get("issueId") or issue_id),
        "issueNum": str(data.get("issueNum") or ""),
        "summary": str(data.get("summary") or ""),
        "statusName": str(status.get("name") or ""),
        "typeCode": str(data.get("typeCode") or ""),
        "typeName": str(itype.get("name") or ""),
        "priority": str(pri.get("name") or ""),
        "creator": str(creator),
        "description_html": str(data.get("description") or ""),
        "attachments": attachments,
    }


def search_users(name: str, size: int = 50, project_id: str | None = None) -> list:
    """按姓名模糊搜索项目成员(返回加密 id / 真实名 / 登录名)。"""
    pid = project_id or DEFAULT_PROJECT_ID
    path = f"/agile/v1/projects/{pid}/issues/users?param={requests.utils.quote(name)}&page=0&size={size}"
    data = _request("POST", path)
    return [
        {
            "id": str(u.get("id") or ""),
            "realName": str(u.get("realName") or ""),
            "loginName": str(u.get("loginName") or ""),
        }
        for u in _list(data)
    ]


def get_status_map(project_id: str | None = None) -> dict:
    """状态名 -> 加密 id 映射。"""
    pid = project_id or DEFAULT_PROJECT_ID
    path = f"/agile/v1/projects/{pid}/schemes/query_status_by_project_id?apply_type=agile"
    data = _request("GET", path)
    lst = _list(data)
    return {str(it.get("name") or ""): str(it.get("id") or "") for it in lst if it.get("name") and it.get("id")}


def search_tasks_by_person(name: str, size: int = 50, project_id: str | None = None) -> list:
    """按经办人姓名搜索任务:先按姓名查成员,再用其 id 过滤任务。"""
    users = search_users(name, size, project_id)
    if not users:
        return []
    return search_issues(size=size, project_id=project_id,
                         assignee_ids=[u["id"] for u in users])


def list_attachments(issue_id: str, project_id: str | None = None) -> list:
    """工单附件列表。"""
    detail = get_issue_detail(issue_id, project_id)
    return detail["attachments"]


def download_attachment(file_url: str) -> dict:
    """通过 hfle redirect-url 获取签名下载地址(返回 302 目标 URL 与文件名)。"""
    token = get_access_token()
    path = f"/hfle/v1/files/redirect-url?bucketName=private&access_token={token}&url={requests.utils.quote(file_url)}"
    resp = requests.get(
        f"{BASE_URL}{path}",
        headers=_headers(token), allow_redirects=False, timeout=30,
    )
    if resp.status_code not in (200, 301, 302):
        raise ChoerodonError(f"附件 redirect 失败 {resp.status_code}")
    target = resp.headers.get("Location") or resp.json().get("url") if resp.status_code in (200,) else resp.headers.get("Location")
    if isinstance(target, dict):
        target = target.get("url")
    return {"signed_url": target, "original_url": file_url}


# ---------------------------------------------------------------------------
# 评论能力(移植自 pg-choerodon add-comment,真实 API)
# ---------------------------------------------------------------------------

_HTML_ESCAPE = {"&": "&amp;", "<": "&lt;", ">": "&gt;"}


def _esc(s: str) -> str:
    return "".join(_HTML_ESCAPE.get(ch, ch) for ch in s)


def _inline_md(s: str) -> str:
    """行内 Markdown 转 HTML：`code`、**加粗**、*斜体*。先转义 HTML，再做标记替换。"""
    s = _esc(s)
    s = s.replace("`", "\x00code\x00")
    import re as _re
    # **加粗**
    s = _re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
    # *斜体*
    s = _re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<i>\1</i>", s)
    s = s.replace("\x00code\x00", "`")
    # 行内代码（转义后的反引号不可再用 _re.sub 处理，改用占位循环）
    if "`" in s:
        parts = s.split("`")
        out = []
        for i, p in enumerate(parts):
            out.append(f"<code>{p}</code>" if i % 2 == 1 else p)
        s = "".join(out)
    return s


def _md_to_html(text: str) -> str:
    """将 Markdown 文本渲染为 HTML（纯标准库），供猪齿鱼评论区展示。

    支持：标题 #/##/###、代码块 ```、无序/有序列表、引用 >、表格、段落、加粗/斜体/行内代码。
    若输入本身就是 HTML（以 < 开头），原样返回，不做二次转义。
    """
    import re as _re
    lines = (text or "").splitlines()
    if not text or not text.strip():
        raise ChoerodonError("评论内容不能为空")
    if text.strip().startswith("<"):
        return text.strip()

    html: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        stripped = line.strip()
        # 代码块
        if stripped.startswith("```"):
            i += 1
            code_lines: list[str] = []
            while i < n and not lines[i].strip().startswith("```"):
                code_lines.append(lines[i])
                i += 1
            i += 1  # 跳过结束 ```（若存在）
            html.append("<pre><code>" + _esc("\n".join(code_lines)) + "</code></pre>")
            continue
        # 标题
        m = _re.match(r"^(#{1,3})\s+(.*)$", stripped)
        if m:
            lvl = len(m.group(1))
            html.append(f"<h{lvl}>{_inline_md(m.group(2))}</h{lvl}>")
            i += 1
            continue
        # 引用块（连续 > 行合并为 blockquote）
        if stripped.startswith(">"):
            quote_lines: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote_lines.append(lines[i].strip().lstrip(">").strip())
                i += 1
            body = "<br/>".join(_inline_md(q) for q in quote_lines if q)
            html.append(f"<blockquote>{body}</blockquote>")
            continue
        # 无序列表（连续 - / * 项）
        if _re.match(r"^\s*[-*+]\s+", line):
            items: list[str] = []
            while i < n and _re.match(r"^\s*[-*+]\s+", lines[i]):
                item_text = _re.sub(r"^\s*[-*+]\s+", "", lines[i].strip())
                items.append("<li>" + _inline_md(item_text) + "</li>")
                i += 1
            html.append("<ul>" + "".join(items) + "</ul>")
            continue
        # 有序列表（连续 数字. 项）
        if _re.match(r"^\s*\d+[.)]\s+", line):
            items = []
            while i < n and _re.match(r"^\s*\d+[.)]\s+", lines[i]):
                item_text = _re.sub(r"^\s*\d+[.)]\s+", "", lines[i].strip())
                items.append("<li>" + _inline_md(item_text) + "</li>")
                i += 1
            html.append("<ol>" + "".join(items) + "</ol>")
            continue
        # 空行
        if not stripped:
            i += 1
            continue
        # 普通段落
        html.append(f"<p>{_inline_md(stripped)}</p>")
        i += 1
    return "\n".join(html)


def _to_html_comment(text: str) -> str:
    """将评论内容转为 HTML：已是 HTML 则原样返回；否则按 Markdown 渲染（展示更美观）。"""
    t = (text or "").strip()
    if not t:
        raise ChoerodonError("评论内容不能为空")
    return _md_to_html(t)


def list_issue_comments(issue_id: str, size: int = 100, project_id: str | None = None) -> dict:
    """查询猪齿鱼任务的评论列表(按时间倒序,最近在前)。

    issue_id 为加密 id(与 query_issue 一致)。
    """
    pid = project_id or DEFAULT_PROJECT_ID
    path = f"/agile/v1/projects/{pid}/issue_comment/issue/{issue_id}/page" \
           f"?organizationId=0&page=0&size={max(1, min(int(size), 200))}"
    data = _request("GET", path)
    comments = [
        {
            "commentId": str(c.get("commentId") or ""),
            "author": str(c.get("userRealName") or c.get("userName") or ""),
            "loginName": str(c.get("userLoginName") or ""),
            "content": str(c.get("commentText") or c.get("htmlContent") or ""),
            "updatedAt": str(c.get("lastUpdateDate") or ""),
        }
        for c in _list(data)
    ]
    return {"total": len(comments), "comments": comments}


def create_issue_comment(issue_id: str, comment: str, project_id: str | None = None) -> dict:
    """为猪齿鱼任务新增评论(写操作)。

    issue_id 为加密 id;comment 支持纯文本或 HTML(纯文本自动转 <p> 段落)。
    对应 API: POST /agile/v1/projects/{pid}/issue_comment,body={issueId, commentText}。
    """
    pid = project_id or DEFAULT_PROJECT_ID
    html_content = _to_html_comment(comment)
    # 写操作:先校验目标任务存在,避免误写
    detail = get_issue_detail(issue_id, pid)
    if not detail or not detail.get("issueId"):
        raise ChoerodonError(f"任务不存在或无法访问: {issue_id}")
    body = {"issueId": issue_id, "commentText": html_content}
    _request("POST", f"/agile/v1/projects/{pid}/issue_comment", json_body=body)
    return {
        "ok": True,
        "issueId": issue_id,
        "issueNum": detail.get("issueNum", ""),
        "summary": detail.get("summary", ""),
        "commentText": html_content,
        "note": "评论已写入猪齿鱼。",
    }


# 工具名 -> 处理函数映射(供 server.py 调用,返回 dict)
CHOERODON_DISPATCH = {
    "query_issue": lambda issue_id, project_id=None: get_issue_detail(issue_id, project_id),
    "list_issue": lambda keyword="", size=20, project_id=None,
                  assignee=None, status=None: list_issue_search(keyword, size, project_id, assignee, status),
    "search_users": search_users,
    "get_status_map": get_status_map,
    "search_tasks_by_person": search_tasks_by_person,
    "list_attachments": list_attachments,
    "download_attachment": download_attachment,
    "list_comments": lambda issue_id, size=100, project_id=None: list_issue_comments(issue_id, size, project_id),
    "create_comment": lambda issue_id, comment, project_id=None: create_issue_comment(issue_id, comment, project_id),
}


def list_issue_search(keyword: str = "", size: int = 20, project_id: str | None = None,
                      assignee: str = "", status: str = "") -> dict:
    """list_issue 工具入口:支持按经办人姓名/状态名过滤。"""
    assignee_ids = None
    if assignee:
        users = search_users(assignee, project_id=project_id)
        assignee_ids = [u["id"] for u in users]
    status_ids = None
    if status:
        smap = get_status_map(project_id)
        sid = smap.get(status)
        status_ids = [sid] if sid else None
    items = search_issues(keyword=keyword, size=size, project_id=project_id,
                          assignee_ids=assignee_ids, status_ids=status_ids)
    return {"total": len(items), "items": items}
