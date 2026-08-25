"""Tests for the chaos harness (chaos.py)."""

import asyncio

import pytest

from swarmd.chaos import ChaosHook, ChaosRunner
from swarmd.runtime import Runtime
from swarmd.scheduler import Scheduler
from swarmd.task import Task


def test_same_seed_same_kills() -> None:
    """Determinism: identical seeds produce identical kill sequences."""
    h1 = ChaosHook(seed=7, kill_rate=0.5)
    h2 = ChaosHook(seed=7, kill_rate=0.5)
    seq1 = [h1.should_kill() for _ in range(100)]
    seq2 = [h2.should_kill() for _ in range(100)]
    assert seq1 == seq2
    assert any(seq1) and not all(seq1)  # sanity: mixed outcomes at 0.5


def test_different_seeds_diverge() -> None:
    h1 = ChaosHook(seed=1, kill_rate=0.5)
    h2 = ChaosHook(seed=2, kill_rate=0.5)
    s1 = "".join(str(int(h1.should_kill())) for _ in range(50))
    s2 = "".join(str(int(h2.should_kill())) for _ in range(50))
    assert s1 != s2


def test_zero_rate_never_kills() -> None:
    h = ChaosHook(seed=1, kill_rate=0.0)
    assert not any(h.should_kill() for _ in range(100))


def test_unit_rate_always_kills() -> None:
    h = ChaosHook(seed=1, kill_rate=1.0)
    assert all(h.should_kill() for _ in range(100))


def test_invalid_rate_rejected() -> None:
    with pytest.raises(ValueError):
        ChaosHook(kill_rate=1.5)


async def test_chaos_runner_kills_live_agents() -> None:
    """With rate 1.0 and fast ticks, agents get killed while tasks are running."""
    rt = Runtime(Scheduler(), concurrency=2, lease_s=0.3)
    rt.register_task_type("job", ["slow"])
    started = [False]

    async def slow_step(_inp: object, _p: dict) -> dict:
        started[0] = True
        await asyncio.sleep(5)  # long enough to be killed mid-step
        return {}

    rt.register_step("job", "slow", slow_step)

    hook = ChaosHook(seed=99, kill_rate=1.0)
    runner = ChaosRunner(rt, hook, tick_s=0.02)

    await rt.start()
    runner.start()
    await rt.scheduler.submit(Task(payload={"type": "job", "n": 0}))
    await asyncio.sleep(0.3)
    await runner.stop()
    await rt.stop()

    assert runner.kills_done >= 1
    assert rt.stats.killed >= 1
