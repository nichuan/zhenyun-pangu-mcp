"""Voyage embedding provider。"""

from __future__ import annotations

import logging
from typing import Any

try:
    import voyageai
except ModuleNotFoundError:  # pragma: no cover - exercised only before dependency installation
    voyageai = None  # type: ignore[assignment]

from .base import EmbeddingConfigurationError, EmbeddingError, EmbeddingProvider, validate_embeddings

logger = logging.getLogger(__name__)


class VoyageEmbeddingProvider(EmbeddingProvider):
    provider_name = "voyage"
    vector_column = "embedding_voyage"

    def __init__(
        self,
        api_key: str,
        model: str = "voyage-4",
        dimension: int = 2048,
        client: Any | None = None,
    ) -> None:
        if not api_key.strip():
            raise EmbeddingConfigurationError("缺少 VOYAGE_API_KEY，无法使用 Voyage embedding provider。")
        if dimension not in {256, 512, 1024, 2048}:
            raise EmbeddingConfigurationError(
                "VOYAGE_EMBEDDING_DIMENSION 必须是 256、512、1024 或 2048。"
            )
        self.api_key = api_key.strip()
        self._model = model.strip()
        self._dimension = dimension
        if client is None:
            if voyageai is None:
                raise EmbeddingConfigurationError(
                    "未安装 voyageai 依赖，请先执行 uv sync。"
                )
            client = voyageai.Client(api_key=self.api_key)
        self.client = client

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed(texts, input_type="document")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], input_type="query")
        return vectors[0]

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Voyage embedding 输入不能为空。")
        try:
            result = self.client.embed(
                texts=texts,
                model=self._model,
                input_type=input_type,
                output_dimension=self._dimension,
                output_dtype="float",
            )
            raw_vectors = getattr(result, "embeddings", None)
            return validate_embeddings(raw_vectors, len(texts), self._dimension, self.provider_name)
        except EmbeddingError as exc:
            logger.error("Voyage embedding 响应校验失败：%s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 不吞掉错误，也不返回假向量
            logger.exception("Voyage embedding 调用失败：%s", exc)
            raise EmbeddingError("Voyage embedding 调用失败，未生成向量。") from exc
