"""LLM provider selection — config change, not code change."""

from __future__ import annotations

import pytest

from reconciliation.config.settings import LlmSettings
from reconciliation.llm import FakeLlmClient, LlmAuthError, build_llm_client


def test_fake_provider_needs_no_credentials():
    client = build_llm_client(LlmSettings(provider="fake", model="claude-sonnet-5"))
    assert isinstance(client, FakeLlmClient)
    assert "claude-sonnet-5" in client.model_id


def test_unknown_provider_raises():
    with pytest.raises(ValueError, match="unknown llm provider"):
        build_llm_client(LlmSettings(provider="does-not-exist"))


def test_anthropic_provider_without_api_key_raises_llm_auth_error(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LlmAuthError):
        build_llm_client(LlmSettings(provider="anthropic"))


def test_settings_default_to_fake_and_zero_temperature():
    """Deny-by-default posture: an unconfigured environment must not silently
    call a paid API, and must not sample (spec 09 reproducibility)."""
    settings = LlmSettings()
    assert settings.provider == "fake"
    assert settings.temperature == 0.0
