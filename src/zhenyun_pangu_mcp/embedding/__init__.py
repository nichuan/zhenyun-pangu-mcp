"""可切换的文档/查询 embedding provider。"""

from .base import EmbeddingConfigurationError, EmbeddingError, EmbeddingProvider
from .factory import create_embedding_provider, get_embedding_provider

__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingError",
    "EmbeddingProvider",
    "create_embedding_provider",
    "get_embedding_provider",
]
