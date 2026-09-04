import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from zhenyun_pangu_mcp import config  # noqa: E402
from zhenyun_pangu_mcp.embedding import factory  # noqa: E402
from zhenyun_pangu_mcp.embedding.base import EmbeddingConfigurationError  # noqa: E402


def test_factory_switches_provider(monkeypatch):
    monkeypatch.setattr(config, "get_voyage_api_key", lambda: "test-key")
    monkeypatch.setattr(config, "get_nvidia_api_key", lambda: "test-key")
    monkeypatch.setattr(config, "get_embedding_provider_name", lambda: "voyage")
    monkeypatch.setattr(factory, "VoyageEmbeddingProvider", lambda **kwargs: object())
    monkeypatch.setattr(factory, "NvidiaEmbeddingProvider", lambda **kwargs: object())

    assert factory.create_embedding_provider().__class__ is object

    monkeypatch.setattr(config, "get_embedding_provider_name", lambda: "nvidia")
    assert factory.create_embedding_provider().__class__ is object


def test_factory_reports_missing_key(monkeypatch):
    monkeypatch.setattr(config, "get_voyage_api_key", lambda: "")
    with pytest.raises(EmbeddingConfigurationError, match="VOYAGE_API_KEY"):
        factory.create_embedding_provider("voyage")
