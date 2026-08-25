"""Tests for harnesses: base contract, LLM structured output, verify checks, draft rendering."""

import asyncio

import pytest
from pydantic import BaseModel

from swarmd.harnesses.base import Harness
from swarmd.harnesses.draft import DraftHarness, Persona
from swarmd.harnesses.fetch import DisallowedHostError, FetchHarness, _TokenBucket
from swarmd.harnesses.llm import LLMHarness
from swarmd.harnesses.verify import (
    VerifyHarness,
    forbidden_content_check,
    range_check,
    schema_check,
)
from swarmd.router.providers import LLMResponse, MockProvider, Provider

# ---- base harness ---------------------------------------------------------


async def test_harness_tool_registration_and_call() -> None:
    h = Harness(name="worker")

    async def echo(value: str) -> str:
        return value.upper()

    h.register_tool("echo", echo)
    assert await h.call("echo", value="hi") == "HI"

    with pytest.raises(KeyError):
        await h.call("missing")
    with pytest.raises(ValueError, match="already registered"):
        h.register_tool("echo", echo)


# ---- LLM harness ----------------------------------------------------------


class Score(BaseModel):
    score: int
    reason: str


async def test_llm_structured_output_with_mock() -> None:
    # The mock returns prose without JSON; the repair loop should fail loudly.
    h = LLMHarness(MockProvider(), temperature=0.2)
    with pytest.raises(ValueError, match="structured output failed"):
        await h.structured("score this", Score)


class _JSONProvider(Provider):
    """Returns a fixed JSON payload — simulates a compliant model."""

    name = "json-mock"

    def __init__(self, payload: str) -> None:
        self.payload = payload

    async def complete(self, request):  # type: ignore[override]
        return LLMResponse(
            text=self.payload, provider=self.name, model="t", latency_s=0.0
        )


async def test_llm_structured_output_parses_valid_json() -> None:
    p = _JSONProvider('{"score": 8, "reason": "strong ICP fit"}')
    h = LLMHarness(p)
    out = await h.structured("score", Score)
    assert out.score == 8
    assert out.reason == "strong ICP fit"


async def test_llm_structured_output_repairs_chatty_json() -> None:
    p = _JSONProvider('Here you go: {"score": 3, "reason": "weak fit"} — hope it helps!')
    h = LLMHarness(p)
    out = await h.structured("score", Score)
    assert out.score == 3


def test_llm_temperature_policy_is_explicit() -> None:
    verifier = LLMHarness(MockProvider(), temperature=0.2)
    drafter = LLMHarness(MockProvider(), temperature=0.7)
    assert verifier.temperature < drafter.temperature


# ---- verify harness -------------------------------------------------------


async def test_schema_check_catches_missing_fields() -> None:
    v = VerifyHarness("v").add_check(schema_check(["company", "email"]))
    ok = await v.verify({"company": "acme", "email": "a@b.c"})
    bad = await v.verify({"company": "acme"})
    assert ok.ok
    assert not bad.ok and "email" in (bad.reason or "")


async def test_range_and_forbidden_checks() -> None:
    v = (
        VerifyHarness("v")
        .add_check(range_check("score", 0, 10))
        .add_check(forbidden_content_check(["free money"]))
    )
    assert (await v.verify({"score": 5})).ok
    r1 = await v.verify({"score": 11})
    r2 = await v.verify({"score": 5, "note": "get free money now"})
    assert not r1.ok and "outside" in (r1.reason or "")
    assert not r2.ok and "banned" in (r2.reason or "")


async def test_first_failing_check_short_circuits() -> None:
    calls = [0]

    async def second(item: dict) -> str | None:
        calls[0] += 1
        return "never reached"

    v = VerifyHarness("v").add_check(schema_check(["x"])).add_check(second)
    res = await v.verify({})
    assert not res.ok
    assert calls[0] == 0  # second check never ran


# ---- draft harness --------------------------------------------------------


def test_draft_rendering_with_persona_and_footer() -> None:
    persona = Persona(name="Ada", title="AE", company="SwarmCo", tone_words=["warm"])
    d = DraftHarness(persona, compliance_footer="Reply STOP to opt out.")
    draft = d.render(
        "Hi {first_name}, saw {company} is hiring {role}.", 
        {"first_name": "Sam", "company": "Acme", "role": "SREs"},
    )
    assert "Sam" in draft["body"] and "Acme" in draft["subject"]
    assert "Reply STOP" in draft["body"]
    assert "Ada" in draft["body"] and "SwarmCo" in draft["body"]


def test_draft_missing_placeholder_raises_loudly() -> None:
    d = DraftHarness(Persona("A", "T", "C", []))
    with pytest.raises(KeyError, match="placeholder"):
        d.render("Hi {missing_field}", {"first_name": "x"})


# ---- fetch harness (offline parts) ----------------------------------------


def test_fetch_allowlist_enforced_without_network() -> None:
    h = FetchHarness(allowed_hosts=["example.com"])

    async def go():
        with pytest.raises(DisallowedHostError):
            await h.fetch_text("https://evil.example.net/x")

    asyncio.run(go())


async def test_token_bucket_burst_then_throttle() -> None:
    bucket = _TokenBucket(rate_per_s=50, burst=3)
    t0 = asyncio.get_event_loop().time()
    for _ in range(3):
        await bucket.acquire()  # burst: instant
    instant = asyncio.get_event_loop().time() - t0
    assert instant < 0.05

    t0 = asyncio.get_event_loop().time()
    await bucket.acquire()  # beyond burst: must wait ~20ms at 50/s
    waited = asyncio.get_event_loop().time() - t0
    assert waited >= 0.01
