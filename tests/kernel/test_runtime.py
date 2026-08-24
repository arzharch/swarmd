"""Tests for runtime: heartbeat expiry, requeue-with-checkpoint, kill-and-resume."""

import asyncio

import pytest

from swarmd.events import EventBus
from swarmd.runtime import Runtime
from swarmd.scheduler import Scheduler


def make_runtime(concurrency: int = 2, lease_s: float = 0.3, **kw: float) -> Runtime:
    return Runtime(Scheduler(), concurrency=concurrency, lease_s=lease_s, **kw)


async def test_all_tasks_complete_clean_run() -> None:
    rt = make_runtime()
    rt.register_task_type("job", ["a", "b"])
    rt.register_step("job", "a", lambda _in, p: _async({"done": p["n"]}))
    rt.register_step("job", "b", lambda _in, p: _async({"final": True}))

    await rt.start()
    for i in range(5):
        await rt.scheduler.submit(Task_of(i))
    await _drain(rt, expected=5)
    await rt.stop()

    assert rt.stats.completed == 5
    assert all(r.ok for r in rt.results().values())


async def test_kill_midwork_requeues_with_checkpoint() -> None:
    """Kill an agent between steps; replacement must skip completed steps."""
    bus = EventBus()
    qid, _q = bus.subscribe(maxsize=512)
    rt = Runtime(Scheduler(), bus=bus, concurrency=1, lease_s=0.2)
    executed: list[str] = []
    killed = [False]

    async def step_a(_inp: object, payload: dict) -> dict:
        executed.append(f"a:{payload['n']}")
        # Kill this agent once, mid-step; the lease must expire and requeue.
        if payload["n"] == 0 and not killed[0]:
            killed[0] = True
            agents = rt.live_agent_ids()
            rt.kill_agent(agents[0])
            await asyncio.sleep(10)  # never returns; cancellation happens first
        return {"a": payload["n"]}

    async def step_b(_inp: object, payload: dict) -> dict:
        executed.append(f"b:{payload['n']}")
        return {"b": payload["n"]}

    rt.register_task_type("job", ["a", "b"])
    rt.register_step("job", "a", step_a)
    rt.register_step("job", "b", step_b)

    await rt.start()
    await rt.scheduler.submit(Task_of(0))
    await asyncio.wait_for(
        _wait_until(lambda: rt.stats.completed == 1, timeout=5), timeout=6
    )
    await rt.stop()

    # Step 'a' ran twice (killed + replacement), but the task completed exactly once.
    assert rt.stats.completed == 1
    assert rt.stats.requeued >= 1
    assert executed.count("b:0") == 1
    result = next(iter(rt.results().values()))
    assert result.ok and result.output["b"]["b"] == 0
    bus.unsubscribe(qid)


async def test_requeue_preserves_completed_steps_deterministically() -> None:
    """Two kills in a row; final output must equal a clean run's output."""
    clean_rt = make_runtime()
    clean_rt.register_task_type("job", ["s1", "s2"])
    clean_rt.register_step("job", "s1", lambda _i, p: _async(p["n"] * 2))
    clean_rt.register_step("job", "s2", lambda i, p: _async((i or 0) + 1))

    await clean_rt.start()
    await clean_rt.scheduler.submit(Task_of(7))
    await _drain(clean_rt, expected=1)
    await clean_rt.stop()
    clean_output = next(iter(clean_rt.results().values())).output

    chaos_rt = make_runtime(lease_s=0.15)
    chaos_rt.register_task_type("job", ["s1", "s2"])

    async def s1(_inp: object, payload: dict) -> int:
        agents = chaos_rt.live_agent_ids()
        if payload["n"] == 7 and agents and chaos_rt.stats.killed < 2:
            chaos_rt.kill_agent(agents[0])
            await asyncio.sleep(10)
        return payload["n"] * 2

    chaos_rt.register_step("job", "s1", s1)
    chaos_rt.register_step("job", "s2", lambda i, p: _async((i or 0) + 1))

    await chaos_rt.start()
    await chaos_rt.scheduler.submit(Task_of(7))
    await asyncio.wait_for(
        _wait_until(lambda: chaos_rt.stats.completed == 1, timeout=5), timeout=6
    )
    await chaos_rt.stop()

    assert chaos_rt.stats.killed >= 2
    assert next(iter(chaos_rt.results().values())).output == clean_output


async def test_lease_expiry_does_not_double_complete() -> None:
    """A task completed exactly once even when claims expire aggressively."""
    rt = make_runtime(concurrency=4, lease_s=0.05, reap_interval_s=0.02)
    rt.register_task_type("job", ["only"])
    rt.register_step("job", "only", lambda _i, p: _async(True))

    await rt.start()
    for i in range(20):
        await rt.scheduler.submit(Task_of(i))
    await _drain(rt, expected=20)
    await rt.stop()

    assert rt.stats.completed == 20
    assert len(rt.results()) == 20


# ---- helpers -------------------------------------------------------------


async def _async(value: object) -> object:
    await asyncio.sleep(0)
    return value


def Task_of(n: int):
    from swarmd.task import Task

    return Task(payload={"type": "job", "n": n})


async def _drain(rt: Runtime, expected: int, timeout: float = 5) -> None:
    """Wait until `expected` tasks have completed."""
    await _wait_until(lambda: rt.stats.completed >= expected, timeout=timeout)


async def _wait_until(pred, timeout: float):
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError()
        await asyncio.sleep(0.01)
    return True


@pytest.mark.filterwarnings("ignore::pytest.PytestUnraisableExceptionWarning")
def test_no_warnings_import() -> None:
    """Import sanity: runtime module exposes its public surface."""
    from swarmd import runtime

    assert hasattr(runtime, "Runtime")
    assert hasattr(runtime, "RuntimeStats")
