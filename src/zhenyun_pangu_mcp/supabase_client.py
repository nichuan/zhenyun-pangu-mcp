"""知识库 Supabase 客户端 + 统一 Embedding 服务。

整合 knowledge_docs / sql_templates / table_catalog / table_relations 四张表，
作为 zhenyun-pangu-mcp 的「知识 / 模板 / 表 / 关系」认知层统一数据入口。

依赖 .env 中的：
  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY   （知识库，不存业务数据）
  NVIDIA_API_KEY                             （语义向量，可选；未配置时检索降级为关键词）

设计要点（对齐「统一 embedding」）：
  EmbeddingService 集中管理向量生成，三个表共用同一模型与维度(2048)，
  未来更换 embedding model 只改这里。
"""
from __future__ import annotations

import logging
from typing import Any

import requests

from . import config

logger = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# 连接与表访问（轻量封装 Supabase REST，与 zhenyun-pangu 的 requests 风格一致）
# --------------------------------------------------------------------------- #
def get_headers() -> dict[str, str]:
    return {
        "apikey": config.get_supabase_key(),
        "Authorization": f"Bearer {config.get_supabase_key()}",
        "Content-Type": "application/json",
    }


def base_url() -> str:
    return config.get_supabase_url().rstrip("/")


def _rest(table: str, path: str = "", params: dict | None = None) -> list[dict[str, Any]]:
    """对某张表发起 REST 查询，返回行列表。path 用于 /rpc/xxx 等。"""
    if path:
        url = f"{base_url()}{path}"
    else:
        url = f"{base_url()}/rest/v1/{table}"
    resp = requests.get(url, headers=get_headers(), params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _rest_request(
    method: str, url: str, body: dict[str, Any] | None = None,
    params: dict | None = None,
) -> list[dict[str, Any]]:
    """对指定 URL 发起 POST/PATCH/DELETE，返回响应（数组）。用于写库。

    POST/PATCH 通过 Prefer: return=representation 让 Supabase 返回受影响行。
    """
    headers = get_headers()
    if method.upper() in ("POST", "PATCH"):
        headers["Prefer"] = "return=representation"
    resp = requests.request(
        method, url, headers=headers, json=body, params=params, timeout=30,
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        # 部分写操作返回空 body（如 DELETE 无返回行），此时以成功状态为准
        return [{"ok": True}] if resp.status_code < 300 else []
    if isinstance(data, list):
        return data
    return [data] if data else []


def rpc(function: str, payload: dict[str, Any]) -> list[dict[str, Any]]:
    """调用 Postgres RPC 函数（如 match_knowledge_docs / search_knowledge_docs_keyword）。"""
    url = f"{base_url()}/rest/v1/rpc/{function}"
    resp = requests.post(url, headers=get_headers(), json=payload, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data if isinstance(data, list) else []


def query_table(
    table: str,
    select: str = "*",
    eq: dict[str, Any] | None = None,
    ilike: dict[str, str] | None = None,
    order: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"select": select, "limit": limit}
    if eq:
        for k, v in eq.items():
            params[k] = f"eq.{v}"
    if ilike:
        for k, v in ilike.items():
            params[k] = f"ilike.{v}"
    if order:
        params["order"] = order
    return _rest(table, params=params)


# --------------------------------------------------------------------------- #
# 统一 Embedding 服务（NVIDIA 免费模型 nvidia/nv-embed-v1，2048 维）
# --------------------------------------------------------------------------- #
class EmbeddingService:
    """集中管理知识/模板/表的向量生成。未配置 key 时 embed() 返回 None（调用方降级）。"""

    def __init__(self) -> None:
        self.api_key = config.get_nvidia_api_key()
        self.model = config.get_nvidia_embed_model()
        self.url = config.get_nvidia_embed_url()

    @property
    def available(self) -> bool:
        return bool(self.api_key)

    def embed(self, text: str, input_type: str = "passage") -> list[float] | None:
        """生成向量。input_type: 'passage' 入库 / 'query' 检索。失败返回 None。"""
        if not self.api_key or not text.strip():
            return None
        payload: dict[str, Any] = {"input": text, "model": self.model}
        if "nv-embedqa" in self.model:
            payload["input_type"] = input_type
        try:
            resp = requests.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()
            vec = data["data"][0]["embedding"]
            return [float(x) for x in vec]
        except Exception as e:  # noqa: BLE001 - 失败降级，不打断主流程
            logger.warning("NVIDIA embedding 调用失败：%s", e)
            return None

    def embed_knowledge(self, payload: dict[str, Any]) -> list[float] | None:
        return self.embed(self._compose_knowledge(payload), input_type="passage")

    def embed_template(self, payload: dict[str, Any]) -> list[float] | None:
        return self.embed(self._compose_template(payload), input_type="passage")

    def embed_table(self, name: str, comment: str, description: str, tags: list[str]) -> list[float] | None:
        return self.embed(
            f"{name} {comment or ''} {description or ''} " + " ".join(tags or []),
            input_type="passage",
        )

    # ---- 向量字面量（Supabase REST 需转成 pgvector 文本） ----
    @staticmethod
    def to_literal(emb: list[float]) -> str:
        return "[" + ",".join(f"{x:.8g}" for x in emb) + "]"

    # ---- 各表向量组合文本 ----
    @staticmethod
    def _compose_knowledge(p: dict[str, Any]) -> str:
        parts = [
            p.get("title") or "", p.get("knowledge_type") or "", p.get("system") or "",
            p.get("module") or "", p.get("summary") or "",
            " ".join(p.get("tags") or []), " ".join(p.get("core_tables") or []),
            p.get("content_md") or "",
        ]
        return "\n".join(x for x in parts if x).strip()

    @staticmethod
    def _compose_template(p: dict[str, Any]) -> str:
        parts = [
            p.get("title") or "", p.get("category") or "", p.get("system") or "",
            p.get("scenario") or "", " ".join(p.get("keywords") or []),
            " ".join(p.get("core_tables") or []), p.get("sql_text") or "",
            p.get("problem_description") or "", p.get("symptom") or "",
            p.get("root_cause") or "", p.get("business_domain") or "",
        ]
        return "\n".join(x for x in parts if x).strip()


embedding = EmbeddingService()
