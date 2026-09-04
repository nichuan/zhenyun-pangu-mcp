import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from zhenyun_pangu_mcp.embedding.base import EmbeddingError  # noqa: E402
from zhenyun_pangu_mcp.embedding.voyage import VoyageEmbeddingProvider  # noqa: E402


class FakeVoyageClient:
    def __init__(self, dimension=2048):
        self.dimension = dimension
        self.calls = []

    def embed(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(embeddings=[[0.1] * self.dimension for _ in kwargs["texts"]])


def test_voyage_uses_document_and_query_modes_and_2048_dimensions():
    client = FakeVoyageClient()
    provider = VoyageEmbeddingProvider("test-key", client=client)

    documents = provider.embed_documents(["文档一", "文档二"])
    query = provider.embed_query("查询")

    assert provider.model_name == "voyage-4"
    assert provider.dimension == 2048
    assert len(documents) == 2
    assert len(documents[0]) == 2048
    assert len(query) == 2048
    assert client.calls[0]["input_type"] == "document"
    assert client.calls[0]["output_dimension"] == 2048
    assert client.calls[0]["output_dtype"] == "float"
    assert client.calls[1]["input_type"] == "query"


def test_voyage_rejects_empty_response():
    class EmptyClient:
        def embed(self, **_kwargs):
            return SimpleNamespace(embeddings=[])

    provider = VoyageEmbeddingProvider("test-key", client=EmptyClient())
    with pytest.raises(EmbeddingError, match="返回数量异常"):
        provider.embed_query("查询")


def test_voyage_requires_api_key():
    with pytest.raises(Exception, match="VOYAGE_API_KEY"):
        VoyageEmbeddingProvider("")
