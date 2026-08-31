"""Provider-pool tests.

No network. The pool's contract is about ROUTING decisions -- which provider,
in what order, backed off for how long -- so the transport is faked and the
decisions are asserted directly.
"""

from __future__ import annotations

import pytest

from swarmd.ledger import CeilingExceeded, CostAccount, InMemoryLedger
from swarmd.router.pool import (
    REGISTRY,
    LimitState,
    NoCapacity,
    ProviderPool,
    ProviderSpec,
    RateLimited,
    _Slot,
)
from swarmd.router.providers import LLMRequest, LLMResponse, ProviderError

PAID_MODEL = "z-ai/glm-5.3-flash"


class FakeProvider:
    """Scripted provider: each call pops the next scripted outcome."""

    def __init__(self, name: str, models: list[str], script: list[object] | None = None):
        self.name = name
        self.models = models
        self.script = list(script or [])
        self.calls: list[tuple[str, str]] = []

    async def complete_with(self, model: str, request: LLMRequest) -> LLMResponse:
        self.calls.append((model, request.prompt))
        outcome = self.script.pop(0) if self.script else None
        if isinstance(outcome, Exception):
            raise outcome
        return LLMResponse(
            text=f"{self.name}:{model}",
            provider=self.name,
            model=model,
            latency_s=0.01,
            tokens_in=10,
            tokens_out=5,
        )

    async def aclose(self) -> None:
        return None


def _slot(name, models, tier="free", script=None):
    spec = ProviderSpec(
        name=name, base_url="http://x", api_key_env="X", models=tuple(models), tier=tier
    )
    return _Slot(FakeProvider(name, models, script), spec)  # type: ignore[arg-type]


def _req(**kw):
    kw.setdefault("max_tokens", 64)
    return LLMRequest(prompt="hello", **kw)


# --- ordering --------------------------------------------------------------


async def test_free_tier_is_preferred_over_paid_even_when_paid_is_healthier():
    """Tier dominates health: free-but-degraded still beats healthy-and-paid."""
    free = _slot("groq", ["m-free"])
    paid = _slot("openrouter-paid", [PAID_MODEL], tier="paid")
    free.state.record_error()
    free.state.record_error()

    pool = ProviderPool([paid, free], allow_paid=True)
    resp = await pool.complete(_req())
    assert resp.provider == "groq"


async def test_within_a_tier_the_healthier_provider_goes_first():
    a = _slot("groq", ["m"])
    b = _slot("cerebras", ["m"])
    for _ in range(3):
        a.state.record_error()
    b.state.record_success()

    pool = ProviderPool([a, b])
    resp = await pool.complete(_req())
    assert resp.provider == "cerebras"


async def test_exhausted_free_capacity_stops_the_run_rather_than_spending():
    """Without an explicit paid opt-in there is no paid slot to fall through to."""
    free = _slot("groq", ["m"], script=[RateLimited("groq", 999.0)])
    pool = ProviderPool([free], allow_paid=False, max_wait_s=0.0)
    with pytest.raises(NoCapacity):
        await pool.complete(_req())


async def test_paid_overflow_is_used_once_free_capacity_is_gone():
    free = _slot("groq", ["m"], script=[RateLimited("groq", 999.0)])
    paid = _slot("openrouter-paid", [PAID_MODEL], tier="paid")
    pool = ProviderPool([free, paid], allow_paid=True, max_wait_s=0.0)

    resp = await pool.complete(_req())
    assert resp.provider == "openrouter-paid"


# --- rate limits -----------------------------------------------------------


async def test_a_429_routes_to_the_next_provider():
    throttled = _slot("groq", ["m"], script=[RateLimited("groq", 60.0)])
    healthy = _slot("cerebras", ["m"])
    pool = ProviderPool([throttled, healthy])

    resp = await pool.complete(_req())
    assert resp.provider == "cerebras"
    assert throttled.state.total_429s == 1
    assert not throttled.state.available()


async def test_a_429_backs_off_the_whole_provider_not_just_one_model():
    """A quota is per-account, so trying the next model only deepens the hole."""
    slot = _slot("groq", ["m1", "m2"], script=[RateLimited("groq", 60.0)])
    healthy = _slot("cerebras", ["m"])
    pool = ProviderPool([slot, healthy])

    await pool.complete(_req())
    assert [c[0] for c in slot.provider.calls] == ["m1"]  # m2 never attempted


async def test_a_transport_error_tries_the_next_model_on_the_same_provider():
    """An error is not a quota signal -- the provider may still be usable."""
    slot = _slot("groq", ["m1", "m2"], script=[ProviderError("m1 broke")])
    pool = ProviderPool([slot])

    resp = await pool.complete(_req())
    assert resp.model == "m2"
    assert slot.state.errors == 1


async def test_provider_supplied_retry_after_wins_over_our_backoff():
    state = LimitState(backoff_base_s=2.0)
    state.record_429(retry_after_s=7.5)
    assert 7.0 < state.wait_s() <= 7.5


async def test_backoff_is_exponential_when_the_provider_says_nothing():
    state = LimitState(backoff_base_s=2.0, backoff_cap_s=100.0)
    state.record_429(None)
    first = state.wait_s()
    state.record_429(None)
    second = state.wait_s()
    assert 1.5 < first <= 2.0
    assert 3.5 < second <= 4.0


async def test_backoff_is_capped():
    state = LimitState(backoff_base_s=2.0, backoff_cap_s=10.0)
    for _ in range(10):
        state.record_429(None)
    assert state.wait_s() <= 10.0


def test_the_shortest_observed_rejection_interval_is_recorded(monkeypatch):
    """This is the discovered limit -- more trustworthy than any published one."""
    ticks = iter([100.0, 100.5, 130.0, 130.2])
    monkeypatch.setattr("swarmd.router.pool.time.monotonic", lambda: next(ticks))
    state = LimitState()
    state.record_429(None)  # t=100.0
    state.record_429(None)  # t=100.5 -> interval 0.5
    state.record_429(None)  # t=130.0 -> interval 29.5, not the minimum
    assert state.observed_min_interval_s == pytest.approx(0.5)


async def test_success_decays_the_streak_rather_than_resetting_it():
    """Resetting to zero makes the pool oscillate between hammering and backoff."""
    state = LimitState(recovery_halflife_s=0.0)
    state.record_429(None)
    state.record_429(None)
    assert state.consecutive_429s == 2
    state.record_success()
    assert state.consecutive_429s == 1


async def test_rate_limits_and_errors_score_differently():
    """A 429 means 'working, asked too fast'; an error means 'broken'."""
    throttled = LimitState()
    broken = LimitState()
    for _ in range(4):
        throttled.record_429(None)
        broken.record_error()
    assert throttled.score() < broken.score()


async def test_exhausted_pool_raises_no_capacity_rather_than_hanging():
    slot = _slot("groq", ["m"], script=[RateLimited("groq", 3600.0)])
    pool = ProviderPool([slot], max_wait_s=0.05)
    with pytest.raises(NoCapacity):
        await pool.complete(_req())


async def test_pool_waits_for_a_provider_to_come_back_within_the_deadline():
    slot = _slot("groq", ["m"], script=[RateLimited("groq", 0.05)])
    pool = ProviderPool([slot], max_wait_s=5.0)
    resp = await pool.complete(_req())
    assert resp.provider == "groq"


# --- ledger integration ----------------------------------------------------


async def test_successful_calls_are_charged_to_the_ledger():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    pool = ProviderPool([_slot("groq", ["m"])], account=acct)

    await pool.complete(_req(metadata={"agent_id": "a-1", "stage": "plan"}))
    rows = acct.ledger.rows()
    assert len(rows) == 1
    assert rows[0].kind == "llm_call"
    assert rows[0].agent_id == "a-1"
    assert rows[0].stage == "plan"
    assert rows[0].provider == "groq"


async def test_a_call_that_cannot_fit_the_ceiling_is_refused_before_spending():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=0.0000001)
    slot = _slot("openrouter-paid", [PAID_MODEL], tier="paid")
    pool = ProviderPool([slot], account=acct, allow_paid=True)

    with pytest.raises(CeilingExceeded):
        await pool.complete(_req(max_tokens=1_000_000))
    assert slot.provider.calls == []  # never issued


async def test_free_calls_are_never_blocked_by_the_ceiling():
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=0.0)
    pool = ProviderPool([_slot("groq", ["m"])], account=acct)
    resp = await pool.complete(_req(max_tokens=1_000_000))
    assert resp.provider == "groq"


# --- ration release on abandonment ------------------------------------------
#
# The ration admits a call (reserving its slice of the day) before the minute
# bucket or the cost ceiling get a say. When one of THOSE refuses, the call was
# never sent, and the reservation must be handed back immediately -- not left
# for the reaper, which only writes off reservations that are minutes old and
# would otherwise let a run's minute-bucket waits or ceiling refusals quietly
# spend the day for calls that never happened.


def _day_rationed(tmp_path, provider: str, *, requests: int = 1000):
    """A BudgetTracker + Ration pair with a real daily allowance for `provider`,
    so `ration.reserve()` actually admits and writes a reservation row -- the
    thing that must be given back, not the no-op grant an unrationed provider
    gets."""
    from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal
    from swarmd.router.ration import Ration

    track = BudgetTracker(
        journal=UsageJournal(str(tmp_path / "usage.jsonl")),
        budgets={
            provider: BudgetSpec(
                provider=provider,
                kind="quota",
                limits=(Limit("day", requests=requests),),
                reset="rolling",
                source="test",
                checked="test",
            )
        },
    )
    return track, Ration(track)


async def test_a_minute_bucket_refusal_releases_the_ration_grant(tmp_path):
    """The ration admits before the minute bucket is even asked, so a bucket
    refusal must give the slice back. Without release(), every busy minute
    would burn a slice of the day's allowance for a request that was never
    sent -- charged against the day forever."""
    import time

    from swarmd.router.quota import InProcessQuota

    track, ration = _day_rationed(tmp_path, "groq")
    quota = InProcessQuota()

    throttled = _slot("groq", ["m"])
    throttled.credential_id = "groq#0"
    healthy = _slot("cerebras", ["m"])
    healthy.credential_id = "cerebras#0"

    pool = ProviderPool([throttled, healthy], quota=quota, budget=track, ration=ration)
    await pool._ensure_quota_configured()
    await quota.configure("groq#0", rate_per_min=1, burst=1)
    await quota.acquire("groq#0")  # drain groq's minute bucket

    resp = await pool.complete(_req())
    assert resp.provider == "cerebras"
    assert throttled.provider.calls == []  # groq was never actually sent to

    rows = track.journal.rows_for(provider="groq", credential="groq#0", since=0.0)
    assert sum(r.requests for r in rows) == 0
    assert sum(r.tokens for r in rows) == 0
    # The release must be immediate, not something the reaper cleans up later:
    # reaping a long time past even the reap threshold finds nothing stranded,
    # because the reservation was already settled the moment the bucket
    # refused it.
    assert ration.reap(now=time.time() + 100_000) == 0


async def test_a_ceiling_refusal_releases_the_ration_grant(tmp_path):
    """`account.precheck` can raise straight out of `complete()` with the
    ration grant still outstanding. The slice must come back even though the
    call site never reaches a `settle()`."""
    import time

    track, ration = _day_rationed(tmp_path, "openrouter-paid")
    acct = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=0.0000001)
    slot = _slot("openrouter-paid", [PAID_MODEL], tier="paid")
    slot.credential_id = "openrouter-paid#0"
    pool = ProviderPool(
        [slot], account=acct, allow_paid=True, budget=track, ration=ration
    )

    with pytest.raises(CeilingExceeded):
        await pool.complete(_req(max_tokens=1_000_000))
    assert slot.provider.calls == []  # never issued

    rows = track.journal.rows_for(
        provider="openrouter-paid", credential="openrouter-paid#0", since=0.0
    )
    assert sum(r.requests for r in rows) == 0
    assert sum(r.tokens for r in rows) == 0
    # Immediate, not reaper-recovered.
    assert ration.reap(now=time.time() + 100_000) == 0


# --- construction ----------------------------------------------------------


def test_from_env_skips_providers_whose_keys_are_absent(monkeypatch):
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "k")

    pool = ProviderPool.from_env()
    assert [s["provider"] for s in pool.status()] == ["groq"]


def test_from_env_refuses_to_build_an_empty_pool(monkeypatch):
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    with pytest.raises(RuntimeError, match="no usable providers"):
        ProviderPool.from_env()


def test_data_training_tier_requires_an_explicit_opt_in(monkeypatch):
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    monkeypatch.setenv("MISTRAL_API_KEY", "k")

    with pytest.raises(RuntimeError, match="no usable providers"):
        ProviderPool.from_env()

    pool = ProviderPool.from_env(allow_data_training=True)
    assert [s["provider"] for s in pool.status()] == ["mistral-free"]


def test_paid_tier_requires_an_explicit_opt_in(monkeypatch):
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")

    free_only = ProviderPool.from_env()
    assert [s["provider"] for s in free_only.status()] == ["openrouter"]

    with_paid = ProviderPool.from_env(allow_paid=True)
    assert {s["provider"] for s in with_paid.status()} == {
        "openrouter",
        "openrouter-paid",
    }


# --- introspection ---------------------------------------------------------


async def test_status_reports_availability_and_discovered_limits():
    slot = _slot("groq", ["m"], script=[RateLimited("groq", 30.0)])
    healthy = _slot("cerebras", ["m"])
    pool = ProviderPool([slot, healthy])
    await pool.complete(_req())

    by_name = {s["provider"]: s for s in pool.status()}
    assert by_name["groq"]["rate_limits"] == 1
    assert by_name["groq"]["available"] is False
    assert by_name["groq"]["wait_s"] > 0
    assert by_name["cerebras"]["successes"] == 1


async def test_probe_reports_one_row_per_provider_including_failures():
    ok = _slot("groq", ["m"])
    bad = _slot("cerebras", ["m"], script=[ProviderError("down")])
    limited = _slot("openrouter", ["m"], script=[RateLimited("openrouter", 12.0)])
    pool = ProviderPool([ok, bad, limited])

    rows = {r["provider"]: r for r in await pool.probe()}
    assert rows["groq"]["ok"] is True
    assert rows["cerebras"]["ok"] is False
    assert rows["openrouter"]["reason"] == "rate_limited"
    assert rows["openrouter"]["retry_after_s"] == 12.0


def test_registry_models_are_priced_or_explicitly_free():
    """A pool model with no price silently disables the ceiling (ADR-007)."""
    from swarmd.ledger import price_for

    for spec in REGISTRY.values():
        for model in spec.models:
            price_for(spec.name, model)  # raises UnpricedModel if unknown


# --- credentials and quota -------------------------------------------------


def test_multiple_credentials_become_multiple_slots(monkeypatch):
    """Two keys are two accounts, so genuinely two quotas."""
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
        monkeypatch.delenv(spec.api_key_env + "S", raising=False)
    monkeypatch.setenv("GROQ_API_KEYS", "key-a, key-b ,key-c")

    pool = ProviderPool.from_env()
    rows = pool.status()
    assert len(rows) == 3
    assert {r["credential"] for r in rows} == {"groq#0", "groq#1", "groq#2"}


def test_singular_and_plural_key_vars_combine_without_duplicating(monkeypatch):
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
        monkeypatch.delenv(spec.api_key_env + "S", raising=False)
    monkeypatch.setenv("GROQ_API_KEYS", "key-a,key-b")
    monkeypatch.setenv("GROQ_API_KEY", "key-a")  # already in the plural list

    pool = ProviderPool.from_env()
    assert len(pool.status()) == 2


def test_credential_ids_never_contain_key_material(monkeypatch):
    """Ids reach logs, metrics and the dashboard; keys must not."""
    for spec in REGISTRY.values():
        monkeypatch.delenv(spec.api_key_env, raising=False)
        monkeypatch.delenv(spec.api_key_env + "S", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", "sk-super-secret-value")

    pool = ProviderPool.from_env()
    assert "sk-super-secret-value" not in str(pool.status())


async def test_quota_exhaustion_routes_to_another_provider():
    """A pod that has spent its share must not send anyway."""
    from swarmd.router.quota import InProcessQuota

    quota = InProcessQuota()
    a = _slot("groq", ["m"])
    a.credential_id = "groq#0"
    b = _slot("cerebras", ["m"])
    b.credential_id = "cerebras#0"
    pool = ProviderPool([a, b], quota=quota)
    await pool._ensure_quota_configured()
    # Drain groq's bucket entirely.
    await quota.configure("groq#0", rate_per_min=1, burst=1)
    await quota.acquire("groq#0")

    resp = await pool.complete(_req())
    assert resp.provider == "cerebras"
    assert a.provider.calls == []


async def test_a_429_tightens_the_quota_bucket_not_only_the_backoff():
    """Backoff expires; the corrected rate must outlive it."""
    from swarmd.router.quota import InProcessQuota

    quota = InProcessQuota()
    throttled = _slot("groq", ["m"], script=[RateLimited("groq", 0.01)])
    throttled.credential_id = "groq#0"
    healthy = _slot("cerebras", ["m"])
    healthy.credential_id = "cerebras#0"
    pool = ProviderPool([throttled, healthy], quota=quota)

    await pool.complete(_req())

    snap = await quota.snapshot()
    assert snap["groq#0"]["rate_per_min"] <= REGISTRY["groq"].hint_rpm * 0.5
    assert snap["groq#0"]["burst"] == 1


async def test_quota_is_keyed_per_credential_not_per_provider():
    """Otherwise three Groq keys would be throttled as if they were one."""
    from swarmd.router.quota import InProcessQuota

    quota = InProcessQuota()
    a = _slot("groq", ["m"])
    a.credential_id = "groq#0"
    b = _slot("groq", ["m"])
    b.credential_id = "groq#1"
    pool = ProviderPool([a, b], quota=quota)
    await pool._ensure_quota_configured()

    await quota.configure("groq#0", rate_per_min=1, burst=1)
    await quota.acquire("groq#0")  # drain only the first credential

    resp = await pool.complete(_req())
    assert resp.provider == "groq"
    assert b.provider.calls  # the second credential served it


async def test_failing_over_to_a_second_model_charges_the_ration_for_both(tmp_path):
    """Two calls on the wire must cost two attempts, not one grant settled twice.

    The grant used to be taken once per SLOT and settled once per ATTEMPT, so a
    model that errored and a model that then succeeded wrote two settle rows
    under one rid: the error row returned the request and the whole estimate,
    and the success row wrote only `actual - estimate` on top of that. The
    served call therefore landed in the journal as ZERO requests and, whenever
    the response came in under the estimate, a NEGATIVE token count -- the
    ration crediting the day for work the account had actually done.
    """
    track, ration = _day_rationed(tmp_path, "groq")
    slot = _slot("groq", ["m-a", "m-b"], script=[ProviderError("m-a is down")])
    slot.credential_id = "groq#0"

    pool = ProviderPool([slot], budget=track, ration=ration)
    resp = await pool.complete(_req())

    assert resp.model == "m-b"
    assert [m for m, _ in slot.provider.calls] == ["m-a", "m-b"]

    rows = track.journal.rows_for(provider="groq", credential="groq#0", since=0.0)
    # One request served, priced at what the response actually cost. Not the
    # estimate, and emphatically not less than zero.
    assert sum(r.requests for r in rows) == 1
    assert sum(r.tokens for r in rows) == resp.tokens_in + resp.tokens_out
    # Two rids, because two calls were sent. One rid here is the bug itself.
    assert len({r.rid for r in rows if r.rid}) == 2


async def test_the_ration_is_reserved_against_the_model_that_serves(tmp_path):
    """`per_model` budgets meter per model, so reserving against `models[0]`
    rationed the whole slot out of one model's share while the rest of the
    account sat untouched -- and filed the usage under a model that never ran.
    Groq is the live case: 200,000 tokens per MODEL per day (CAPACITY.md §7).
    """
    from dataclasses import replace

    track, ration = _day_rationed(tmp_path, "groq")
    track.budgets["groq"] = replace(track.budgets["groq"], per_model=True)
    slot = _slot("groq", ["m-a", "m-b"], script=[ProviderError("m-a is down")])
    slot.credential_id = "groq#0"

    pool = ProviderPool([slot], budget=track, ration=ration)
    resp = await pool.complete(_req())
    assert resp.model == "m-b"

    served = track.journal.rows_for(
        provider="groq", credential="groq#0", model="m-b", since=0.0
    )
    assert sum(r.requests for r in served) == 1
    # And nothing was billed to the model that only failed.
    failed = track.journal.rows_for(
        provider="groq", credential="groq#0", model="m-a", since=0.0
    )
    assert sum(r.requests for r in failed) == 0
    assert sum(r.tokens for r in failed) == 0


async def test_one_call_costs_the_day_one_request(tmp_path):
    """The ration and the budget tracker share a journal, so the pool must not
    charge both.

    `ration.reserve` wrote +1 request and the token ESTIMATE, `ration.settle`
    corrected the tokens to the actual figure, and then the success path wrote a
    SECOND row of +1 request and the actual tokens. Every rationed call
    therefore cost the day two requests and twice its tokens: `providers budget`
    declared a free tier spent at half its real capacity, and the session
    envelope -- computed from the same rows -- handed out half the slice it had.
    On the numbers this project actually runs on that is the difference between
    ~2,200 and ~1,100 requests a day.
    """
    track, ration = _day_rationed(tmp_path, "groq")
    slot = _slot("groq", ["m"])
    slot.credential_id = "groq#0"

    pool = ProviderPool([slot], budget=track, ration=ration)
    resp = await pool.complete(_req())

    day = track.window_state("groq", "day")
    assert day.used_requests == 1
    assert day.used_tokens == resp.tokens_in + resp.tokens_out


async def test_the_token_estimate_is_measured_from_settled_calls_not_itself(tmp_path):
    """`observed_tokens_per_request` skipped rows with no request count, which
    is exactly the row the ration writes to CORRECT its estimate. It therefore
    averaged the estimate and never the outcome: the figure sat at its default
    forever, and every ration reserved that default no matter what the provider
    really charged. A pool sending 15-token calls kept reserving 1,250.
    """
    track, ration = _day_rationed(tmp_path, "groq")
    slot = _slot("groq", ["m"])
    slot.credential_id = "groq#0"
    pool = ProviderPool([slot], budget=track, ration=ration)

    resp = await pool.complete(_req())
    assert track.observed_tokens_per_request("groq") == (
        resp.tokens_in + resp.tokens_out
    )
