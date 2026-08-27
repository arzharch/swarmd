"""Simulated-provider tests.

Only one property here really matters, and it is not determinism: **simulated
output must be impossible to mistake for real output downstream**. Everything
else is convenience. The tests are written so that a future refactor which
quietly drops the taint fails loudly.
"""

from __future__ import annotations

import pytest

from swarmd.ledger import (
    CostAccount,
    InMemoryLedger,
    SimulatedDataRefused,
    price_for,
    refuse_simulated,
)
from swarmd.router.pool import ProviderPool
from swarmd.router.providers import LLMRequest, ProviderError
from swarmd.router.simulated import ENV_FLAG, SimulatedProvider, simulation_enabled


@pytest.fixture
def simulated_on(monkeypatch):
    monkeypatch.setenv(ENV_FLAG, "true")
    return True


# --- the fence -------------------------------------------------------------


def test_provider_refuses_to_construct_without_the_explicit_flag(monkeypatch):
    """It must never be reachable as a silent fallback for a missing key."""
    monkeypatch.delenv(ENV_FLAG, raising=False)
    with pytest.raises(RuntimeError, match=ENV_FLAG):
        SimulatedProvider()


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_flag_accepts_the_usual_truthy_spellings(monkeypatch, value):
    monkeypatch.setenv(ENV_FLAG, value)
    assert simulation_enabled() is True


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  "])
def test_flag_is_off_for_anything_else(monkeypatch, value):
    monkeypatch.setenv(ENV_FLAG, value)
    assert simulation_enabled() is False


async def test_every_row_it_produces_is_tainted(simulated_on):
    """The taint rides on the DATA, so no config mistake can strip it."""
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    pool = ProviderPool.from_env(account=acct)

    await pool.complete(LLMRequest(prompt="hello", max_tokens=32))

    rows = acct.ledger.rows()
    assert len(rows) == 1
    assert rows[0].simulated is True
    assert rows[0].provider == "simulated"


async def test_report_is_tainted_when_any_row_is(simulated_on):
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    pool = ProviderPool.from_env(account=acct)
    await pool.complete(LLMRequest(prompt="hello", max_tokens=32))

    report = acct.report()
    assert report["simulated"] is True
    assert report["simulated_rows"] == 1


def test_a_single_simulated_row_taints_an_otherwise_real_report():
    """Part-measured, part-invented is not a real run. It reports as simulated."""
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    for _ in range(9):
        acct.charge_call(provider="groq", model="m", tokens_in=10, tokens_out=5)
    acct.charge_call(
        provider="simulated", model="simulated-v1",
        tokens_in=10, tokens_out=5, simulated=True,
    )

    report = acct.report()
    assert report["simulated"] is True
    assert report["simulated_rows"] == 1
    assert report["rows"] == 10


def test_a_fully_real_report_is_not_tainted():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    acct.charge_call(provider="groq", model="m", tokens_in=10, tokens_out=5)
    assert acct.report()["simulated"] is False
    assert acct.report()["simulated_rows"] == 0


def test_refuse_simulated_blocks_publishing_a_tainted_report():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    acct.charge_call(
        provider="simulated", model="simulated-v1",
        tokens_in=1, tokens_out=1, simulated=True,
    )
    with pytest.raises(SimulatedDataRefused, match="eval"):
        refuse_simulated(acct.report(), context="eval")


def test_refuse_simulated_allows_a_clean_report():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    acct.charge_call(provider="groq", model="m", tokens_in=1, tokens_out=1)
    refuse_simulated(acct.report(), context="eval")  # must not raise


def test_simulated_provider_is_priced_so_the_ceiling_still_functions():
    """An unpriced provider would raise mid-run and look like a real bug."""
    assert price_for("simulated", "simulated-v1").cost(1_000_000, 1_000_000) == 0.0


# --- pool wiring -----------------------------------------------------------


async def test_simulation_replaces_the_pool_rather_than_joining_it(
    simulated_on, monkeypatch
):
    """Mixing real and simulated providers yields numbers meaning nothing."""
    monkeypatch.setenv("GROQ_API_KEY", "real-key")
    monkeypatch.setenv("GOOGLE_API_KEY", "real-key")

    pool = ProviderPool.from_env()
    rows = pool.status()
    assert [r["provider"] for r in rows] == ["simulated"]
    assert rows[0]["simulated"] is True


def test_pool_status_marks_real_providers_as_not_simulated(monkeypatch):
    monkeypatch.delenv(ENV_FLAG, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")
    pool = ProviderPool.from_env(include=["groq"])
    assert pool.status()[0]["simulated"] is False


# --- behaviour -------------------------------------------------------------


async def test_responses_are_deterministic(simulated_on):
    """Chaos-integrity hashes are only comparable if replay is identical."""
    p = SimulatedProvider(latency_s=0)
    req = LLMRequest(prompt="same prompt", temperature=0.2, max_tokens=32)
    first = await p.complete(req)
    second = await p.complete(req)
    assert first.text == second.text


async def test_different_prompts_give_different_responses(simulated_on):
    p = SimulatedProvider(latency_s=0)
    a = await p.complete(LLMRequest(prompt="prompt A", max_tokens=32))
    b = await p.complete(LLMRequest(prompt="prompt B", max_tokens=32))
    assert a.text != b.text


async def test_structured_requests_get_schema_valid_json(simulated_on):
    p = SimulatedProvider(latency_s=0)
    schema = (
        'Respond with ONLY a JSON object matching this schema:\n'
        '{"properties": {"score": {"type": "integer", "minimum": 0, '
        '"maximum": 10}, "reason": {"type": "string"}}}'
    )
    resp = await p.complete(LLMRequest(prompt=schema, max_tokens=64))

    import json

    payload = json.loads(resp.text)
    assert 0 <= payload["score"] <= 10, "declared bounds must be respected"
    assert isinstance(payload["reason"], str)


async def test_declared_bounds_are_respected_across_many_prompts(simulated_on):
    """Out-of-bounds values would look like model errors, not deliberate fiction."""
    p = SimulatedProvider(latency_s=0)
    import json

    for i in range(50):
        schema = (
            f'Prompt {i}. Respond with ONLY a JSON object matching this schema:\n'
            '{"properties": {"n": {"type": "integer", "minimum": 3, "maximum": 7}}}'
        )
        payload = json.loads((await p.complete(LLMRequest(prompt=schema))).text)
        assert 3 <= payload["n"] <= 7


async def test_default_latency_is_nonzero_so_concurrency_bugs_still_surface():
    """A provider that answers instantly hides every backpressure bug."""
    import os

    os.environ["SWARMD_SIMULATED_PROVIDER"] = "true"
    try:
        assert SimulatedProvider().latency_s > 0
    finally:
        del os.environ["SWARMD_SIMULATED_PROVIDER"]


async def test_seeded_failures_exercise_the_error_paths(simulated_on):
    """Always-succeeding fakes are how a system falls over on first real API."""
    p = SimulatedProvider(latency_s=0, failure_rate=1.0)
    with pytest.raises(ProviderError, match="simulated failure"):
        await p.complete(LLMRequest(prompt="anything"))


async def test_failures_are_deterministic_for_a_given_prompt(simulated_on):
    p = SimulatedProvider(latency_s=0, failure_rate=0.5)
    outcomes = []
    for _ in range(2):
        try:
            await p.complete(LLMRequest(prompt="stable prompt"))
            outcomes.append("ok")
        except ProviderError:
            outcomes.append("fail")
    assert outcomes[0] == outcomes[1]
