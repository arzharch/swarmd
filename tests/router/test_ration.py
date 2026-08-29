"""The session ration: what stops one afternoon from spending a whole day.

The budget tracker answers "is there anything left today". This answers "is
there anything left NOW", which is the question that keeps a run working across
several sittings instead of blocking on 429s by lunchtime.

Everything here uses an explicit `now`, never the wall clock. A rationing test
that waits for real time to pass is a test that is either slow or lying.
"""

from __future__ import annotations

import asyncio

import pytest

from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal
from swarmd.router.ration import (
    DAY,
    SAFETY,
    SESSION_LEN,
    SESSIONS_PER_DAY,
    Cost,
    InProcessRation,
    Ration,
    evaluate,
    session_slot,
)

T0 = 1_700_000_000.0  # a fixed instant; the tests never read the clock


def spec(
    *,
    requests: int | None = 1000,
    tokens: int | None = None,
    kind: str = "quota",
    reset: str = "rolling",
    per_model: bool = False,
) -> BudgetSpec:
    return BudgetSpec(
        provider="p",
        kind=kind,
        limits=(Limit("day", requests=requests, tokens=tokens),),
        reset=reset,
        source="test",
        checked="test",
        per_model=per_model,
    )


def tracker(tmp_path, budget_spec: BudgetSpec) -> BudgetTracker:
    return BudgetTracker(
        journal=UsageJournal(str(tmp_path / "usage.jsonl")),
        budgets={"p": budget_spec},
    )


def spend(track: BudgetTracker, n: int, *, at: float, tokens: int = 0, model: str = ""):
    for _ in range(n):
        track.record(
            provider="p", credential="", model=model,
            requests=1, tokens=tokens, ts=at, kind="ok",
        )


# --- the slice itself --------------------------------------------------------


def test_a_sitting_gets_its_share_and_not_the_day(tmp_path):
    """The whole point. A run that could spend 1000 requests in one afternoon
    is what produced the 429s this exists to prevent."""
    track = tracker(tmp_path, spec(requests=1000))
    ration = InProcessRation(track)
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY

    spend(track, share, at=T0)
    rows = ration.rows(spec(requests=1000), "", "", T0 - DAY)
    decision = evaluate(rows, spec(requests=1000), Cost(1), now=T0)

    assert not decision.admitted
    assert decision.reason == "session_ration"
    assert decision.envelope == share


def test_the_slice_leaves_the_rest_of_the_day_intact(tmp_path):
    """A refusal has to be "not yet", not "not ever". If the sitting's slice
    were taken out of tomorrow's, pausing would not help."""
    track = tracker(tmp_path, spec(requests=1000))
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY
    spend(track, share, at=T0)

    later = T0 + SESSION_LEN + 1
    rows = InProcessRation(track).rows(spec(requests=1000), "", "", later - DAY)
    assert evaluate(rows, spec(requests=1000), Cost(1), now=later).admitted


def test_a_sitting_does_not_shrink_its_own_ration_as_it_spends(tmp_path):
    """Earlier sittings' spend leaves the numerator; this sitting's does not.

    Subtracting the current sitting's own spend would make the envelope shrink
    with every call, so a run would be refused well before reaching the slice
    it was promised.
    """
    track = tracker(tmp_path, spec(requests=1000))
    ration = InProcessRation(track)
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY

    envelopes = []
    for i in range(0, share, max(1, share // 4)):
        rows = ration.rows(spec(requests=1000), "", "", T0 - DAY)
        envelopes.append(
            evaluate(rows, spec(requests=1000), Cost(1), now=T0).envelope
        )
        spend(track, max(1, share // 4), at=T0)
        del i
    assert len(set(envelopes)) == 1, f"envelope moved during a sitting: {envelopes}"


def test_an_unspent_sitting_rolls_forward(tmp_path):
    """An operator who runs nothing all morning should not be held to a
    quarter of the day at 3pm."""
    fixed = spec(requests=1000, reset="utc-midnight")
    track = tracker(tmp_path, fixed)
    slot = session_slot(fixed, T0)
    # Halfway through the day with nothing spent: the remaining slots share the
    # whole allowance, so each is bigger than a flat quarter.
    late = slot.day_start + DAY / 2 + 1
    envelope = Ration(track).envelope("p", now=late)["envelope_requests"]
    assert envelope > int(1000 * SAFETY) // SESSIONS_PER_DAY


# --- the dimensions ----------------------------------------------------------


def test_tokens_ration_separately_from_requests(tmp_path):
    """Groq blocked a run at 98 of 1000 requests because tokens were the
    binding limit. Rationing requests alone would repeat exactly that."""
    fixed = spec(requests=1000, tokens=200_000)
    track = tracker(tmp_path, fixed)
    share = int(200_000 * SAFETY) // SESSIONS_PER_DAY
    spend(track, 5, at=T0, tokens=share // 4)

    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)
    decision = evaluate(rows, fixed, Cost(1, tokens=share), now=T0)
    assert not decision.admitted
    assert decision.dimension == "tokens"


def test_the_binding_dimension_is_named(tmp_path):
    """"98/1000" printed beside "budget exhausted" was the most confusing
    possible way to report a token limit."""
    fixed = spec(requests=1000, tokens=200_000)
    track = tracker(tmp_path, fixed)
    spend(track, 400, at=T0, tokens=1)
    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)
    decision = evaluate(rows, fixed, Cost(1, tokens=1), now=T0)
    assert decision.dimension == "requests"


# --- what is NOT rationed ----------------------------------------------------


def test_a_rate_limited_provider_is_never_session_rationed(tmp_path):
    """Mistral publishes a per-minute rate and a monthly cap, no day. Cutting a
    day it does not have into sittings invents a scarcity and throws away the
    most generous free tier available."""
    fixed = spec(requests=None, tokens=None, kind="rate")
    track = tracker(tmp_path, fixed)
    spend(track, 10_000, at=T0)
    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)
    assert evaluate(rows, fixed, Cost(1), now=T0).admitted


def test_a_provider_the_tracker_says_is_blocked_is_refused_outright(tmp_path):
    """A 429 that named a daily metric outranks every published figure, and it
    stands until the instant the provider gave (ADR-008)."""
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    track.record(
        provider="p", credential="", requests=0, tokens=0,
        ts=T0, kind="exhausted", until=T0 + 3600,
    )
    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)
    decision = evaluate(rows, fixed, Cost(1), now=T0)
    assert not decision.admitted
    assert decision.reason == "observed_day_limit"
    assert decision.resumes_at == pytest.approx(T0 + 3600)


def test_the_provider_word_outranks_a_slice_that_would_have_admitted(tmp_path):
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    track.record(
        provider="p", credential="", requests=0, tokens=0,
        ts=T0, kind="exhausted", until=T0 + 60,
    )
    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)
    assert not evaluate(rows, fixed, Cost(1), now=T0).admitted


# --- per-credential and per-model metering ----------------------------------


def test_credentials_ration_independently(tmp_path):
    """Three Google keys are three daily allowances. Pooling them into one
    would waste two thirds of the capacity the operator supplied."""
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    ration = InProcessRation(track)
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY
    for _ in range(share):
        track.record(provider="p", credential="p#0", requests=1, ts=T0, kind="ok")

    spent = ration.rows(fixed, "p#0", "", T0 - DAY)
    fresh = ration.rows(fixed, "p#1", "", T0 - DAY)
    assert not evaluate(spent, fixed, Cost(1), now=T0).admitted
    assert evaluate(fresh, fixed, Cost(1), now=T0).admitted


def test_per_model_metering_is_only_applied_where_the_provider_does_it(tmp_path):
    """Groq meters 200K tokens per MODEL. Filtering by model unconditionally
    would hand a run that rotates models a fresh ration per rotation, which is
    a way to spend a day in an afternoon."""
    metered = spec(requests=1000, per_model=True)
    pooled = spec(requests=1000, per_model=False)
    track = tracker(tmp_path, metered)
    spend(track, 50, at=T0, model="a")

    assert len(InProcessRation(track).rows(metered, "", "b", T0 - DAY)) == 0
    assert len(InProcessRation(track).rows(pooled, "", "b", T0 - DAY)) == 50


# --- reservations ------------------------------------------------------------


async def test_concurrent_agents_cannot_exceed_the_slice(tmp_path):
    """Admission and reservation are ONE operation for this reason. A check
    followed by a separate write is a race that hundreds of agents will find,
    and every one that wins it is a 429.
    """
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    ration = Ration(track)
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY

    grants = await asyncio.gather(
        *(ration.reserve(provider="p", credential="", now=T0) for _ in range(share + 40))
    )
    assert sum(1 for g in grants if g.admitted) <= share


async def test_a_settled_call_charges_what_it_actually_used(tmp_path):
    fixed = spec(requests=1000, tokens=200_000)
    track = tracker(tmp_path, fixed)
    ration = Ration(track)

    grant = await ration.reserve(provider="p", credential="", now=T0)
    assert grant.admitted
    await ration.settle(grant, tokens=1234)

    rows = track.journal.rows_for(provider="p", credential="", since=T0 - DAY)
    assert sum(r.tokens for r in rows) == 1234


async def test_a_released_reservation_returns_the_capacity(tmp_path):
    """A reservation that is never settled would hold capacity forever, so a
    run that errors out would shrink every later sitting."""
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    ration = Ration(track)
    share = int(1000 * SAFETY) // SESSIONS_PER_DAY

    held = [await ration.reserve(provider="p", credential="", now=T0) for _ in range(share)]
    assert not (await ration.reserve(provider="p", credential="", now=T0)).admitted

    for grant in held:
        await ration.release(grant)
    assert (await ration.reserve(provider="p", credential="", now=T0)).admitted


async def test_a_rate_limited_call_keeps_the_request_and_returns_the_tokens(tmp_path):
    """The request reached the provider and counts against the day. The tokens
    did not, and charging for them would make every 429 shrink the ration
    twice."""
    fixed = spec(requests=1000, tokens=200_000)
    track = tracker(tmp_path, fixed)
    ration = Ration(track)

    grant = await ration.reserve(provider="p", credential="", now=T0)
    await ration.settle(grant, tokens=5000, outcome="rate_limited")

    rows = track.journal.rows_for(provider="p", credential="", since=T0 - DAY)
    assert sum(r.requests for r in rows) >= 1
    assert sum(r.tokens for r in rows) == 0


async def test_abandoned_reservations_are_reaped(tmp_path):
    """A killed agent leaves a reservation nobody will settle. Without a reaper
    those accumulate until the sitting is refused on capacity that was never
    actually spent."""
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    ration = Ration(track)

    await ration.reserve(provider="p", credential="", now=T0)
    assert ration.reap(now=T0 + DAY) >= 1


# --- refusals are actionable -------------------------------------------------


def test_a_refusal_says_when_to_come_back(tmp_path):
    """"No capacity" with no time attached is what made a paused run
    indistinguishable from a hung one."""
    fixed = spec(requests=1000)
    track = tracker(tmp_path, fixed)
    spend(track, int(1000 * SAFETY) // SESSIONS_PER_DAY, at=T0)
    rows = InProcessRation(track).rows(fixed, "", "", T0 - DAY)

    decision = evaluate(rows, fixed, Cost(1), now=T0)
    assert decision.wait_s > 0
    assert decision.resumes_at > T0
    assert decision.reason


def test_a_scheduled_reset_is_not_raced(tmp_path):
    """Spending in the last seconds before a provider's midnight reset lands on
    whichever side of the boundary the provider's clock says, not ours."""
    fixed = spec(requests=1000, reset="utc-midnight")
    track = tracker(tmp_path, fixed)
    slot = session_slot(fixed, T0)
    just_before = slot.reset_at - 1
    rows = InProcessRation(track).rows(fixed, "", "", just_before - DAY)

    decision = evaluate(rows, fixed, Cost(1), now=just_before)
    assert not decision.admitted
    assert decision.reason == "reset_guard_band"
    assert decision.resumes_at == pytest.approx(slot.reset_at)


def test_a_rolling_provider_has_no_boundary_to_align_to(tmp_path):
    """Groq does not publish its daily reset instant, so the sitting is the
    trailing window rather than a slice of a calendar day."""
    slot = session_slot(spec(requests=1000, reset="rolling"), T0)
    assert slot.rolling
    assert slot.slot_start == pytest.approx(T0 - SESSION_LEN)
