"""The preflight timeline: when a run's calls actually get spent.

A yes/no verdict was the right shape when running out meant failing. It is the
wrong shape now that a run pauses and resumes: "does not fit today" covers both
"finishes at 18:40 after one pause" and "spans three days", and only the second
is a reason not to start.
"""

from __future__ import annotations

import time

import pytest

from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal
from swarmd.router.ration import DAY, SESSION_LEN, SESSIONS_PER_DAY, Ration


def tracker_with(day_requests: int, tmp_path, *, kind: str = "quota") -> BudgetTracker:
    return BudgetTracker(
        journal=UsageJournal(str(tmp_path / "usage.jsonl")),
        budgets={
            "p": BudgetSpec(
                provider="p",
                kind=kind,
                limits=(Limit("day", requests=day_requests),),
                reset="rolling",
                source="test",
                checked="test",
            )
        },
    )


@pytest.fixture
def ration(tmp_path):
    return Ration(tracker_with(4000, tmp_path))


def test_a_small_run_finishes_in_the_current_sitting(ration):
    plan = ration.forecast(10)
    assert plan["verdict"] == "fits_this_session"
    assert plan["expected_pauses"] == 0
    assert plan["first_pause_at"] is None


def test_a_run_past_one_slice_pauses_rather_than_failing(ration):
    """The distinction the whole feature exists for: this is a run that
    completes, later, not a run that cannot start."""
    per_session = ration.session_capacity()
    assert per_session > 0

    plan = ration.forecast(per_session + 5)
    assert plan["verdict"] in {"fits_today_with_pauses", "spans_days"}
    assert plan["expected_pauses"] >= 1
    assert plan["first_pause_at"] is not None
    assert plan["projected_finish"] > plan["first_pause_at"]


def test_a_run_past_a_day_says_it_spans_days(ration):
    per_session = ration.session_capacity()
    plan = ration.forecast(per_session * (SESSIONS_PER_DAY + 2))
    assert plan["verdict"] == "spans_days"
    assert plan["projected_finish"] > time.time() + DAY


def test_a_run_that_never_finishes_is_named_rather_than_projected(ration):
    """Reporting a finish date a week out as if it were a plan would be worse
    than saying the allowance is wrong for the run."""
    plan = ration.forecast(10_000_000)
    assert plan["verdict"] == "exceeds_horizon"


def test_the_projection_is_session_shaped(ration):
    plan = ration.forecast(ration.session_capacity() + 5)
    first = plan["timeline"][0]
    assert first["end"] - first["start"] == pytest.approx(SESSION_LEN, rel=0.01)


def test_a_rate_limited_provider_is_not_counted_as_an_allowance(tmp_path):
    """A per-minute rate multiplied out is not a plannable budget. Counting it
    made every preflight answer "fits", which is the failure this excludes."""
    ration = Ration(tracker_with(4000, tmp_path, kind="rate"))
    assert ration.session_capacity() == 0


def test_more_credentials_mean_more_capacity(ration):
    """Three keys are three daily allowances. Counting them as one understates
    capacity by 3x and projects pauses a real run would never hit."""
    one = ration.forecast(500, credentials={"p": ["p#0"]})
    three = ration.forecast(500, credentials={"p": ["p#0", "p#1", "p#2"]})
    assert three["session_capacity"] > one["session_capacity"]


def test_spent_capacity_shortens_the_first_window_only(tmp_path):
    """The current sitting uses real headroom; later ones assume a full slice.

    A forecast that applied today's spend to every future window would predict
    a run stuck forever after one busy afternoon.
    """
    tracker = tracker_with(4000, tmp_path)
    ration = Ration(tracker)
    fresh = ration.forecast(50)["session_capacity"]

    now = time.time()
    for _ in range(200):
        tracker.record(
            provider="p", credential="", requests=1, tokens=100, ts=now, kind="ok"
        )

    after = ration.forecast(50)
    assert after["session_capacity"] < fresh
    later = [w for w in after["timeline"][1:]]
    if later:
        assert later[0]["capacity"] > after["session_capacity"]


def test_the_pool_forecasts_against_the_credentials_it_actually_loaded(tmp_path):
    """The preflight test below stubs the forecast, so this is what proves the
    real pool method exists and reads its own slots."""
    from swarmd.router.pool import ProviderPool, ProviderSpec, _Slot
    from tests.router.test_pool import FakeProvider

    spec = ProviderSpec(
        name="p", base_url="http://x", api_key_env="X", models=("m",), tier="free"
    )
    slots = [
        _Slot(FakeProvider("p", ("m",), None), spec, credential_id=f"p#{i}")
        for i in range(3)
    ]
    pool = ProviderPool(
        slots,  # type: ignore[arg-type]
        budget=tracker_with(4000, tmp_path),
        ration=Ration(tracker_with(4000, tmp_path)),
    )
    assert pool.credential_map() == {"p": ["p#0", "p#1", "p#2"]}
    assert pool.forecast(10)["verdict"] == "fits_this_session"


async def test_preflight_carries_the_forecast_to_the_operator(tmp_path):
    """The projection is worthless if it stops at the ration."""
    from swarmd.swarm.run import SwarmRun
    from tests.swarm.test_run import ScriptedProvider

    class WithForecast(ScriptedProvider):
        def forecast(self, estimated_calls):
            return {
                "verdict": "fits_today_with_pauses",
                "estimated_calls": estimated_calls,
                "sessions_needed": 2,
                "expected_pauses": 1,
                "first_pause_at": time.time() + SESSION_LEN,
                "projected_finish": time.time() + 2 * SESSION_LEN,
                "session_capacity": 100,
                "timeline": [],
            }

    run = SwarmRun(WithForecast(), profile="smoke", agents=4)
    payload = run.preflight()
    assert payload["forecast"]["verdict"] == "fits_today_with_pauses"
    assert payload["forecast"]["expected_pauses"] == 1


async def test_a_broken_forecast_does_not_stop_the_run(tmp_path):
    """Being unable to predict the timeline is not a reason to refuse to start."""
    from swarmd.swarm.run import SwarmRun
    from tests.swarm.test_run import ScriptedProvider

    class Broken(ScriptedProvider):
        def forecast(self, estimated_calls):
            raise RuntimeError("clock unavailable")

    run = SwarmRun(Broken(), profile="smoke", agents=4)
    payload = run.preflight()
    assert "forecast" not in payload
    result = await run.run("summarise the source records")
    assert result.status == "completed"
