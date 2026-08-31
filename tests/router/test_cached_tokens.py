"""Cached prompt tokens: measured where reported, never inferred.

Reordering a prompt so its stable part comes first is only worth anything if
a provider's prefix cache actually serves it, and the only honest evidence of
that is the provider's own usage block. Nothing here estimates: swarmd knows
what it sent, and what it sent says nothing about what the far side kept.

Three separate things have to stay true, and each is easy to break in a way
that looks like an improvement:

  PARSING     providers disagree about the field's name, its nesting, and
              whether it exists at all. Absent must read as "not measured",
              not as "nothing was cached".
  BILLING     a cached token is cheaper only for a model whose price row says
              so. Assuming a discount under-bills, and an under-billed run is
              one whose cost ceiling stops firing.
  QUOTA       a cached token still counts against a free tier's daily well in
              full. Crediting the ration for it turns a latency win into an
              afternoon of 429s.
"""

from __future__ import annotations

import pytest

from swarmd.ledger import PRICES, CostAccount, InMemoryLedger, ModelPrice
from swarmd.router.budget import BudgetTracker, UsageJournal
from swarmd.router.pool import ProviderPool, ProviderSpec, _Slot
from swarmd.router.providers import LLMRequest, LLMResponse, parse_cached_tokens
from swarmd.router.ration import Ration

PAID_MODEL = "z-ai/glm-5.3-flash"


# --- parsing ---------------------------------------------------------------
#
# The payload shapes below are the ones this pool's providers actually send.


def test_the_openai_shape_is_read():
    """Groq, OpenRouter, and Google's OpenAI-compat shim all nest it here."""
    usage = {
        "prompt_tokens": 1500,
        "completion_tokens": 60,
        "prompt_tokens_details": {"cached_tokens": 1280},
    }
    assert parse_cached_tokens(usage) == (1280, True)


def test_the_flat_anthropic_style_key_is_read():
    """Some gateways proxy an Anthropic-shaped usage block through the same
    endpoint. Reading it costs one dict lookup and avoids a silent zero."""
    assert parse_cached_tokens(
        {"prompt_tokens": 900, "cache_read_input_tokens": 700}
    ) == (700, True)


def test_a_provider_that_says_nothing_is_recorded_as_saying_nothing():
    """0 and "not reported" are different facts and must stay different.

    Collapsing them is how a working prefix cache reads as a no-op, and how a
    provider that simply omits the field gets reported as evidence that the
    reordering failed. The boolean is the whole point of the pair.
    """
    assert parse_cached_tokens({"prompt_tokens": 900}) == (0, False)


def test_a_provider_that_reports_zero_is_recorded_as_reporting_zero():
    """The other half of the same distinction: a genuine cold cache."""
    assert parse_cached_tokens(
        {"prompt_tokens": 900, "prompt_tokens_details": {"cached_tokens": 0}}
    ) == (0, True)


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens_details": None},          # observed: an explicit null
        {"prompt_tokens_details": []},            # a list where a dict was due
        {"prompt_tokens_details": {"cached_tokens": "1280"}},   # a string
        {"prompt_tokens_details": {"cached_tokens": -5}},       # nonsense
        {"prompt_tokens_details": {"cached_tokens": True}},     # a bool
        None,                                     # no usage block at all
        "usage",                                  # not even a mapping
    ],
)
def test_a_malformed_usage_block_reports_nothing_rather_than_raising(usage):
    """This runs inside a SUCCESSFUL call, on the hot path.

    A parser that raised here would turn a bookkeeping gap into a failed
    request and a provider fail-over -- spending the run's quota to punish a
    provider for a field swarmd only wanted for a report.
    """
    assert parse_cached_tokens(usage) == (0, False)


# --- billing ---------------------------------------------------------------


def test_no_published_cached_rate_means_no_discount():
    """Silence is not a discount.

    Under-billing is exactly how a `ceiling_usd` stops triggering, and a run
    that quietly outspends its ceiling is the failure the cost controls exist
    to prevent. A model with no cached rate bills every prompt token at the
    full input rate no matter what the provider reports as cached.
    """
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0)
    assert price.cost(1_000_000, 0, 900_000) == pytest.approx(1.0)
    assert price.cost(1_000_000, 0, 900_000) == price.cost(1_000_000, 0)


def test_a_published_cached_rate_discounts_only_the_reported_portion():
    """The discount applies to the cached tokens and to nothing else."""
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.1)
    # 900k cached at 0.1, 100k fresh at 1.0, no output.
    assert price.cost(1_000_000, 0, 900_000) == pytest.approx(0.19)
    # Nothing cached: unchanged from the plain rate.
    assert price.cost(1_000_000, 0, 0) == pytest.approx(1.0)


def test_a_provider_over_reporting_cached_tokens_cannot_bill_below_zero():
    """A hostile or buggy usage block must not produce a negative charge.

    A cost that can be pushed down by a number the far side controls is a
    ceiling the far side controls.
    """
    price = ModelPrice(input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.0)
    assert price.cost(1_000, 0, 10_000) == pytest.approx(0.0)
    assert price.cost(1_000, 0, 10_000) >= 0.0


def test_the_ledger_row_carries_what_the_provider_reported():
    """Every figure swarmd reports is an aggregate over rows (ADR-007).

    Cached tokens are no exception: they are stored per call so "how much of
    this run's prompt was cached" stays a query rather than a counter someone
    increments and forgets to reset.
    """
    account = CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)
    account.charge_call(
        provider="groq", model="qwen/qwen3.8-27b",
        tokens_in=1000, tokens_out=50,
        cached_tokens=800, cached_tokens_reported=True, stage="worker",
    )
    row = account.ledger.rows()[0]
    assert row.cached_tokens == 800
    assert row.detail["cached_tokens_reported"] is True


def test_the_report_distinguishes_unmeasured_from_uncached():
    """A dashboard reading `cache_hit_tokens == 0` deserves to know which.

    On a provider that omits the field the reordering may be working
    perfectly with no way to see it. Reporting a bare 0 with no qualifier
    would make an unmeasurable win look like a failed one -- and would
    eventually be quoted as evidence against the change.
    """
    account = CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)
    account.charge_call(
        provider="groq", model="qwen/qwen3.8-27b",
        tokens_in=1000, tokens_out=50,
    )
    report = account.report()
    assert report["cache_hit_tokens"] == 0
    assert report["prefix_cache"]["reported"] is False
    assert report["prefix_cache"]["reported_by"] == []
    assert "not measured" in report["prefix_cache"]["note"]

    account.charge_call(
        provider="groq", model="qwen/qwen3.8-27b",
        tokens_in=1000, tokens_out=50,
        cached_tokens=750, cached_tokens_reported=True,
    )
    report = account.report()
    assert report["cache_hit_tokens"] == 750
    assert report["prefix_cache"]["prompt_tokens"] == 2000
    assert report["prefix_cache"]["ratio"] == pytest.approx(0.375)
    assert report["prefix_cache"]["reported"] is True
    assert report["prefix_cache"]["reported_by"] == ["groq"]
    assert report["prefix_cache"]["note"] == ""


def test_the_ledger_still_sums_its_rows_when_a_cached_rate_exists(monkeypatch):
    """The ceiling is enforced on the discounted figure, not on a guess."""
    monkeypatch.setitem(
        PRICES, PAID_MODEL,
        ModelPrice(input_per_m=1.0, output_per_m=2.0, cached_input_per_m=0.1),
    )
    account = CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)
    charged = account.charge_call(
        provider="openrouter-paid", model=PAID_MODEL,
        tokens_in=1_000_00, tokens_out=0, cached_tokens=90_000,
        cached_tokens_reported=True,
    )
    assert charged == pytest.approx((10_000 * 1.0 + 90_000 * 0.1) / 1_000_000)
    assert account.total_cost() == pytest.approx(charged)


# --- quota -----------------------------------------------------------------


class CachingProvider:
    """Returns a response that claims most of its prompt was cached."""

    def __init__(self, name: str, models: list[str]) -> None:
        self.name = name
        self.models = models

    async def complete_with(self, model: str, request: LLMRequest) -> LLMResponse:
        return LLMResponse(
            text="ok", provider=self.name, model=model, latency_s=0.01,
            tokens_in=1000, tokens_out=50,
            cached_tokens=900, cached_tokens_reported=True,
        )

    async def aclose(self) -> None:
        return None


class RecordingRation(Ration):
    """A real ration that remembers what it was told to settle."""

    def __init__(self, tracker: BudgetTracker) -> None:
        super().__init__(tracker)
        self.settled: list[tuple[int, str]] = []

    async def settle(self, grant, *, tokens: int, outcome: str = "ok") -> None:
        self.settled.append((tokens, outcome))
        await super().settle(grant, tokens=tokens, outcome=outcome)


def _pool(tmp_path):
    spec = ProviderSpec(
        name="groq", base_url="http://x", api_key_env="X", models=("m",)
    )
    tracker = BudgetTracker(journal=UsageJournal(str(tmp_path / "usage.jsonl")))
    ration = RecordingRation(tracker)
    slot = _Slot(CachingProvider("groq", ["m"]), spec)  # type: ignore[arg-type]
    account = CostAccount(InMemoryLedger("run-1"), "run-1", ceiling_usd=1.0)
    pool = ProviderPool(
        [slot], account=account, budget=tracker, ration=ration, max_wait_s=0.0
    )
    return pool, ration, tracker, account


async def test_cached_tokens_are_never_credited_back_to_the_ration(tmp_path):
    """A free tier's daily well counts a cached prompt token in full.

    The discount providers offer on cached tokens is on PRICE. Treating it as
    quota headroom would let a run believe it has capacity the account does
    not have, and the symptom -- a wave of 429s hours earlier than forecast --
    would look like a provider problem rather than an accounting one.

    So the settled figure is the whole prompt plus the whole completion, and
    it must not move when the provider reports 900 of those 1000 prompt
    tokens as cached.
    """
    pool, ration, _tracker, _account = _pool(tmp_path)
    try:
        resp = await pool.complete(LLMRequest(prompt="hello", max_tokens=64))
    finally:
        await pool.aclose()

    assert resp.cached_tokens == 900
    assert ration.settled == [(1050, "ok")], (
        "the ration was credited for cached tokens the provider still counts"
    )


async def test_the_usage_journal_carries_cached_tokens_beside_the_total(tmp_path):
    """Observational, and stored where the month's history lives.

    The journal is what the ration reads back across restarts, so the cached
    figure has to sit BESIDE `tokens` rather than inside it: a future reader
    that subtracted it would re-create the quota bug one layer down.
    """
    pool, _ration, tracker, _account = _pool(tmp_path)
    try:
        await pool.complete(LLMRequest(prompt="hello", max_tokens=64))
    finally:
        await pool.aclose()

    # Selected by what the row REPORTS, not by what it charges. A rationed
    # call's observation row now carries zero cost -- the ration's own
    # reserve/settle pair is the charge, and a second charging row billed the
    # day twice -- so filtering on `r.requests` would drop the very row this
    # test is about while the property it asserts still holds.
    rows = tracker.journal.rows_since(0.0)
    observed = [r for r in rows if r.cached_tokens]
    assert observed, "nothing was journalled"
    assert any(r.cached_tokens == 900 for r in observed)
    # Beside the total, never subtracted from it: the ration reads this back.
    charged = [r for r in rows if r.tokens > 0]
    assert charged and all(r.tokens >= r.cached_tokens for r in charged)


async def test_a_cached_call_lands_in_the_ledger_with_its_cached_count(tmp_path):
    """End to end: what the provider said reaches the run's cost report."""
    pool, _ration, _tracker, account = _pool(tmp_path)
    try:
        await pool.complete(LLMRequest(prompt="hello", max_tokens=64))
    finally:
        await pool.aclose()

    report = account.report()
    assert report["cache_hit_tokens"] == 900
    assert report["prefix_cache"]["reported"] is True
    assert report["prefix_cache"]["ratio"] == pytest.approx(0.9)
    # Free tier: the saving is in latency and throughput, not dollars, and the
    # ledger says so rather than inventing a discount.
    assert report["total_usd"] == 0.0


# --- metrics ---------------------------------------------------------------
#
# Two counters, no stored ratio. The ratio is derived at read time so it cannot
# drift away from the tokens it claims to summarise, and both numbers come from
# the provider's usage block rather than from anything swarmd believes about
# its own prompts.


def _counter(name: str, **labels: str) -> float:
    from swarmd.observability import metrics

    return metrics.sample(name, **labels) or 0.0


def test_a_reported_cache_hit_moves_both_counters():
    """`cached` is a SUBSET of `prompt`, so both series must advance together.

    A dashboard divides one by the other. If the cached counter moved without
    the prompt counter -- or the prompt counter were net of cached tokens --
    the ratio would exceed 1.0 or under-report, and the first person to notice
    would be someone quoting a fabricated number in a capacity argument.
    """
    from swarmd.observability import metrics

    before_prompt = _counter(
        "swarmd_prompt_tokens_total", provider="metric-probe", stage="worker"
    )
    before_cached = _counter(
        "swarmd_prompt_tokens_cached_total", provider="metric-probe", stage="worker"
    )
    metrics.record_prompt_tokens(
        provider="metric-probe", stage="worker",
        prompt_tokens=1000, cached_tokens=800,
    )
    assert _counter(
        "swarmd_prompt_tokens_total", provider="metric-probe", stage="worker"
    ) == before_prompt + 1000
    assert _counter(
        "swarmd_prompt_tokens_cached_total", provider="metric-probe", stage="worker"
    ) == before_cached + 800


def test_a_silent_provider_advances_only_the_prompt_counter():
    """A flat cached series must read as "unmeasured here", not as "cold".

    Inventing a zero sample for a provider that reported nothing would create
    a series that looks measured and says the reordering failed. Leaving it
    absent is what makes the difference visible in the query.
    """
    from swarmd.observability import metrics

    before = _counter(
        "swarmd_prompt_tokens_cached_total", provider="silent-probe", stage="worker"
    )
    metrics.record_prompt_tokens(
        provider="silent-probe", stage="worker", prompt_tokens=1000, cached_tokens=0
    )
    assert _counter(
        "swarmd_prompt_tokens_total", provider="silent-probe", stage="worker"
    ) == 1000
    assert _counter(
        "swarmd_prompt_tokens_cached_total", provider="silent-probe", stage="worker"
    ) == before


# --- the capability label --------------------------------------------------


def test_the_registry_labels_which_providers_can_cache_a_prefix():
    """A zero has two explanations, and the label is what separates them.

    Groq, OpenRouter and Mistral cache a byte-identical prefix on their own,
    so a zero from one of them means the shared prefix is not being hit and
    the prompt layout is the thing to look at. Google's caching is `explicit`
    -- a `cachedContents` handle with no representation in the OpenAI-compat
    payload this adapter sends -- so a zero from it is expected and says
    nothing about the layout. Reading them the same way would send someone
    debugging a prompt that is already correct.
    """
    from swarmd.router.pool import REGISTRY

    assert REGISTRY["groq"].prefix_cache == "auto"
    assert REGISTRY["openrouter"].prefix_cache == "auto"
    assert REGISTRY["google-aistudio"].prefix_cache == "explicit"
    assert {s.prefix_cache for s in REGISTRY.values()} <= {"auto", "explicit", "none"}


async def test_probe_reports_the_label_beside_the_liveness(tmp_path):
    """`swarmd providers` is where an operator looks first, so it says so there.

    The label cannot be probed -- no endpoint answers "do you cache prefixes"
    -- so it travels with the spec. Carrying it on the probe row is what stops
    it being a constant nothing reads.
    """
    pool, _ration, _tracker, _account = _pool(tmp_path)
    try:
        rows = await pool.probe()
    finally:
        await pool.aclose()

    assert rows and all("prefix_cache" in row for row in rows)
    assert rows[0]["prefix_cache"] == "auto"
