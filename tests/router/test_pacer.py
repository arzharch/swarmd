"""The pause: one per run, shared by every agent in the air.

A pause is the difference between a run that finishes tomorrow and a run that
died today. What makes it usable rather than a hang is that it is announced,
heartbeated, checkpointed, and endable early.

Waits here are milliseconds. A test that sleeps for a real ration window is a
test nobody runs.
"""

from __future__ import annotations

import asyncio
import time

import pytest

from swarmd.router.pacer import Paced, Pacer, PauseCause


def cause(*, seconds: float = 0.05, reason: str = "session_ration") -> PauseCause:
    return PauseCause(
        reason=reason,
        provider="p",
        credential="p#0",
        dimension="requests",
        used=225,
        envelope=225,
        resumes_at=time.time() + seconds,
    )


@pytest.fixture
async def pacer():
    # Real timings scaled down, not replaced: the pacer still sleeps, still
    # heartbeats, and still overshoots the estimate -- in milliseconds.
    p = Pacer(heartbeat_s=0.02, grace_s=0.02)
    yield p
    await p.aclose()


# --- the pause is announced --------------------------------------------------


async def test_a_pause_is_announced_before_it_is_waited_out(pacer):
    """A silent pause is indistinguishable from a hang, and the run is silent
    for hours precisely when an operator most wants to know why."""
    events = []
    pacer.emit = events.append

    await pacer.park(cause(), agent_id="a0001")

    kinds = [e["kind"] for e in events]
    assert "run_paused" in kinds
    assert kinds.index("run_paused") == 0


async def test_the_pause_event_carries_what_an_operator_needs(pacer):
    events = []
    pacer.emit = events.append
    pacer.checkpoint_path = "/state/run-1.json"

    started = time.time()
    await pacer.park(cause(), agent_id="a0001")

    paused = next(e for e in events if e["kind"] == "run_paused")
    assert paused["reason"] == "session_ration"
    assert paused["provider"] == "p"
    assert paused["dimension"] == "requests"
    # Compared against when the pause began, not against now: the pause has
    # already ended by the time this runs.
    assert paused["resumes_at"] > started
    # Where the work went, so a pause that outlives the process is recoverable
    # rather than merely survivable.
    assert paused["checkpoint_path"] == "/state/run-1.json"


async def test_a_pause_heartbeats_so_it_is_not_read_as_a_hang(pacer):
    events = []
    pacer.emit = events.append
    await pacer.park(cause(seconds=0.12))
    assert sum(1 for e in events if e["kind"] == "pace_waiting") >= 1


async def test_a_resume_is_announced_too(pacer):
    events = []
    pacer.emit = events.append
    await pacer.park(cause())
    assert any(e["kind"] == "run_resumed" for e in events)


# --- one pause, many agents --------------------------------------------------


async def test_every_agent_waits_on_one_pause_not_one_each(pacer):
    """Hundreds of agents each opening their own pause would emit hundreds of
    banners and, worse, each re-check capacity independently."""
    events = []
    pacer.emit = events.append

    await asyncio.gather(
        *(pacer.park(cause(seconds=0.08), agent_id=f"a{i}") for i in range(12))
    )
    assert sum(1 for e in events if e["kind"] == "run_paused") == 1
    assert pacer.pauses == 1


async def test_every_parked_agent_is_released(pacer):
    """One agent left parked after the wake is a run that never finishes."""
    released = []

    async def park(i):
        await pacer.park(cause(seconds=0.05), agent_id=f"a{i}")
        released.append(i)

    await asyncio.gather(*(park(i) for i in range(10)))
    assert sorted(released) == list(range(10))
    assert not pacer.waiting


async def test_an_earlier_opening_shortens_the_pause(pacer):
    """The pause exists to end as soon as it can. Ignoring a shorter wait would
    hold every agent to the longest estimate any of them found."""
    long_wait = cause(seconds=30)
    short_wait = cause(seconds=0.05)

    async def park_long():
        await pacer.park(long_wait, agent_id="slow")

    task = asyncio.create_task(park_long())
    await asyncio.sleep(0.01)
    await pacer.park(short_wait, agent_id="fast")
    await asyncio.wait_for(task, timeout=2.0)


# --- hooks -------------------------------------------------------------------


async def test_the_run_is_told_to_checkpoint_on_the_way_in(pacer):
    """Where a run releases what it must not hold for hours, and where the
    working set reaches disk."""
    seen = []
    pacer.on_pause = seen.append
    pacer.on_resume = lambda c: seen.append("resumed")

    await pacer.park(cause())
    assert len(seen) == 2
    assert seen[1] == "resumed"


async def test_a_hook_that_raises_does_not_strand_the_run(pacer):
    """A failing checkpoint makes the run less recoverable. Letting it also
    hang every parked agent would make it fatal."""

    def explode(_cause):
        raise RuntimeError("disk full")

    pacer.on_pause = explode
    await asyncio.wait_for(pacer.park(cause()), timeout=2.0)


async def test_an_emit_that_raises_does_not_strand_the_run(pacer):
    def explode(_event):
        raise RuntimeError("hub closed")

    pacer.emit = explode
    await asyncio.wait_for(pacer.park(cause()), timeout=2.0)


# --- no_wait -----------------------------------------------------------------


async def test_no_wait_raises_instead_of_parking(pacer):
    """One flag rather than a second code path: CI's fail-fast route is the
    same park() every other caller takes."""
    pacer.no_wait = True
    with pytest.raises(Paced):
        await pacer.park(cause(seconds=3600))


async def test_the_refusal_names_the_wait_it_declined(pacer):
    """"no capacity" without a time is what made a paused run look hung."""
    pacer.no_wait = True
    with pytest.raises(Paced) as excinfo:
        await pacer.park(cause(seconds=3600, reason="session_ration"))
    assert "session_ration" in str(excinfo.value)


async def test_a_refusal_is_announced_so_it_is_traceable(pacer):
    events = []
    pacer.emit = events.append
    pacer.no_wait = True
    with pytest.raises(Paced):
        await pacer.park(cause())
    assert any(e["kind"] == "run_pace_refused" for e in events)


# --- waking early ------------------------------------------------------------


async def test_a_pause_can_be_ended_early(pacer):
    """A provider that comes back sooner than predicted, or an operator who
    added a credential, should not have to wait out an estimate."""
    task = asyncio.create_task(pacer.park(cause(seconds=30)))
    await asyncio.sleep(0.02)
    await pacer.wake(by="operator")
    await asyncio.wait_for(task, timeout=2.0)


async def test_waking_a_pacer_that_is_not_paused_is_harmless(pacer):
    await pacer.wake()
    assert not pacer.paused


# --- stalling ----------------------------------------------------------------


async def test_repeated_pauses_in_a_row_are_reported_as_a_stall(pacer):
    """An ETA that keeps sliding is worse than a long one: the operator is
    told the run will resume and it repeatedly does not."""
    events = []
    pacer.emit = events.append

    for _ in range(4):
        await pacer.park(cause(seconds=0.02))

    assert pacer.extensions >= 1
    assert any(e["kind"] == "pace_stalled" for e in events)


async def test_a_stall_does_not_end_the_run(pacer):
    """Loud and still not fatal: the run must complete."""
    for _ in range(4):
        await pacer.park(cause(seconds=0.02))
    assert not pacer.paused


# --- status ------------------------------------------------------------------


async def test_status_distinguishes_a_parked_pool_from_a_running_one(pacer):
    assert pacer.status()["paused"] is False

    task = asyncio.create_task(pacer.park(cause(seconds=30), agent_id="a0001"))
    await asyncio.sleep(0.02)
    status = pacer.status()
    assert status["paused"] is True
    assert status["waiting_agents"] == 1
    assert status["resumes_at"] > time.time()

    await pacer.wake()
    await asyncio.wait_for(task, timeout=2.0)


async def test_time_spent_paused_is_accounted_for(pacer):
    """A run's wall-clock duration stops meaning anything if hours of parking
    are folded into it unlabelled."""
    await pacer.park(cause(seconds=0.05))
    assert pacer.paused_total_s > 0


async def test_closing_the_pacer_stops_its_ticker(pacer):
    """A heartbeat task that outlives its pool hangs the test suite, which is
    exactly how this was found."""
    task = asyncio.create_task(pacer.park(cause(seconds=30)))
    await asyncio.sleep(0.02)
    await pacer.aclose()
    await asyncio.wait_for(task, timeout=2.0)
    assert pacer._ticker is None or pacer._ticker.done()


# --- observability -----------------------------------------------------------


async def test_a_pause_is_visible_to_prometheus(pacer):
    """The pacer exists to stop hitting 429s, so "throttled" stopped being the
    saturation signal and "parked" became it. A parked run emits nothing,
    finishes nothing and errors on nothing -- without a metric it is
    indistinguishable from a hang to everything watching."""
    from swarmd.observability import metrics

    task = asyncio.create_task(pacer.park(cause(seconds=30), agent_id="a0001"))
    await asyncio.sleep(0.03)
    assert metrics.metric("run_paused")._value.get() == 1

    await pacer.wake()
    await asyncio.wait_for(task, timeout=2.0)
    assert metrics.metric("run_paused")._value.get() == 0


async def test_time_spent_parked_is_counted(pacer):
    from swarmd.observability import metrics

    before = metrics.metric("pause_seconds").labels(
        provider="p", reason="session_ration"
    )._value.get()
    await pacer.park(cause(seconds=0.05))
    after = metrics.metric("pause_seconds").labels(
        provider="p", reason="session_ration"
    )._value.get()
    assert after > before
