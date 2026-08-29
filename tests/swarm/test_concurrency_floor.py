"""At least five agents are actually in the air at once.

The operating requirement for going live: not a thousand agents, but a floor
that is genuinely concurrent. A system that runs five agents one after another
satisfies every count in every report and is not a swarm.

Concurrency is measured by watching how many calls overlap inside the provider,
because it is the only place that cannot be faked by a counter elsewhere.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal
from swarmd.router.pacer import Pacer
from swarmd.router.pool import ProviderPool, ProviderSpec, _Slot
from swarmd.router.providers import LLMResponse
from swarmd.router.ration import Ration
from swarmd.swarm.run import MAX_IN_FLIGHT, SwarmRun

FLOOR = 5

# `:free` is how the ledger prices a model at zero. An unpriced model is a
# fatal error by design, so the fake has to look like a real free-tier one.
MODEL = "fake/overlap-counter:free"


# Strict enough that the batched first draft fails and every agent has to
# repair. Repairs are the only per-agent provider calls the design makes --
# generation is batched to one call per node -- so they are where agent
# concurrency is observable at all.
CRITERION = {
    "description": "structured artifact",
    "checks": [
        {"kind": "json_parses", "params": {"required_keys": ["summary", "count"]}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
    ],
}
PASSING = json.dumps(
    {"summary": "did the work carefully and thoroughly", "count": 7,
     "detail": ["a", "b"]}
)
REPAIR_MARKER = "YOUR PREVIOUS ATTEMPT FAILED"
PLAN = {
    "nodes": [
        {"name": "gather", "instruction": "collect the records", "depends_on": []},
        {"name": "verify", "instruction": "check them", "depends_on": ["gather"]},
    ]
}


class OverlapCounter:
    """A provider that records the high-water mark of concurrent calls.

    The sleep is what makes overlap observable: without it every call completes
    before the next begins and a perfectly serial pool measures as concurrent.
    """

    name = "fake"

    def __init__(self, delay: float = 0.02) -> None:
        self.models = [MODEL]
        self.delay = delay
        self.in_flight = 0
        self.peak = 0
        self.calls = 0

    async def complete_with(self, model: str, request) -> LLMResponse:
        self.in_flight += 1
        self.calls += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.delay)
            prompt = request.prompt
            if "checks" in prompt and "matching this schema" in prompt:
                text = json.dumps(CRITERION)
            elif "matching this schema" in prompt:
                text = json.dumps(PLAN)
            elif REPAIR_MARKER in prompt:
                text = PASSING
            else:
                # The batched first draft, deliberately failing the criterion
                # so every agent has to make its own repair call.
                text = "prose, which is not the JSON the criterion asks for"
            return LLMResponse(
                text=text, provider="fake", model=model,
                latency_s=self.delay, tokens_in=10, tokens_out=10,
            )
        finally:
            self.in_flight -= 1

    async def complete(self, request) -> LLMResponse:
        return await self.complete_with(MODEL, request)

    async def aclose(self) -> None:
        return None


def build_pool(tmp_path, *, day_requests: int = 100_000) -> tuple[ProviderPool, OverlapCounter]:
    tracker = BudgetTracker(
        journal=UsageJournal(str(tmp_path / "usage.jsonl")),
        budgets={
            "fake": BudgetSpec(
                provider="fake",
                kind="quota",
                limits=(Limit("day", requests=day_requests),),
                reset="rolling",
                source="test",
                checked="test",
            )
        },
    )
    provider = OverlapCounter()
    spec = ProviderSpec(
        name="fake", base_url="http://x", api_key_env="X",
        models=(MODEL,), tier="free", hint_rpm=100_000, hint_tpm=100_000_000,
    )
    pool = ProviderPool(
        [_Slot(provider, spec, credential_id="fake#0")],  # type: ignore[arg-type]
        budget=tracker,
        ration=Ration(tracker),
        pacer=Pacer(heartbeat_s=0.02, grace_s=0.02),
    )
    return pool, provider


async def test_the_pool_lets_at_least_five_calls_overlap(tmp_path):
    """The gate the ration sits in front of. A reservation taken per call could
    have serialised every agent behind one lock without changing any count."""
    pool, provider = build_pool(tmp_path)
    from swarmd.router.providers import LLMRequest

    await asyncio.gather(
        *(
            pool.complete(LLMRequest(prompt=f"p{i}", max_tokens=32))
            for i in range(16)
        )
    )
    await pool.aclose()
    assert provider.peak >= FLOOR, (
        f"only {provider.peak} call(s) ever overlapped; the ration or the pool "
        f"is serialising work that should run concurrently"
    )


async def test_a_run_keeps_five_agents_in_flight(tmp_path):
    """End to end, through the real executor rather than the pool alone."""
    pool, provider = build_pool(tmp_path)
    run = SwarmRun(pool, profile="smoke", agents=16, store=None)  # type: ignore[arg-type]
    await run.run("summarise the source records")
    await pool.aclose()
    assert provider.peak >= FLOOR


async def test_the_concurrency_bound_is_not_the_population_bound(tmp_path):
    """An operator asking for 1000 agents gets 1000 agents; MAX_IN_FLIGHT only
    decides how many are in the air. Conflating them made 500 and 1000 quote
    the same price and run the same way."""
    pool, _ = build_pool(tmp_path)
    run = SwarmRun(pool, profile="smoke", agents=1000, store=None)  # type: ignore[arg-type]
    assert run.agents == 1000
    assert MAX_IN_FLIGHT < 1000
    await pool.aclose()


async def test_a_spent_ration_pauses_rather_than_serialising(tmp_path):
    """The failure this floor protects against is subtle: a ration that refuses
    instead of parking turns a swarm into a queue of one, and every count in
    the report stays correct while it happens."""
    from swarmd.router.providers import LLMRequest

    # A day so small that the session slice is a handful of calls.
    pool, provider = build_pool(tmp_path, day_requests=40)
    pool.pacer.no_wait = True

    from swarmd.router.pacer import Paced

    results = await asyncio.gather(
        *(
            pool.complete(LLMRequest(prompt=f"p{i}", max_tokens=32))
            for i in range(30)
        ),
        return_exceptions=True,
    )
    await pool.aclose()

    refused = [r for r in results if isinstance(r, Paced)]
    assert refused, "the tiny ration admitted everything; it is not binding"
    # What must NOT happen: every call refused one at a time. The admitted ones
    # still overlapped.
    assert provider.peak >= 2


@pytest.mark.parametrize("agents", [10, 16, 32])
async def test_every_supported_population_reaches_the_floor(tmp_path, agents):
    """Against the two-node plan this provider returns, so the population has
    to be at least FLOOR x 2 for the floor to be reachable at all."""
    pool, provider = build_pool(tmp_path)
    run = SwarmRun(pool, profile="smoke", agents=agents, store=None)  # type: ignore[arg-type]
    await run.run("summarise the source records")
    await pool.aclose()
    assert provider.peak >= FLOOR


async def test_the_default_profile_reaches_the_floor_without_being_asked(tmp_path):
    """The requirement is about what a run does by default, not what it can be
    talked into. A profile whose headline count divides below the floor
    describes a population the run never keeps in flight."""
    pool, provider = build_pool(tmp_path)
    run = SwarmRun(pool, profile="smoke", store=None)  # type: ignore[arg-type]
    await run.run("summarise the source records")
    await pool.aclose()
    assert provider.peak >= FLOOR


async def test_asking_for_fewer_than_the_floor_is_honoured_and_announced(tmp_path):
    """A floor that cannot be overridden is a lie about who is in control. It
    is still worth saying out loud, because a per-node pool of one makes
    distillation structurally dead."""
    events: list[dict] = []
    pool, _ = build_pool(tmp_path)
    run = SwarmRun(
        pool, profile="smoke", agents=2, store=None,  # type: ignore[arg-type]
        on_event=events.append,
    )
    await run.run("summarise the source records")
    await pool.aclose()

    assert run.agents == 2
    below = [e for e in events if e.get("kind") == "pool_below_floor"]
    assert below, "the run silently ran below the floor"
    assert below[0]["floor"] == FLOOR
