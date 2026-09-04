import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from zhenyun_pangu_mcp.embedding.base import EmbeddingError  # noqa: E402
from zhenyun_pangu_mcp.embedding.nvidia import NvidiaEmbeddingProvider  # noqa: E402


class FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        return None

    def json(self):
        return self.body


class FakeHttp:
    def __init__(self, body):
        self.body = body
        self.calls = []

    def post(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return FakeResponse(self.body)


def test_nvidia_preserves_old_column_and_supports_batch_documents():
    http = FakeHttp({"data": [{"index": 1, "embedding": [0.2] * 2048}, {"index": 0, "embedding": [0.1] * 2048}]})
    provider = NvidiaEmbeddingProvider("test-key", http_client=http)

    vectors = provider.embed_documents(["a", "b"])

    assert provider.vector_column == "embedding"
    assert len(vectors) == 2
    assert vectors[0][0] == 0.1
    assert http.calls[0][1]["json"]["input"] == ["a", "b"]


def test_nvidia_rejects_wrong_dimension_without_fake_vector():
    http = FakeHttp({"data": [{"embedding": [0.2]}]})
    provider = NvidiaEmbeddingProvider("test-key", http_client=http)

    with pytest.raises(EmbeddingError, match="维度异常"):
        provider.embed_query("查询")
