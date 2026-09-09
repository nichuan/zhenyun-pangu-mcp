"""Cloudflare Workers AI embedding provider（@cf/qwen/qwen3-embedding-0.6b）。

选择理由：免费层每天 10,000 Neurons，对本知识库规模（数千条）足够；
计价 $0.012 / M input tokens，无 NVIDIA nv-embed-v1 下线、Voyage 限速问题。

注意：该模型输出 1024 维，embedding 列需为 vector(1024)。
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from .base import EmbeddingConfigurationError, EmbeddingError, EmbeddingProvider, validate_embeddings

logger = logging.getLogger(__name__)


class CloudflareEmbeddingProvider(EmbeddingProvider):
    provider_name = "cloudflare"
    # 向量统一写回单列 embedding（vector(1024)）。
    vector_column = "embedding"

    def __init__(
        self,
        api_token: str,
        account_id: str,
        model: str = "@cf/qwen/qwen3-embedding-0.6b",
        dimension: int = 1024,
        base_url: str = "https://api.cloudflare.com/client/v4",
        http_client: Any = requests,
    ) -> None:
        if not api_token.strip():
            raise EmbeddingConfigurationError(
                "缺少 CF_API_TOKEN，无法使用 Cloudflare Workers AI embedding provider。"
            )
        if not account_id.strip():
            raise EmbeddingConfigurationError(
                "缺少 CF_ACCOUNT_ID，无法使用 Cloudflare Workers AI embedding provider。"
            )
        if dimension <= 0:
            raise EmbeddingConfigurationError("CF_EMBEDDING_DIMENSION 必须是正整数。")
        self.api_token = api_token.strip()
        self.account_id = account_id.strip()
        self._model = model.strip()
        self._dimension = dimension
        self.base_url = base_url.strip().rstrip("/")
        self.http_client = http_client

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def model_name(self) -> str:
        return self._model

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        return self._embed(texts)

    def embed_query(self, text: str) -> list[float]:
        return self._embed([text])[0]

    def _embed(self, texts: list[str]) -> list[list[float]]:
        if any(not isinstance(text, str) or not text.strip() for text in texts):
            raise EmbeddingError("Cloudflare embedding 输入不能为空。")
        url = f"{self.base_url}/accounts/{self.account_id}/ai/run/{self._model}"
        try:
            response = self.http_client.post(
                url,
                headers={
                    "Authorization": f"Bearer {self.api_token}",
                    "Content-Type": "application/json",
                },
                json={"text": texts},
                timeout=60,
            )
            response.raise_for_status()
            body = response.json()
            if not body.get("success", False):
                raise EmbeddingError(f"Cloudflare embedding 返回失败：{body.get('errors')}")
            raw_vectors = (body.get("result") or {}).get("data")
            return validate_embeddings(raw_vectors, len(texts), self._dimension, self.provider_name)
        except EmbeddingError as exc:
            logger.error("Cloudflare embedding 响应校验失败：%s", exc)
            raise
        except Exception as exc:  # noqa: BLE001 - 对外统一成不泄露 token 的领域异常
            logger.exception("Cloudflare embedding 调用失败：%s", exc)
            raise EmbeddingError("Cloudflare embedding 调用失败，未生成向量。") from exc
