"""Tests for providers: mock determinism, fallback chains, OpenRouter wiring."""

import pytest

from swarmd.router.providers import (
    FallbackRouter,
    LLMRequest,
    MockProvider,
    OpenRouterProvider,
    ProviderError,
    _ModelHealth,
    make_router,
    request_to_json,
)


async def test_mock_provider_is_deterministic() -> None:
    p = MockProvider()
    req = LLMRequest(prompt="enrich this lead", temperature=0.2)
    r1 = await p.complete(req)
    r2 = await p.complete(req)
    assert r1.text == r2.text
    assert r1.provider == "mock"


async def test_mock_temperature_changes_output() -> None:
    """Same prompt at different temperatures is a different request."""
    p = MockProvider()
    r1 = await p.complete(LLMRequest(prompt="x", temperature=0.2))
    r2 = await p.complete(LLMRequest(prompt="x", temperature=0.9))
    assert r1.text != r2.text


async def test_fallback_router_serves_from_first_healthy() -> None:
    router = FallbackRouter([MockProvider()])
    resp = await router.complete(LLMRequest(prompt="hi"))
    assert resp.provider == "mock"


async def test_fallback_router_skips_broken_provider() -> None:
    class Broken(MockProvider):
        name = "broken"

        async def complete(self, request: LLMRequest):  # type: ignore[override]
            raise ProviderError("simulated outage")

    router = FallbackRouter([Broken(), MockProvider()])
    resp = await router.complete(LLMRequest(prompt="hi"))
    assert resp.provider == "mock"


async def test_fallback_router_raises_when_all_fail() -> None:
    class Broken(MockProvider):
        async def complete(self, request: LLMRequest):  # type: ignore[override]
            raise ProviderError("down")

    router = FallbackRouter([Broken(), Broken()])
    with pytest.raises(ProviderError, match="all providers failed"):
        await router.complete(LLMRequest(prompt="hi"))


def test_openrouter_requires_api_key() -> None:
    import os

    saved = os.environ.pop("OPENROUTER_API_KEY", None)
    try:
        with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
            OpenRouterProvider(api_key="")
    finally:
        if saved is not None:
            os.environ["OPENROUTER_API_KEY"] = saved


def test_openrouter_default_models_are_free() -> None:
    """Every default model must be a free-tier model — cost control by construction."""
    for m in OpenRouterProvider.DEFAULT_FREE_MODELS if hasattr(
        OpenRouterProvider, "DEFAULT_FREE_MODELS"
    ) else __import__("swarmd.router.providers", fromlist=["DEFAULT_FREE_MODELS"]).DEFAULT_FREE_MODELS:
        assert m.endswith(":free"), f"non-free model in defaults: {m}"


def test_health_scoring_demotes_errors() -> None:
    h_ok, h_bad = _ModelHealth(), _ModelHealth()
    h_ok.record_success()
    h_bad.record_error()
    assert h_bad.score() > h_ok.score()


def test_simulated_mode_requires_the_explicit_flag(monkeypatch) -> None:
    """There is no unmarked mock on a user-facing path (ADR-006)."""
    monkeypatch.delenv("SWARMD_SIMULATED_PROVIDER", raising=False)
    with pytest.raises(RuntimeError, match="SWARMD_SIMULATED_PROVIDER"):
        make_router("simulated")


def test_simulated_mode_returns_the_tainted_provider(monkeypatch) -> None:
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")
    assert make_router("simulated").name == "simulated"


def test_mock_is_an_alias_that_now_goes_through_the_tainted_path(monkeypatch) -> None:
    """The frozen LeadOps example still works, but not with an unmarked mock."""
    monkeypatch.setenv("SWARMD_SIMULATED_PROVIDER", "true")
    assert make_router("mock").name == "simulated"


def test_a_missing_key_raises_rather_than_downgrading_to_mock(monkeypatch) -> None:
    """A crash is visible; quietly fabricated output is not.

    The previous behaviour degraded to an unmarked mock 'rather than crashing a
    demo run', which is exactly the trade ADR-006 refuses.
    """
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="OPENROUTER_API_KEY"):
        make_router("openrouter")


def test_an_unknown_mode_is_refused() -> None:
    with pytest.raises(ValueError, match="unknown router mode"):
        make_router("wishful")


def test_request_serialization_is_stable() -> None:
    a = request_to_json(LLMRequest(prompt="p", system="s", temperature=0.5))
    b = request_to_json(LLMRequest(prompt="p", system="s", temperature=0.5))
    assert a == b
