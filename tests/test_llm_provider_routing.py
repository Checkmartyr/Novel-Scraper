"""Tests for NouSetsu model/provider routing integration."""

import os
import sys
from types import ModuleType, SimpleNamespace

import pytest
from pydantic import BaseModel
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from src.agent.llm.client import LLMClient


def _install_nousetsu_llm_factory(monkeypatch, factory):
    package = ModuleType("nousetsu")
    package.__path__ = []
    agents = ModuleType("nousetsu.agents")
    agents.__path__ = []
    llm = ModuleType("nousetsu.agents.llm")
    setattr(llm, "get_llm", factory)
    scraper = ModuleType("nousetsu.scraper")
    scraper.__path__ = []
    llm_config = ModuleType("nousetsu.scraper.llm_config")

    def resolve_settings():
        return SimpleNamespace(
            model=(
                os.environ.get("NOVEL_SCRAPER_MODEL", "").strip()
                or os.environ.get("NOVEL_MODEL", "").strip()
                or "gemini-3.1-flash-lite"
            ),
            fallback_model=(
                os.environ.get("NOVEL_FALLBACK_MODEL", "").strip()
                or "gemini-3.5-flash-lite"
            ),
        )

    setattr(llm_config, "resolve_scraper_llm_settings", resolve_settings)
    setattr(scraper, "resolve_scraper_llm_settings", resolve_settings)
    monkeypatch.setitem(sys.modules, "nousetsu", package)
    monkeypatch.setitem(sys.modules, "nousetsu.agents", agents)
    monkeypatch.setitem(sys.modules, "nousetsu.agents.llm", llm)
    monkeypatch.setitem(sys.modules, "nousetsu.scraper", scraper)
    monkeypatch.setitem(sys.modules, "nousetsu.scraper.llm_config", llm_config)


def test_client_routes_to_scraper_model_and_global_fallback(monkeypatch):
    calls = []
    provider_model = object()

    def fake_get_llm(**kwargs):
        calls.append(kwargs)
        return provider_model

    _install_nousetsu_llm_factory(monkeypatch, fake_get_llm)
    monkeypatch.setenv("NOVEL_SCRAPER_MODEL", "openrouter:anthropic/claude-3.7-sonnet")
    monkeypatch.setenv("NOVEL_MODEL", "openai:gpt-4.1")
    monkeypatch.setenv("NOVEL_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = LLMClient()

    assert client.model == "openrouter:anthropic/claude-3.7-sonnet"
    assert client.is_available is True
    assert calls[0]["model_name"] == "openrouter:anthropic/claude-3.7-sonnet"
    assert calls[0]["fallback_model"] == "gemini-3.5-flash-lite"


def test_client_inherits_novel_model_when_scraper_model_is_blank(monkeypatch):
    calls = []

    def fake_get_llm(**kwargs):
        calls.append(kwargs)
        return object()

    _install_nousetsu_llm_factory(monkeypatch, fake_get_llm)
    monkeypatch.delenv("NOVEL_SCRAPER_MODEL", raising=False)
    monkeypatch.setenv("NOVEL_MODEL", "openai:gpt-4.1")
    monkeypatch.delenv("NOVEL_FALLBACK_MODEL", raising=False)
    monkeypatch.delenv("DEFAULT_MODEL", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)

    client = LLMClient()

    assert client.model == "openai:gpt-4.1"
    assert calls[0]["model_name"] == "openai:gpt-4.1"
    assert calls[0]["fallback_model"] == "gemini-3.5-flash-lite"


def test_client_uses_heuristic_fallback_without_provider_credentials(monkeypatch):
    class MockNovelLLM:
        pass

    _install_nousetsu_llm_factory(monkeypatch, lambda **kwargs: MockNovelLLM())
    monkeypatch.setenv("NOVEL_SCRAPER_MODEL", "openai:gpt-4.1")
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    client = LLMClient()

    assert client.is_available is False


def test_client_uses_configured_fallback_when_primary_provider_is_unavailable(monkeypatch):
    class MockNovelLLM:
        pass

    class ConfiguredProvider:
        pass

    configured_fallback = ConfiguredProvider()
    routed = SimpleNamespace(primary=MockNovelLLM(), fallback=configured_fallback)
    _install_nousetsu_llm_factory(monkeypatch, lambda **kwargs: routed)
    monkeypatch.setenv("NOVEL_SCRAPER_MODEL", "openrouter:anthropic/claude-3.7-sonnet")
    monkeypatch.setenv("NOVEL_FALLBACK_MODEL", "openai:gpt-4.1-mini")
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    client = LLMClient()

    assert client.is_available is True
    assert client._chat_model is configured_fallback


def test_client_does_not_fall_back_to_unconfigured_mock_provider(monkeypatch):
    class MockNovelLLM:
        pass

    class ConfiguredProvider:
        pass

    configured_primary = ConfiguredProvider()
    routed = SimpleNamespace(primary=configured_primary, fallback=MockNovelLLM())
    _install_nousetsu_llm_factory(monkeypatch, lambda **kwargs: routed)
    monkeypatch.setenv("NOVEL_SCRAPER_MODEL", "openai:gpt-4.1")
    monkeypatch.setenv("NOVEL_FALLBACK_MODEL", "gemini-3.5-flash-lite")
    for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    client = LLMClient()

    assert client.is_available is True
    assert client._chat_model is configured_primary


@pytest.mark.asyncio
async def test_routed_client_parses_json_and_tracks_provider_usage(monkeypatch):
    class ResultSchema(BaseModel):
        answer: str

    class FixedProviderModel(BaseChatModel):
        @property
        def _llm_type(self) -> str:
            return "fixed-provider-test"

        def _generate(self, messages: list[BaseMessage], stop=None, run_manager=None, **kwargs):
            message = AIMessage(
                content='{"answer":"ok"}',
                usage_metadata={"input_tokens": 11, "output_tokens": 3, "total_tokens": 14},
            )
            return ChatResult(generations=[ChatGeneration(message=message)])

    _install_nousetsu_llm_factory(monkeypatch, lambda **kwargs: FixedProviderModel())
    monkeypatch.setenv("NOVEL_SCRAPER_MODEL", "openai:gpt-4.1")
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)

    from src.agent.llm.tracker import token_tracker

    token_tracker.reset()
    try:
        result = await LLMClient().generate_json("Return JSON", ResultSchema)
        summary = token_tracker.get_summary()
    finally:
        token_tracker.reset()

    assert result.answer == "ok"
    assert summary["prompt_tokens"] == 11
    assert summary["completion_tokens"] == 3
    assert summary["total_tokens"] == 14
    assert summary["last_call"]["call_type"] == "scraper"


def test_standalone_client_keeps_explicit_gemini_compatibility():
    from src.agent.llm.client import ChatGeminiInteractions

    client = LLMClient(api_key="test-key", model="gemini-standalone-test")

    assert client.model == "gemini-standalone-test"
    assert isinstance(client._chat_model, ChatGeminiInteractions)
    assert client.is_available is True


def test_batch_runner_reuses_injected_client_for_self_healing():
    from src.agent.ch.analyzer import DOMStructurePlan
    from src.scraper.batch_runner import BatchScraperRunner

    shared_client = object()
    runner = BatchScraperRunner(
        novel_title="Test novel",
        initial_plan=DOMStructurePlan(title_selector="h1", content_selector="article"),
        llm_client=shared_client,
    )

    assert runner.healer.llm is shared_client
