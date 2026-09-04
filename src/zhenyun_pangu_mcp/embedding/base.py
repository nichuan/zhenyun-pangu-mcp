"""Embedding provider 的统一接口与响应校验。"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Any


class EmbeddingError(RuntimeError):
    """Embedding API 调用或响应校验失败。"""


class EmbeddingConfigurationError(EmbeddingError):
    """Embedding provider 配置不完整或不受支持。"""


class EmbeddingProvider(ABC):
    """所有 provider 必须实现的 document/query 双通道接口。"""

    provider_name: str
    vector_column: str

    @property
    @abstractmethod
    def dimension(self) -> int:
        """返回向量维度。"""

    @property
    @abstractmethod
    def model_name(self) -> str:
        """返回模型名。"""

    @abstractmethod
    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        """将文档文本批量转成用于入库的向量。"""

    @abstractmethod
    def embed_query(self, text: str) -> list[float]:
        """将用户查询转成用于检索的向量。"""

    @staticmethod
    def to_literal(vector: list[float]) -> str:
        """转成 Postgres pgvector 接受的文本字面量。"""
        return "[" + ",".join(f"{value:.8g}" for value in vector) + "]"


def validate_embeddings(
    raw_embeddings: Any,
    expected_count: int,
    dimension: int,
    provider_name: str,
) -> list[list[float]]:
    """严格校验第三方返回的向量，禁止把坏响应写入数据库。"""
    if not isinstance(raw_embeddings, list) or len(raw_embeddings) != expected_count:
        actual = len(raw_embeddings) if isinstance(raw_embeddings, list) else type(raw_embeddings).__name__
        raise EmbeddingError(
            f"{provider_name} embedding 返回数量异常：期望 {expected_count}，实际 {actual}"
        )

    checked: list[list[float]] = []
    for index, vector in enumerate(raw_embeddings):
        if not isinstance(vector, (list, tuple)) or len(vector) != dimension:
            actual = len(vector) if isinstance(vector, (list, tuple)) else type(vector).__name__
            raise EmbeddingError(
                f"{provider_name} embedding 第 {index} 条维度异常："
                f"期望 {dimension}，实际 {actual}"
            )
        try:
            converted = [float(value) for value in vector]
        except (TypeError, ValueError) as exc:
            raise EmbeddingError(
                f"{provider_name} embedding 第 {index} 条包含不可转换的数值"
            ) from exc
        if not all(math.isfinite(value) for value in converted):
            raise EmbeddingError(f"{provider_name} embedding 第 {index} 条包含非有限数值")
        checked.append(converted)
    return checked
