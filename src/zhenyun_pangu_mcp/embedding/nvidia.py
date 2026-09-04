"""NVIDIA embedding provider，保留原有 NVIDIA API 能力。"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .base import EmbeddingConfigurationError, EmbeddingError, EmbeddingProvider, validate_embeddings

logger = logging.getLogger(__name__)


class NvidiaEmbeddingProvider(EmbeddingProvider):
    provider_name = "nvidia"
    vector_column = "embedding"

    def __init__(
        self,
        api_key: str,
        model: str = "nvidia/nv-embed-v1",
        url: str = "https://integrate.api.nvidia.com/v1/embeddings",
        dimension: int = 2048,
        http_client: Any = requests,
    ) -> None:
        if not api_key.strip():
            raise EmbeddingConfigurationError("缺少 NVIDIA_API_KEY，无法使用 NVIDIA embedding provider。")
        if dimension <= 0:
            raise EmbeddingConfigurationError("NVIDIA_EMBEDDING_DIMENSION 必须是正整数。")
        self.api_key = api_key.strip()
        self._model = model.strip()
        self.url = url.strip()
        self._dimension = dimension
        self.http_client = http_client

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return self._embed(texts, input_type="passage")

    def embed_query(self, text: str) -> list[float]:
        vectors = self._embed([text], input_type="query")
        return vectors[0]

    def _embed(self, texts: list[str], input_type: str) -> list[list[float]]:
        if not texts:
            return []
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("NVIDIA embedding 输入不能为空。")

        payload: dict[str, Any] = {
            "input": texts[0] if len(texts) == 1 else texts,
            "model": self._model,
        }
        # nv-embed-v1 兼容原实现，不传 input_type；nv-embedqa 系列需要显式区分。
        if "nv-embedqa" in self._model.lower():
            payload["input_type"] = input_type
        try:
            response = self.http_client.post(
                self.url,
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
            response.raise_for_status()
            body = response.json()
            items = body.get("data") if isinstance(body, dict) else None
            if not isinstance(items, list):
                raise EmbeddingError("NVIDIA embedding 响应缺少 data 数组。")
            # API 通常按输入顺序返回；存在 index 时按 index 排序，确保批量回填不串行。
            if all(isinstance(item, dict) and "index" in item for item in items):
                items = sorted(items, key=lambda item: int(item["index"]))
            raw_vectors = [item.get("embedding") for item in items if isinstance(item, dict)]
            return validate_embeddings(raw_vectors, len(texts), self._dimension, self.provider_name)
        except EmbeddingError as exc:
            logger.error("NVIDIA embedding 响应校验失败：%s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 对外统一成不泄露 key 的领域异常
            logger.exception("NVIDIA embedding 调用失败：%s", exc)
            raise EmbeddingError("NVIDIA embedding 调用失败，未生成向量。") from exc
