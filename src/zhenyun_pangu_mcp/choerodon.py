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
    # 2xx 均视为成功:200 OK / 201 Created(POST 创建) / 204 No Content 等。
    # 注意:猪齿鱼创建类接口常返回 201,此前误把非 200 当失败,导致"实际成功却报错"。
    if not (200 <= resp.status_code < 300):
        raise ChoerodonError(
            f"API 请求失败 {resp.status_code} {method} {path}: {resp.text[:300]}"
        )
    # 204/无响应体时无法解析 JSON,直接返回空 dict
    if not resp.content or not resp.text.strip():
        return {}
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


def _split_issue_num(issue_num: str) -> tuple[str, str]:
    """从完整任务编号拆分出 (前缀, 纯数字任务号),仅用于兜底展示。

    猪齿鱼敏捷项目的 issueNum 常见两种形态:
      1. 带前缀: 如 'prod-bug-213849' -> ('prod-bug', '213849')
      2. 纯数字: 如 '213849'          -> ('', '213849')
    注意: 这里的 '前缀' 是编号前缀(项目+租户短码),并非权威租户编码。
    权威租户编码应取 foundationFieldValue.pro_code1(见 _tenant_code)。
    """
    s = str(issue_num or "").strip()
    if not s:
        return "", ""
    if "-" in s:
        head, _, tail = s.rpartition("-")
        if head and tail.isdigit():
            return head, tail
    return "", s


def _tenant_code(it: dict) -> str:
    """真正的租户编码(与 raycast 插件一致,取自 foundationFieldValue.pro_code1)。

    work_list 列表接口的原始响应里带有 foundationFieldValue.pro_code1(如 'SRM-FLGE'),
    这才是猪齿鱼任务上可查到的'租户编码'。详情接口 issues/{id} 不返回该字段,
    需由调用方在列表阶段取得后带入。这里做多层兼容回退:
      1. foundationFieldValue.pro_code1 (权威,raycast 同款)
      2. 独立 organizationCode / tenantCode 字段
    """
    ffv = it.get("foundationFieldValue") or {}
    if isinstance(ffv, dict):
        pro_code1 = str(ffv.get("pro_code1") or "").strip()
        if pro_code1:
            return pro_code1
    independent = str(it.get("organizationCode") or it.get("tenantCode") or "").strip()
    return independent


def _proj_code(it: dict) -> str:
    return str(it.get("projectCode") or "").strip()


def _full_issue_num(it: dict) -> str:
    """返回用户在猪齿鱼页面上看到的人类可读完整任务编号(如 'prod-bug-213849')。

    work_list 返回的 issueNum 通常已是完整编号(含前缀),直接采用;
    若为纯数字则尝试用租户/项目编码拼接前缀兜底。
    """
    num = str(it.get("issueNum") or "").strip()
    if num and "-" in num:
        return num  # 已是完整编号(如 prod-bug-213849)
    org = _tenant_code(it)
    proj = _proj_code(it)
    pure = _split_issue_num(num)[1]
    prefix = "-".join(p for p in (org, proj) if p)
    if not prefix:
        return pure or num
    return f"{prefix}-{pure}" if pure else prefix


def _issue_brief(it: dict) -> dict:
    status = (it.get("statusVO") or {})
    itype = (it.get("issueTypeVO") or {})
    pri = (it.get("priorityVO") or {})
    creator = (it.get("createUser") or {}).get("name") or it.get("reporterName") or ""
    return {
        "issueId": str(it.get("issueId") or ""),
        "issueNum": str(it.get("issueNum") or ""),
        "fullIssueNum": _full_issue_num(it),
        "tenantCode": _tenant_code(it),
        "projectCode": _proj_code(it),
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

    租户编码(tenantCode)的权威来源是 work_list 列表接口的 foundationFieldValue.pro_code1,
    但本详情接口(issues/{id})不返回该字段。因此先尝试从详情响应自身取;
    取不到时,用详情返回的完整 issueNum 反查 work_list 回填,确保与列表/raycast 一致。
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
    tenant_code = _tenant_code(data)
    if not tenant_code:
        # 详情接口不返回 foundationFieldValue,用完整 issueNum 反查 work_list 回填
        issue_num = str(data.get("issueNum") or "")
        if issue_num:
            try:
                for it in search_issues(keyword=issue_num, size=20, project_id=pid):
                    if it.get("issueNum") == issue_num and it.get("tenantCode"):
                        tenant_code = it["tenantCode"]
                        break
            except Exception:
                pass
    return {
        "issueId": str(data.get("issueId") or issue_id),
        "issueNum": str(data.get("issueNum") or ""),
        "fullIssueNum": _full_issue_num(data),
        "tenantCode": tenant_code,
        "projectCode": _proj_code(data),
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


def _split_table_row(line: str) -> list[str] | None:
    """拆分 Markdown 管道表格的一行，支持反斜杠转义和代码 span 内的管道符。"""
    raw = line.strip()
    if "|" not in raw:
        return None
    if raw.startswith("|"):
        raw = raw[1:]
    if raw.endswith("|") and not raw.endswith("\\|"):
        raw = raw[:-1]

    cells: list[str] = []
    current: list[str] = []
    escaped = False
    in_code = False
    for ch in raw:
        if escaped:
            current.append(ch)
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == "`":
            current.append(ch)
            in_code = not in_code
        elif ch == "|" and not in_code:
            cells.append("".join(current).strip())
            current = []
        else:
            current.append(ch)
    if escaped:
        current.append("\\")
    cells.append("".join(current).strip())
    return cells


def _is_table_separator(cells: list[str] | None) -> bool:
    if not cells:
        return False
    return all(bool(re.fullmatch(r":?-{3,}:?", cell.replace(" ", ""))) for cell in cells)


def _table_start(lines: list[str], index: int) -> tuple[list[str], list[str]] | None:
    """返回表头和分隔线；普通含管道的段落不会被当成表格。"""
    if index + 1 >= len(lines):
        return None
    header = _split_table_row(lines[index])
    separator = _split_table_row(lines[index + 1])
    if not header or not separator or len(header) != len(separator):
        return None
    if not _is_table_separator(separator):
        return None
    return header, separator


def _md_to_html(text: str) -> str:
    """将 Markdown 文本渲染为 HTML（纯标准库），供猪齿鱼评论区展示。

    支持：标题 #/##/###、代码块 ```、无序/有序列表、引用 >、表格、段落、加粗/斜体/行内代码。
    评论只接受规范 Markdown，不接受原始 HTML，避免编辑器混合解析导致样式异常。
    """
    import re as _re
    lines = (text or "").splitlines()
    if not text or not text.strip():
        raise ChoerodonError("评论内容不能为空")

    if _re.search(r"<\/?[A-Za-z][^>]*>|<!--[\s\S]*?-->", text):
        raise ChoerodonError("评论必须使用 Markdown，不能直接传入 HTML 标签")
    if any(ord(ch) < 32 and ch not in "\n\r\t" for ch in text):
        raise ChoerodonError("评论 Markdown 含有不可见控制字符")

    # 只校验本渲染器支持的 Markdown 结构，避免未闭合代码块或散乱文本进入评论区。
    has_markdown = False
    in_fence = False
    for line_no, raw_line in enumerate(lines, 1):
        stripped_line = raw_line.strip()
        if in_fence:
            if stripped_line == "```":
                in_fence = False
            continue
        if stripped_line.startswith("```"):
            if not _re.fullmatch(r"```[A-Za-z0-9_+.-]*", stripped_line):
                raise ChoerodonError(f"第 {line_no} 行代码块标记不规范")
            in_fence = True
            has_markdown = True
            continue
        if _table_start(lines, line_no - 1):
            has_markdown = True
        if stripped_line.startswith("#"):
            if not _re.match(r"^#{1,3}\s+\S", stripped_line):
                raise ChoerodonError(f"第 {line_no} 行标题格式不规范，应使用 '# 标题'")
            has_markdown = True
        if _re.match(r"^[-*+]\s+\S", stripped_line) or _re.match(r"^\d+[.)]\s+\S", stripped_line):
            has_markdown = True
        if stripped_line.startswith(">"):
            has_markdown = True
        if _re.search(r"\*\*[^*\n]+\*\*|(?<!\*)\*[^*\n]+\*(?!\*)|`[^`\n]+`", raw_line):
            has_markdown = True
    if in_fence:
        raise ChoerodonError("评论 Markdown 代码块未闭合")
    if not has_markdown:
        raise ChoerodonError("评论必须使用规范 Markdown（至少包含标题、列表、引用、代码块或强调/行内代码）")

    html: list[str] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i].rstrip()
        stripped = line.strip()
        # 代码块
        if stripped.startswith("```"):
            opening = _re.fullmatch(r"```([A-Za-z0-9_+.-]*)", stripped)
            language = opening.group(1) if opening else ""
            i += 1
            code_lines: list[str] = []
            while i < n and lines[i].strip() != "```":
                code_lines.append(lines[i])
                i += 1
            if i < n:
                i += 1  # 跳过结束 ```
            class_attr = f' class="language-{language}"' if language else ""
            html.append(f"<pre><code{class_attr}>" + _esc("\n".join(code_lines)) + "</code></pre>")
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
        # Markdown 管道表格
        table = _table_start(lines, i)
        if table:
            headers, separators = table
            alignments: list[str | None] = []
            for separator in separators:
                compact = separator.replace(" ", "")
                if compact.startswith(":") and compact.endswith(":"):
                    alignments.append("center")
                elif compact.startswith(":"):
                    alignments.append("left")
                elif compact.endswith(":"):
                    alignments.append("right")
                else:
                    alignments.append(None)

            def render_cell(tag: str, value: str, align: str | None) -> str:
                align_attr = f' align="{align}"' if align else ""
                return f"<{tag}{align_attr}>{_inline_md(value)}</{tag}>"

            header_html = "".join(
                render_cell("th", value, alignments[col])
                for col, value in enumerate(headers)
            )
            rows_html: list[str] = []
            i += 2
            while i < n:
                row = _split_table_row(lines[i])
                if not row or len(row) != len(headers):
                    break
                rows_html.append(
                    "<tr>" + "".join(
                        render_cell("td", value, alignments[col])
                        for col, value in enumerate(row)
                    ) + "</tr>"
                )
                i += 1
            html.append(
                "<table><thead><tr>" + header_html + "</tr></thead>"
                "<tbody>" + "".join(rows_html) + "</tbody></table>"
            )
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
    """将规范 Markdown 评论转为 HTML；原始 HTML/纯文本会被拒绝。"""
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

    issue_id 为加密 id；comment 必须是规范 Markdown，工具会统一渲染为 HTML。
    不接受纯文本、原始 HTML 或混合格式。
    对应 API: POST /agile/v1/projects/{pid}/issue_comment,body={issueId, commentText}。
    """
    pid = project_id or DEFAULT_PROJECT_ID
    html_content = _to_html_comment(comment)
    # 写操作:先校验目标任务存在,避免误写
    detail = get_issue_detail(issue_id, pid)
    if not detail or not detail.get("issueId"):
        raise ChoerodonError(f"任务不存在或无法访问: {issue_id}")
    body = {"issueId": issue_id, "commentText": html_content}
    result = _request("POST", f"/agile/v1/projects/{pid}/issue_comment", json_body=body) or {}
    comment_id = str(result.get("commentId") or "")
    return {
        "ok": True,
        "commentId": comment_id,
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
