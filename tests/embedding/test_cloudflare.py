import os
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from zhenyun_pangu_mcp.embedding.base import EmbeddingError  # noqa: E402
from zhenyun_pangu_mcp.embedding.cloudflare import CloudflareEmbeddingProvider  # noqa: E402


class FakeHttp:
    """模拟 Workers AI REST 响应（按请求文本条数动态返回）。"""

    def __init__(self, dimension=1024):
        self.dimension = dimension
        self.calls = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"url": url, "json": json})
        count = len(json["text"]) if json and "text" in json else 1
        payload = {
            "data": [[0.1] * self.dimension for _ in range(count)],
            "shape": [count, self.dimension],
        }
        response = SimpleNamespace(
            raise_for_status=lambda: None,
            json=lambda: {"success": True, "result": payload},
        )
        return response


def _provider(http, dimension=1024):
    return CloudflareEmbeddingProvider("test-token", "test-account", dimension=dimension, http_client=http)


def test_cf_uses_batch_text_and_returns_vectors():
    http = FakeHttp()
    provider = _provider(http)

    vectors = provider.embed_documents(["文档一", "文档二"])
    query = provider.embed_query("查询")

    assert provider.model_name == "@cf/qwen/qwen3-embedding-0.6b"
    assert provider.dimension == 1024
    assert provider.vector_column == "embedding"
    assert len(vectors) == 2 and len(query) == 1024
    assert "/accounts/test-account/ai/run/@cf/qwen/qwen3-embedding-0.6b" in http.calls[0]["url"]
    assert http.calls[0]["json"] == {"text": ["文档一", "文档二"]}


def test_cf_rejects_wrong_dimension_without_fake_vector():
    http = FakeHttp(dimension=8)
    provider = _provider(http)
    with pytest.raises(EmbeddingError, match="维度异常"):
        provider.embed_query("查询")


def test_cf_rejects_error_response():
    class FailHttp:
        def post(self, *args, **kwargs):
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"success": False, "errors": [{"message": "quota exceeded"}]},
            )

    provider = _provider(FailHttp())
    with pytest.raises(EmbeddingError, match="quota exceeded"):
        provider.embed_query("查询")


def test_cf_requires_credentials():
    with pytest.raises(Exception, match="CF_API_TOKEN"):
        CloudflareEmbeddingProvider("", "acc")
    with pytest.raises(Exception, match="CF_ACCOUNT_ID"):
        CloudflareEmbeddingProvider("tok", "")
