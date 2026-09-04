"""Embedding provider factory。"""

from __future__ import annotations

from functools import lru_cache

from .. import config
from .base import EmbeddingConfigurationError, EmbeddingProvider
from .nvidia import NvidiaEmbeddingProvider
from .voyage import VoyageEmbeddingProvider


def create_embedding_provider(provider_name: str | None = None) -> EmbeddingProvider:
    name = (provider_name or config.get_embedding_provider_name()).strip().lower()
    if name == "voyage":
        return VoyageEmbeddingProvider(
            api_key=config.get_voyage_api_key(),
            model=config.get_voyage_embed_model(),
            dimension=config.get_voyage_embed_dimension(),
        )
    if name == "nvidia":
        return NvidiaEmbeddingProvider(
            api_key=config.get_nvidia_api_key(),
            model=config.get_nvidia_embed_model(),
            url=config.get_nvidia_embed_url(),
            dimension=config.get_nvidia_embed_dimension(),
        )
    raise EmbeddingConfigurationError(
        f"不支持的 EMBEDDING_PROVIDER：{name}（可选 nvidia / voyage）。"
    )


@lru_cache(maxsize=None)
def get_embedding_provider() -> EmbeddingProvider:
    """获取进程级单例，避免每次 MCP 请求重复创建 API client。"""
    return create_embedding_provider()
