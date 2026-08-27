"""The semantic cache, actually wired to something.

`SemanticCache` and `CostAccount.charge_cache_hit` both existed for phases
without a single caller, so every report's `cache_hits` was a structural zero
while CAPACITY.md counted a 2.5x saving from caching. These tests exist to make
that impossible to regress into: they assert on counted provider calls and on
ledger rows, not on the cache's own hit counter, which would happily report
hits nobody consumed.
"""

from __future__ import annotations

import pytest

from swarmd.ledger import CostAccount, InMemoryLedger
from swarmd.router.cache import SemanticCache
from swarmd.router.cached import CachedProvider, cache_key
from swarmd.router.providers import LLMRequest, LLMResponse
from swarmd.swarm.run import SwarmRun
from tests.swarm.test_run import ScriptedProvider


class Counter:
    name = "counting"

    def __init__(self, provider: str = "groq", model: str = "llama-3.1-8b-instant"):
        self.calls = 0
        self.provider = provider
        self.model = model

    async def complete(self, request):
        self.calls += 1
        return LLMResponse(
            text=f"answer {self.calls}",
            provider=self.provider,
            model=self.model,
            latency_s=0.001,
            tokens_in=100,
            tokens_out=50,
        )


def _request(prompt="hello", *, temperature=0.7, max_tokens=256, system="s"):
    return LLMRequest(
        prompt=prompt, system=system, temperature=temperature,
        max_tokens=max_tokens, metadata={"stage": "worker"},
    )


def _account():
    return CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)


# --- the basic contract -----------------------------------------------------


async def test_a_repeated_request_does_not_reach_the_provider():
    provider = Counter()
    cached = CachedProvider(provider, SemanticCache())

    first = await cached.complete(_request())
    second = await cached.complete(_request())

    assert provider.calls == 1
    assert second.text == first.text


async def test_a_different_prompt_is_a_miss():
    provider = Counter()
    cached = CachedProvider(provider, SemanticCache())

    await cached.complete(_request("summarise the quarterly revenue figures"))
    await cached.complete(_request("write a haiku about the sea in winter"))

    assert provider.calls == 2


# --- what the key must include ---------------------------------------------


async def test_temperature_is_part_of_the_key():
    """Criterion synthesis runs N proposers whose diversity is partly sampling.

    Serving proposer 1's answer to proposers 2 and 3 turns a consensus of three
    into one opinion repeated three times -- and the merge would still report
    unanimous agreement.
    """
    provider = Counter()
    cached = CachedProvider(provider, SemanticCache())

    await cached.complete(_request(temperature=0.2))
    await cached.complete(_request(temperature=0.9))

    assert provider.calls == 2


async def test_the_system_prompt_is_part_of_the_key():
    provider = Counter()
    cached = CachedProvider(provider, SemanticCache())

    await cached.complete(_request(system="you are a planner"))
    await cached.complete(_request(system="you are a critic"))

    assert provider.calls == 2


async def test_max_tokens_is_part_of_the_key():
    provider = Counter()
    cached = CachedProvider(provider, SemanticCache())

    await cached.complete(_request(max_tokens=256))
    await cached.complete(_request(max_tokens=2048))

    assert provider.calls == 2


def test_simulated_and_live_entries_cannot_collide():
    """ADR-012: synthetic output must not be servable into a real run."""
    request = _request()
    assert cache_key(request, simulated=True) != cache_key(request, simulated=False)


# --- accounting -------------------------------------------------------------


async def test_a_hit_writes_a_ledger_row_rather_than_incrementing_a_counter():
    """Priced provider, so the avoided dollars are visible in the row."""
    provider = Counter(provider="openrouter", model="z-ai/glm-5.3-flash")
    account = _account()
    cached = CachedProvider(provider, SemanticCache(), account=account)

    await cached.complete(_request())
    await cached.complete(_request())

    report = account.report()
    assert report["cache_hits"] == 1
    rows = [r for r in account.ledger.rows() if r.kind == "cache_hit"]
    assert len(rows) == 1
    assert rows[0].cost_usd == 0.0
    assert rows[0].would_have_cost > 0.0


async def test_a_free_tier_hit_saves_no_dollars_and_is_still_recorded():
    """What the cache actually buys on a free tier is REQUESTS, not money.

    The row exists at zero saved dollars because the rate-limit budget it
    preserves is the scarce resource, and a report that only counted dollars
    would show the cache doing nothing on exactly the tier it matters most on.
    """
    provider = Counter()   # groq: free, priced at zero
    account = _account()
    cached = CachedProvider(provider, SemanticCache(), account=account)

    await cached.complete(_request())
    await cached.complete(_request())

    rows = [r for r in account.ledger.rows() if r.kind == "cache_hit"]
    assert len(rows) == 1
    assert rows[0].would_have_cost == 0.0
    assert provider.calls == 1


async def test_a_hit_costs_nothing_against_the_ceiling():
    provider = Counter()
    account = _account()
    cached = CachedProvider(provider, SemanticCache(), account=account)

    await cached.complete(_request())
    before = account.total_cost()
    await cached.complete(_request())

    assert account.total_cost() == before


async def test_a_hit_on_a_simulated_entry_stays_marked_simulated():
    """Otherwise a cached synthetic answer launders itself into a real report."""
    provider = Counter(provider="simulated", model="sim-1")
    account = _account()
    cached = CachedProvider(provider, SemanticCache(), account=account)

    await cached.complete(_request())
    await cached.complete(_request())

    rows = [r for r in account.ledger.rows() if r.kind == "cache_hit"]
    assert rows and all(r.simulated for r in rows)
    assert account.report()["simulated"]


async def test_the_account_reaches_the_wrapped_provider():
    """A proxy that drops the wrapped object's interface is how a cost ceiling
    stops being wired up."""
    class HasAccount(Counter):
        account = None

    provider = HasAccount()
    cached = CachedProvider(provider, SemanticCache())
    account = _account()
    cached.account = account

    assert provider.account is account
    assert cached.account is account


async def test_undefined_attributes_delegate_to_the_provider():
    cached = CachedProvider(Counter(), SemanticCache())
    assert cached.name == "counting"


# --- inside a run -----------------------------------------------------------


async def test_a_second_identical_run_makes_fewer_calls():
    """The saving is across runs, which is where the repetition lives."""
    cache = SemanticCache()
    provider = ScriptedProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, cache=cache)
    await first.run("summarise the source records")
    after_first = provider.calls

    second = SwarmRun(provider, profile="smoke", agents=4, cache=cache)
    await second.run("summarise the source records")
    second_run_calls = provider.calls - after_first

    assert second_run_calls < after_first, (
        f"second run made {second_run_calls} calls against {after_first}: "
        "the cache saved nothing"
    )


async def test_an_eval_run_refuses_a_cache():
    """Identical cached repeats do not bias a bootstrap interval, they collapse
    it -- and a zero-width interval reads as a strong result."""
    with pytest.raises(ValueError, match="eval run cannot use the semantic cache"):
        SwarmRun(ScriptedProvider(), profile="eval", cache=SemanticCache())


async def test_a_run_without_a_cache_reports_none_rather_than_zeroes():
    """A structural zero and a measured zero must not look the same."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=2)
    result = await run.run("summarise the source records")
    assert run.report(result)["cache"] is None


async def test_a_cached_run_reports_its_hit_rate():
    cache = SemanticCache()
    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=2, cache=cache)
    result = await run.run("summarise the source records")

    report = run.report(result)["cache"]
    assert report is not None
    assert report["hits"] + report["misses"] > 0
