import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from zhenyun_pangu_mcp import config  # noqa: E402
from zhenyun_pangu_mcp.embedding import factory  # noqa: E402
from zhenyun_pangu_mcp.embedding.base import EmbeddingConfigurationError  # noqa: E402


def test_factory_builds_cloudflare_provider(monkeypatch):
    monkeypatch.setattr(config, "get_cf_api_token", lambda: "test-token")
    monkeypatch.setattr(config, "get_cf_account_id", lambda: "test-account")
    monkeypatch.setattr(factory, "CloudflareEmbeddingProvider", lambda **kwargs: object())

    assert factory.create_embedding_provider().__class__ is object


def test_factory_reports_missing_credentials(monkeypatch):
    monkeypatch.setattr(config, "get_cf_api_token", lambda: "")
    monkeypatch.setattr(config, "get_cf_account_id", lambda: "test-account")
    with pytest.raises(EmbeddingConfigurationError, match="CF_API_TOKEN"):
        factory.create_embedding_provider()

    monkeypatch.setattr(config, "get_cf_api_token", lambda: "test-token")
    monkeypatch.setattr(config, "get_cf_account_id", lambda: "")
    with pytest.raises(EmbeddingConfigurationError, match="CF_ACCOUNT_ID"):
        factory.create_embedding_provider()
