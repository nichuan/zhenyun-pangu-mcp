"""知识库 Supabase 客户端 + 统一 Embedding 服务。

整合 knowledge_docs / sql_templates / table_catalog / table_relations 四张表，
作为 zhenyun-pangu-mcp 的「知识 / 模板 / 表 / 关系」认知层统一数据入口。

依赖 .env 中的：
  SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY   （知识库，不存业务数据）
  NVIDIA_API_KEY / VOYAGE_API_KEY            （语义向量，可选；未配置时检索降级为关键词）

设计要点：
  EmbeddingService 只负责 provider 适配和输入文本拼装，业务层只使用
  embed_documents / embed_query；NVIDIA 的旧 embedding 列与 Voyage 的新列完全隔离。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

import requests

from . import config
from .embedding import EmbeddingConfigurationError, EmbeddingProvider, get_embedding_provider
from .embedding import text as embedding_text

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
# 统一 Embedding 服务（provider 可通过 EMBEDDING_PROVIDER 切换）
# --------------------------------------------------------------------------- #
class EmbeddingService:
    """懒加载 provider；配置缺失时让调用方走关键词降级。"""

    @property
    def provider(self) -> EmbeddingProvider:
        return get_embedding_provider()

    @property
    def available(self) -> bool:
        try:
            self.provider
            return True
        except EmbeddingConfigurationError as exc:
            logger.info("embedding provider 不可用，语义检索降级为关键词：%s", exc)
            return False

    @property
    def provider_name(self) -> str:
        return self.provider.provider_name

    @property
    def model_name(self) -> str:
        return self.provider.model_name

    @property
    def dimension(self) -> int:
        return self.provider.dimension

    @property
    def vector_column(self) -> str:
        """当前 provider 的向量列；voyage 不会覆盖旧 embedding。"""
        if self.available:
            return self.provider.vector_column
        return "embedding_voyage" if config.get_embedding_provider_name() == "voyage" else "embedding"

    def metadata_payload(self) -> dict[str, Any]:
        """返回 provider 专属元数据；NVIDIA 旧写入路径不增加旧库不存在的列。"""
        if self.provider_name != "voyage":
            return {}
        return {
            "embedding_voyage_provider": self.provider_name,
            "embedding_voyage_model": self.model_name,
            "embedding_voyage_dimension": self.dimension,
            "embedding_voyage_updated_at": datetime.now(timezone.utc).isoformat(),
        }

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """显式使用 document 模式，供入库和批量回填调用。"""
        return self.provider.embed_documents(texts)

    def embed_query(self, text: str) -> list[float]:
        """显式使用 query 模式，供语义检索调用。"""
        return self.provider.embed_query(text)

    def embed_knowledge(self, payload: dict[str, Any]) -> list[float]:
        return self.embed_documents([embedding_text.compose_knowledge(payload)])[0]

    def embed_template(self, payload: dict[str, Any]) -> list[float]:
        return self.embed_documents([embedding_text.compose_template(payload)])[0]

    def embed_table(self, name: str, comment: str, description: str, tags: list[str]) -> list[float]:
        return self.embed_documents([embedding_text.compose_table(name, comment, description, tags)])[0]

    def rpc_name(self, resource: str) -> str:
        """返回与当前 provider 配套的独立检索 RPC，防止异构向量混查。"""
        names = {
            "nvidia": {
                "knowledge": "match_knowledge_docs",
                "template": "match_sql_templates",
                "table": "search_table_catalog",
            },
            "voyage": {
                "knowledge": "match_knowledge_docs_voyage",
                "template": "match_sql_templates_voyage",
                "table": "search_table_catalog_voyage",
            },
        }
        try:
            return names[self.provider_name][resource]
        except KeyError as exc:
            raise ValueError(f"未知的 embedding 检索资源：{resource}") from exc

    # ---- 向量字面量（Supabase REST 需转成 pgvector 文本） ----
    @staticmethod
    def to_literal(emb: list[float]) -> str:
        return "[" + ",".join(f"{x:.8g}" for x in emb) + "]"

embedding = EmbeddingService()
