"""Embedding provider factory。"""

from __future__ import annotations

from functools import lru_cache

from .. import config
from .base import EmbeddingProvider
from .cloudflare import CloudflareEmbeddingProvider


def create_embedding_provider() -> EmbeddingProvider:
    """构建 Cloudflare Workers AI provider；缺少凭据时抛 EmbeddingConfigurationError。"""
    return CloudflareEmbeddingProvider(
        api_token=config.get_cf_api_token(),
        account_id=config.get_cf_account_id(),
        model=config.get_cf_embed_model(),
        dimension=config.get_cf_embed_dimension(),
    )


@lru_cache(maxsize=None)
def get_embedding_provider() -> EmbeddingProvider:
    """获取进程级单例，避免每次 MCP 请求重复创建 API client。"""
    return create_embedding_provider()
