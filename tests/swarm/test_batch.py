"""Batched generation.

CAPACITY.md claims a 500-agent run fits fifteen minutes partly because one call
serves a whole pool. That claim was previously unbacked -- every agent made its
own call -- so these tests assert on COUNTED provider calls rather than on the
run completing, which it did either way.

The second property is the one that is easy to lose: the variants must be
DIFFERENT. A batch that returns the same candidate K times halves nothing and
turns population search into K agents grading one answer.
"""

from __future__ import annotations

import pytest

from swarmd.swarm.batch import (
    SEPARATOR,
    Batch,
    batch_prompt,
    generate_batch,
    split_candidates,
)
from swarmd.swarm.run import SwarmRun
from tests.swarm.test_run import ScriptedProvider


# --- parsing ----------------------------------------------------------------


def test_candidates_split_on_the_separator():
    text = f"first\n{SEPARATOR}\nsecond\n{SEPARATOR}\nthird"
    assert split_candidates(text, expected=3) == ("first", "second", "third")


def test_a_response_without_the_separator_degrades_to_one_candidate():
    """An optimisation that can break correctness is not one."""
    assert split_candidates("just the one", expected=4) == ("just the one",)


def test_empty_sections_are_dropped():
    text = f"{SEPARATOR}\nfirst\n{SEPARATOR}\n\n{SEPARATOR}\nsecond"
    assert split_candidates(text, expected=4) == ("first", "second")


def test_extra_candidates_are_truncated_so_pool_position_stays_stable():
    text = SEPARATOR.join(["a", "b", "c", "d"])
    assert len(split_candidates(text, expected=2)) == 2


def test_an_empty_response_yields_no_candidates():
    assert split_candidates("   ", expected=3) == ()


def test_the_batch_instruction_names_the_count_and_the_separator():
    prompt = batch_prompt("STEP: one", 5)
    assert "5 SEPARATE" in prompt
    assert SEPARATOR in prompt


# --- distribution -----------------------------------------------------------


def test_each_agent_gets_its_own_variant():
    batch = Batch(("a", "b", "c"), requested=3, calls=1, cost_credits=0.0)
    assert [batch.for_agent(i) for i in range(3)] == ["a", "b", "c"]


def test_variants_wrap_when_the_model_returned_fewer_than_asked():
    """Sharing a variant loses diversity. Re-calling to fill the gap would
    spend exactly the requests batching exists to save."""
    batch = Batch(("a", "b"), requested=4, calls=1, cost_credits=0.0)
    assert [batch.for_agent(i) for i in range(4)] == ["a", "b", "a", "b"]


def test_saved_calls_is_the_difference_against_one_call_per_agent():
    assert Batch(("a",) * 8, requested=8, calls=1, cost_credits=0.0).saved_calls == 7


def test_an_empty_batch_hands_out_nothing_rather_than_raising():
    """A failed batch must fall back to individual generation, not kill the node."""
    assert Batch((), requested=4, calls=1, cost_credits=0.0).for_agent(0) == ""


# --- the call ---------------------------------------------------------------


class CountingProvider(ScriptedProvider):
    """ScriptedProvider already counts calls; this only records the request."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.last_max_tokens = 0

    async def complete(self, request):
        self.last_max_tokens = request.max_tokens
        return await super().complete(request)


async def _batch(provider, k):
    return await generate_batch(
        provider=provider, prompt="STEP: solve\nREQUIRED: produce out.json",
        k=k, max_tokens=256, temperature=0.7, system="sys", stage="solve",
    )


async def test_a_batch_of_k_costs_one_call():
    provider = CountingProvider()
    batch = await _batch(provider, 8)
    assert provider.calls == 1
    assert batch.calls == 1


async def test_max_tokens_scales_with_the_batch():
    """Without this the model truncates mid-candidate and the batch silently
    returns fewer variants than it was asked for."""
    provider = CountingProvider()
    await _batch(provider, 4)
    assert provider.last_max_tokens > 256


async def test_max_tokens_is_capped_for_a_wide_pool():
    """A free-tier model has no context window for 32 x 512."""
    provider = CountingProvider()
    await _batch(provider, 32)
    assert provider.last_max_tokens <= 8192


async def test_a_batch_of_one_skips_the_batch_instruction():
    """Extra instructions the model has to read, for no candidates gained."""
    class Recorder(CountingProvider):
        prompt = ""

        async def complete(self, request):
            Recorder.prompt = request.prompt
            return await super().complete(request)

    await _batch(Recorder(), 1)
    assert SEPARATOR not in Recorder.prompt


async def test_a_provider_failure_returns_an_empty_batch_rather_than_raising():
    class Broken:
        async def complete(self, request):
            raise RuntimeError("provider down")

    batch = await _batch(Broken(), 4)
    assert batch.variants == ()


# --- inside a real run ------------------------------------------------------


async def test_a_pool_of_n_does_not_make_n_generation_calls():
    """The capacity claim, as a counted assertion.

    Before batching this run made one call per agent per node. The exact call
    count depends on how many agents need repairs, so the assertion is that it
    is far below one-per-agent rather than an exact figure -- a brittle exact
    count would fail on an unrelated criterion change and teach people to
    update the number instead of reading it.
    """
    provider = CountingProvider()
    run = SwarmRun(provider, profile="smoke", agents=32)
    result = await run.run("summarise the source records")

    assert result.status == "completed"
    agents_run = len(result.results)
    assert agents_run >= 8, "pool too small for this test to mean anything"
    assert provider.calls < agents_run, (
        f"{provider.calls} calls for {agents_run} agents: batching saved nothing"
    )


async def test_the_pool_receives_genuinely_different_candidates(monkeypatch):
    """A batch returning one candidate K times is not a population.

    Uses the simulated provider rather than the scripted double: the double
    answers by prompt shape with one fixed string, so it cannot express a
    multi-candidate response at all.
    """
    from swarmd.router.simulated import ENV_FLAG, SimulatedProvider

    monkeypatch.setenv(ENV_FLAG, "true")
    run = SwarmRun(SimulatedProvider(), profile="smoke", agents=16)
    result = await run.run("summarise the source records")

    by_node: dict[str, set[str]] = {}
    for outcome in result.results:
        by_node.setdefault(outcome.node, set()).add(outcome.candidate.output)
    assert any(len(outputs) > 1 for outputs in by_node.values()), (
        "every agent on every node produced identical output"
    )


async def test_saved_calls_are_reported_as_an_event():
    """Operators should see the saving, not be told about it in a doc."""
    seen: list[dict] = []
    run = SwarmRun(
        ScriptedProvider(), profile="smoke", agents=16,
        on_event=lambda e: seen.append(e),
    )
    await run.run("summarise the source records")

    batches = [e for e in seen if e["kind"] == "batch_generated"]
    assert batches
    assert all(e["calls"] == 1 for e in batches)
    assert sum(e["saved_calls"] for e in batches) > 0


@pytest.mark.parametrize("agents", [2, 8, 32])
async def test_the_run_completes_at_every_pool_width(agents):
    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=agents)
    result = await run.run("summarise the source records")
    assert result.status == "completed"
