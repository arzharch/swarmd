"""Long-window budgets.

These cover the properties that decide whether a run may start: what is left in
each window, what "left" means for a grant that never refills, and whether any
of it survives a restart. The last one is the whole reason the module exists --
a per-month figure held in memory is a per-process figure wearing a month's
name.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

import pytest

from swarmd.router.budget import (
    BUDGETS,
    DAY,
    HOUR,
    MONTH,
    SESSION,
    WEEK,
    BudgetSpec,
    BudgetTracker,
    Limit,
    UsageJournal,
    next_pacific_midnight,
)

SPECS = {
    "rated": BudgetSpec(
        provider="rated",
        kind="rate",
        limits=(Limit("minute", requests=10), Limit("day", requests=100)),
    ),
    "granted": BudgetSpec(
        provider="granted",
        kind="grant",
        limits=(Limit("minute", requests=40),),
        grant_total=50,
        grant_expires_days=10,
    ),
}


def _tracker(tmp_path, specs=None):
    return BudgetTracker(
        journal=UsageJournal(tmp_path / "usage.jsonl"),
        budgets=specs if specs is not None else SPECS,
    )


# --- windows ----------------------------------------------------------------


def test_usage_inside_the_window_counts_and_older_usage_does_not(tmp_path):
    tracker = _tracker(tmp_path)
    now = time.time()
    tracker.record(provider="rated", credential="k#0", ts=now - 30)
    tracker.record(provider="rated", credential="k#0", ts=now - 30)
    tracker.record(provider="rated", credential="k#0", ts=now - 600)  # >1 minute

    minute = tracker.window_state("rated", "minute", now=now)
    assert minute.used_requests == 2
    assert minute.remaining_requests == 8

    day = tracker.window_state("rated", "day", now=now)
    assert day.used_requests == 3


def test_tokens_and_requests_are_tracked_separately(tmp_path):
    """The first ceiling reached is what stops the call."""
    spec = BudgetSpec(
        provider="p", limits=(Limit("hour", requests=100, tokens=1_000),)
    )
    tracker = _tracker(tmp_path, {"p": spec})
    tracker.record(provider="p", credential="k#0", requests=1, tokens=1_000)

    state = tracker.window_state("p", "hour")
    assert state.remaining_requests == 99
    assert state.remaining_tokens == 0
    assert state.exhausted, "a spent token budget must block despite spare requests"


def test_the_five_hour_session_is_its_own_window(tmp_path):
    """The unit an operator plans in, and not derivable from the others."""
    tracker = _tracker(
        tmp_path, {"p": BudgetSpec(provider="p", limits=(Limit("session", requests=20),))}
    )
    now = time.time()
    tracker.record(provider="p", credential="k#0", ts=now - HOUR * 4)
    tracker.record(provider="p", credential="k#0", ts=now - HOUR * 6)  # outside

    state = tracker.window_state("p", "session", now=now)
    assert state.used_requests == 1
    assert state.resets_in_s == pytest.approx(SESSION)


@pytest.mark.parametrize(
    ("window", "duration"),
    [("hour", HOUR), ("session", SESSION), ("day", DAY), ("week", WEEK), ("month", MONTH)],
)
def test_every_window_is_available(tmp_path, window, duration):
    tracker = _tracker(
        tmp_path, {"p": BudgetSpec(provider="p", limits=(Limit(window, requests=5),))}
    )
    now = time.time()
    tracker.record(provider="p", credential="k#0", ts=now - duration * 0.5)
    tracker.record(provider="p", credential="k#0", ts=now - duration * 2)

    assert tracker.window_state("p", window, now=now).used_requests == 1


# --- calendar resets --------------------------------------------------------


def test_a_daily_quota_that_resets_on_a_wall_clock_is_not_rolling(tmp_path):
    """Google's RPD resets at midnight Pacific.

    Treating it as a rolling 24 hours under-uses it all morning and
    over-commits against it at night.
    """
    spec = BudgetSpec(
        provider="g",
        kind="quota",
        limits=(Limit("day", requests=100),),
        resets_at_pacific_midnight=True,
    )
    tracker = _tracker(tmp_path, {"g": spec})
    now = time.time()
    state = tracker.window_state("g", "day", now=now)

    assert 0 < state.resets_in_s <= DAY
    assert state.resets_in_s == pytest.approx(next_pacific_midnight(now) - now)


def test_pacific_midnight_lands_at_local_midnight():
    ts = next_pacific_midnight(
        datetime(2026, 7, 1, 18, 0, tzinfo=UTC).timestamp()
    )
    when = datetime.fromtimestamp(ts, tz=UTC)
    # July is PDT (UTC-7), so local midnight is 07:00 UTC.
    assert (when.hour, when.minute) == (7, 0)


def test_the_offset_follows_daylight_saving():
    """An hour of error at the boundary is an hour of imagined quota."""
    winter = datetime.fromtimestamp(
        next_pacific_midnight(datetime(2026, 1, 15, 18, 0, tzinfo=UTC).timestamp()),
        tz=UTC,
    )
    summer = datetime.fromtimestamp(
        next_pacific_midnight(datetime(2026, 7, 15, 18, 0, tzinfo=UTC).timestamp()),
        tz=UTC,
    )
    assert winter.hour == 8   # PST, UTC-8
    assert summer.hour == 7   # PDT, UTC-7


# --- grants -----------------------------------------------------------------


def test_a_grant_counts_every_request_ever_made(tmp_path):
    """It has no window. Spending it is permanent."""
    tracker = _tracker(tmp_path)
    now = time.time()
    tracker.record(provider="granted", credential="k#0", ts=now - MONTH * 3)
    tracker.record(provider="granted", credential="k#0", ts=now)

    grant = tracker.grant_state("granted", now=now)
    assert grant is not None
    assert grant["used"] == 2
    assert grant["remaining"] == 48


def test_a_replenishing_provider_has_no_grant(tmp_path):
    assert _tracker(tmp_path).grant_state("rated") is None


def test_an_exhausted_grant_blocks(tmp_path):
    tracker = _tracker(tmp_path)
    for _ in range(50):
        tracker.record(provider="granted", credential="k#0")
    assert tracker.blocked("granted") == "grant exhausted"


def test_a_grant_is_planned_over_its_remaining_life(tmp_path):
    """50 credits over 10 days is 5/day, not 40/minute.

    Reported as its per-minute rate a grant looks like abundance right up until
    it stops, which is the specific way this number misleads.
    """
    tracker = _tracker(tmp_path)
    assert tracker.sustainable_requests_per_day("granted") == 5


def test_the_capacity_plan_excludes_grants_from_what_is_sustainable(tmp_path):
    """A month cannot be planned on a pool that does not refill."""
    tracker = _tracker(tmp_path)
    plan = tracker.capacity_plan()

    assert plan["sustainable_daily_requests"] == 100      # the rated provider
    assert plan["grant_backed_daily_requests"] == 5       # reported, not counted
    assert plan["month_requests"] == 3_000


def test_a_token_cap_can_bind_before_the_request_cap(tmp_path):
    """The correction that cut the capacity plan almost in half.

    Groq publishes 1,000 requests AND 100,000 tokens per day. At the ~1,035
    tokens per call this system actually sends, the token budget runs out after
    98 requests -- so reporting 1,000/day overstated the provider tenfold, and
    the operator saw "day budget exhausted" printed beside "98 / 1,000".
    """
    spec = BudgetSpec(
        provider="p",
        kind="quota",
        limits=(Limit("day", requests=1_000, tokens=100_000),),
    )
    tracker = _tracker(tmp_path, {"p": spec})
    # 20 calls at 1,000 tokens each: the observed rate.
    for _ in range(20):
        tracker.record(provider="p", credential="k#0", requests=1, tokens=1_000)

    value, basis = tracker.daily_capacity("p")
    assert basis == "daily_cap_tokens"
    assert value == 100      # 100,000 tokens / 1,000 per call
    assert value < 1_000


def test_the_request_cap_binds_when_calls_are_small(tmp_path):
    """The token dimension must not be assumed to dominate either."""
    spec = BudgetSpec(
        provider="p",
        kind="quota",
        limits=(Limit("day", requests=50, tokens=100_000),),
    )
    tracker = _tracker(tmp_path, {"p": spec})
    for _ in range(10):
        tracker.record(provider="p", credential="k#0", requests=1, tokens=10)

    value, basis = tracker.daily_capacity("p")
    assert basis == "daily_cap"
    assert value == 50


def test_tokens_per_request_is_measured_not_assumed(tmp_path):
    """It depends on prompt size, which depends on schema hints and retrieved
    skills, which change."""
    tracker = _tracker(tmp_path, {"p": BudgetSpec(provider="p")})
    tracker.record(provider="p", credential="k#0", requests=2, tokens=3_000)
    assert tracker.observed_tokens_per_request("p") == 1_500


def test_tokens_per_request_falls_back_when_there_is_no_history(tmp_path):
    tracker = _tracker(tmp_path, {"p": BudgetSpec(provider="p")})
    assert tracker.observed_tokens_per_request("p", default=777) == 777


# --- blocking ---------------------------------------------------------------


def test_an_exhausted_window_blocks_and_names_itself(tmp_path):
    tracker = _tracker(tmp_path)
    for _ in range(100):
        tracker.record(provider="rated", credential="k#0")
    assert tracker.blocked("rated") == "day budget exhausted"


def test_headroom_does_not_block(tmp_path):
    tracker = _tracker(tmp_path)
    tracker.record(provider="rated", credential="k#0")
    assert tracker.blocked("rated") == ""


def test_a_provider_with_no_declared_budget_never_blocks(tmp_path):
    """Absence of a limit is not a limit of zero."""
    assert _tracker(tmp_path).blocked("unknown-provider") == ""


# --- durability -------------------------------------------------------------


def test_usage_survives_a_restart(tmp_path):
    """The whole reason this is not a counter in memory.

    A month tracked in a process is a process tracked in a month.
    """
    path = tmp_path / "usage.jsonl"
    first = BudgetTracker(journal=UsageJournal(path), budgets=SPECS)
    for _ in range(7):
        first.record(provider="rated", credential="k#0")

    reopened = BudgetTracker(journal=UsageJournal(path), budgets=SPECS)
    assert reopened.window_state("rated", "day").used_requests == 7


def test_a_torn_final_line_costs_one_row_not_the_file(tmp_path):
    path = tmp_path / "usage.jsonl"
    tracker = BudgetTracker(journal=UsageJournal(path), budgets=SPECS)
    tracker.record(provider="rated", credential="k#0")
    with path.open("a", encoding="utf-8") as handle:
        handle.write('{"ts": 123, "provider": "rat')

    reopened = BudgetTracker(journal=UsageJournal(path), budgets=SPECS)
    assert reopened.window_state("rated", "day").used_requests == 1


def test_compaction_drops_rows_no_window_can_ask_about(tmp_path):
    path = tmp_path / "usage.jsonl"
    journal = UsageJournal(path)
    now = time.time()
    journal.record(provider="rated", credential="k#0", ts=now - MONTH * 6)
    journal.record(provider="rated", credential="k#0", ts=now)

    assert journal.compact(now=now) == 1
    assert len(UsageJournal(path).load()) == 1


def test_an_unwritable_journal_degrades_rather_than_failing(tmp_path):
    """Losing durability must not stop a run over bookkeeping."""
    journal = UsageJournal(tmp_path / "nope" / "deep" / "usage.jsonl")
    journal.path = tmp_path  # a directory: every write will fail
    journal.record(provider="rated", credential="k#0")
    assert len(journal.load()) == 1


# --- the shipped table ------------------------------------------------------


def test_every_declared_budget_cites_a_source_and_a_date():
    """A limit with no provenance cannot be re-checked when it stops matching."""
    for name, spec in BUDGETS.items():
        assert spec.source, f"{name} has no source"
        assert spec.checked, f"{name} has no checked date"


def test_nvidia_is_declared_a_grant_not_a_tier():
    """The distinction the whole month plan rests on."""
    spec = BUDGETS["nvidia-nim"]
    assert spec.kind == "grant"
    assert spec.grant_total and spec.grant_expires_days


def test_the_journal_path_is_overridable_by_environment(monkeypatch, tmp_path):
    """The hook that keeps a test run from spending the operator's real budget.

    Without it, any BudgetTracker built with defaults writes to the live
    `.swarmd/usage.jsonl`. That is not hypothetical: it happened, and a test run
    showed up as 36 consumed requests in `swarmd providers budget`.
    """
    target = tmp_path / "elsewhere.jsonl"
    monkeypatch.setenv("SWARMD_USAGE_JOURNAL", str(target))
    journal = UsageJournal()
    journal.record(provider="rated", credential="k#0")
    assert target.exists()


def test_cerebras_is_not_declared_at_all():
    """Its key returns 402. A generous budget for a provider that refuses
    every call would be the most misleading entry in the table."""
    assert "cerebras" not in BUDGETS
