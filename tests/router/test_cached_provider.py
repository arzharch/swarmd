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


def _cache(**kw):
    """Exact matching, which `CachedProvider` requires.

    Similarity matching on templated prompts served one plan node's answer to
    another at a measured cosine of 0.97; the module docstring on
    `router/cache.py` has the numbers.
    """
    return SemanticCache(exact_only=True, **kw)


def _account():
    return CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)


# --- the basic contract -----------------------------------------------------


async def test_a_repeated_request_does_not_reach_the_provider():
    provider = Counter()
    cached = CachedProvider(provider, _cache())

    first = await cached.complete(_request())
    second = await cached.complete(_request())

    assert provider.calls == 1
    assert second.text == first.text


async def test_a_different_prompt_is_a_miss():
    provider = Counter()
    cached = CachedProvider(provider, _cache())

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
    cached = CachedProvider(provider, _cache())

    await cached.complete(_request(temperature=0.2))
    await cached.complete(_request(temperature=0.9))

    assert provider.calls == 2


async def test_the_system_prompt_is_part_of_the_key():
    provider = Counter()
    cached = CachedProvider(provider, _cache())

    await cached.complete(_request(system="you are a planner"))
    await cached.complete(_request(system="you are a critic"))

    assert provider.calls == 2


async def test_max_tokens_is_part_of_the_key():
    provider = Counter()
    cached = CachedProvider(provider, _cache())

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
    cached = CachedProvider(provider, _cache(), account=account)

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
    cached = CachedProvider(provider, _cache(), account=account)

    await cached.complete(_request())
    await cached.complete(_request())

    rows = [r for r in account.ledger.rows() if r.kind == "cache_hit"]
    assert len(rows) == 1
    assert rows[0].would_have_cost == 0.0
    assert provider.calls == 1


async def test_a_hit_costs_nothing_against_the_ceiling():
    provider = Counter()
    account = _account()
    cached = CachedProvider(provider, _cache(), account=account)

    await cached.complete(_request())
    before = account.total_cost()
    await cached.complete(_request())

    assert account.total_cost() == before


async def test_a_hit_on_a_simulated_entry_stays_marked_simulated():
    """Otherwise a cached synthetic answer launders itself into a real report."""
    provider = Counter(provider="simulated", model="sim-1")
    account = _account()
    cached = CachedProvider(provider, _cache(), account=account)

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
    cached = CachedProvider(provider, _cache())
    account = _account()
    cached.account = account

    assert provider.account is account
    assert cached.account is account


async def test_undefined_attributes_delegate_to_the_provider():
    cached = CachedProvider(Counter(), _cache())
    assert cached.name == "counting"


# --- independence must survive the cache ------------------------------------


async def test_a_bypass_request_is_never_served_from_the_cache():
    """Calls whose whole point is independence opt out."""
    provider = Counter()
    cached = CachedProvider(provider, _cache())

    request = _request()
    request.metadata["cache"] = "bypass"
    await cached.complete(request)
    await cached.complete(request)

    assert provider.calls == 2
    assert cached.bypassed == 2


async def test_plan_proposers_stay_independent_with_a_cache_attached():
    """The live bug this covers.

    The plan proposers sent an identical prompt and differed only by sampling.
    Putting a cache in front of the provider served proposer 1's answer to
    proposers 2 and 3, so three competing DAGs became one DAG judged against
    itself -- and the selection reported a clean winner.
    """
    from swarmd.swarm.run import SwarmRun as _Run

    run = _Run(ScriptedProvider(), profile="smoke", cache=_cache())
    seen = []

    async def record(prompt, system, stage, *, cacheable=True):
        seen.append((stage, cacheable, prompt))
        return ""

    run._ask = record
    await run._propose_plan("t", 1, 0)
    await run._propose_plan("t", 1, 1)

    assert all(not cacheable for _, cacheable, _ in seen)
    assert seen[0][2] != seen[1][2], "two proposers sent an identical prompt"


async def test_criterion_proposers_stay_independent_too():
    run = SwarmRun(ScriptedProvider(), profile="smoke", cache=_cache())
    seen = []

    async def record(prompt, system, stage, *, cacheable=True):
        seen.append((cacheable, prompt))
        return ""

    run._ask = record
    await run._propose_criterion("t", 1, 0)
    await run._propose_criterion("t", 1, 1)

    assert all(not cacheable for cacheable, _ in seen)
    assert seen[0][1] != seen[1][1]


# --- inside a run -----------------------------------------------------------


async def test_a_second_identical_run_reuses_the_first_runs_generations():
    """The saving is across runs, which is where the repetition lives.

    Asserted on cache hits rather than on total calls: proposals deliberately
    BYPASS the cache to stay independent, so the totals include calls that can
    never be saved and comparing them measures the wrong thing.
    """
    cache = _cache()
    provider = ScriptedProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, cache=cache)
    await first.run("summarise the source records")
    hits_after_first = first.provider.hits

    second = SwarmRun(provider, profile="smoke", agents=4, cache=cache)
    await second.run("summarise the source records")

    assert second.provider.hits > hits_after_first, (
        "the second run generated everything again"
    )
    assert second.provider.misses < second.provider.hits


async def test_different_plan_nodes_never_share_a_cached_answer():
    """The live bug that made exact matching non-negotiable.

    Worker prompts share a long template and differ in one step name, which put
    genuinely different nodes at cosine 0.97 -- above the 0.95 similarity
    threshold. Every node received the same answer, and the run reported a high
    hit rate and a low cost while being wrong.
    """
    from swarmd.router.cache import cosine, hash_embedder
    from swarmd.swarm.batch import batch_prompt

    # The REAL prompt shape, batching instruction included. Using a shortened
    # stand-in would understate the effect and prove nothing: similarity here
    # rises with template length, so a shorter envelope sits safely below the
    # threshold while the prompt production actually sends sits above it.
    template = (
        "TASK: extract and verify the numeric claims in a short report\n"
        "STEP: {step}\n"
        "REQUIRED: {req}"
    )
    a = batch_prompt(
        template.format(step="extract", req="pull every claim into out.json"), 8
    )
    b = batch_prompt(
        template.format(step="verify", req="recompute each claim and record it"), 8
    )

    # The hazard, measured: two genuinely different nodes match anyway.
    assert cosine(hash_embedder(a), hash_embedder(b)) > 0.95

    cache = _cache()
    provider = Counter()
    cached = CachedProvider(provider, cache)
    await cached.complete(_request(a))
    await cached.complete(_request(b))

    assert provider.calls == 2, "one node was served another node's answer"


async def test_a_similarity_cache_is_refused_outright():
    """Refused rather than warned about: the symptom is a fast, cheap run whose
    nodes all produced the same artifact, which looks like success."""
    with pytest.raises(ValueError, match="exact_only=True"):
        CachedProvider(Counter(), SemanticCache())


async def test_an_eval_run_refuses_a_cache():
    """Identical cached repeats do not bias a bootstrap interval, they collapse
    it -- and a zero-width interval reads as a strong result."""
    with pytest.raises(ValueError, match="eval run cannot use the semantic cache"):
        SwarmRun(ScriptedProvider(), profile="eval", cache=_cache())


async def test_a_run_without_a_cache_reports_none_rather_than_zeroes():
    """A structural zero and a measured zero must not look the same."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=2)
    result = await run.run("summarise the source records")
    assert run.report(result)["cache"] is None


async def test_a_cached_run_reports_its_hit_rate():
    cache = _cache()
    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=2, cache=cache)
    result = await run.run("summarise the source records")

    report = run.report(result)["cache"]
    assert report is not None
    assert report["hits"] + report["misses"] > 0
