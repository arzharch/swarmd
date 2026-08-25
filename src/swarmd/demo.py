"""Demo: kernel kill-and-resume determinism.

Runs N tasks through a multi-step pipeline twice — once clean, once under chaos
kills — and prints both output hashes. The gate requires them to be EQUAL:
chaos may slow the system down, but it must never change the result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

from swarmd.chaos import ChaosHook, ChaosRunner
from swarmd.events import EventBus
from swarmd.runtime import Runtime
from swarmd.scheduler import Scheduler
from swarmd.task import Task

DEMO_STEPS = ["fetch", "normalize", "enrich", "score"]


def _make_runtime(
    bus: EventBus | None, concurrency: int, lease_s: float, steps: list[str]
) -> Runtime:
    rt = Runtime(Scheduler(), bus=bus, concurrency=concurrency, lease_s=lease_s)
    rt.register_task_type("demo", steps)

    # Step latency (0.15s) vs chaos tick (0.5s): an agent usually finishes its
    # current step before the next kill roll, so progress is steady but kills
    # still land mid-task frequently enough to exercise checkpoint/resume.
    async def synth(_inp: object, payload: dict[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.15)
        return {"n": payload["n"]}

    for i, step in enumerate(steps):
        if i == 0:
            rt.register_step("demo", step, synth)
        else:
            # Each step folds the previous step's output; deterministic by design.

            def make_fold(prev_name: str) -> Any:
                async def fold(
                    inp: object, _payload: dict[str, Any]
                ) -> dict[str, Any]:
                    await asyncio.sleep(0.15)
                    return {
                        "acc": json.dumps(inp, sort_keys=True) + f"+{prev_name}"
                    }

                return fold

            rt.register_step("demo", step, make_fold(steps[i - 1]))
    return rt


def integrity_hash(rt: Runtime) -> str:
    """Order-independent hash over completed task outputs.

    Task IDs are random UUIDs and differ between runs by design — the integrity
    guarantee is about WORK DONE (which tasks finished, with what output), not
    about internal identifiers.
    """
    records = sorted(
        json.dumps(r.output, sort_keys=True) for r in rt.results().values() if r.ok
    )
    return hashlib.sha256("\n".join(records).encode()).hexdigest()[:16]


async def run_once(
    n_tasks: int,
    *,
    chaos: ChaosHook | None = None,
    concurrency: int = 4,
    lease_s: float = 0.5,
) -> tuple[Runtime, ChaosRunner | None]:
    """Run the demo pipeline once; returns the runtime for inspection."""
    bus = EventBus()
    rt = _make_runtime(bus, concurrency, lease_s, DEMO_STEPS)

    runner = None
    if chaos is not None:
        runner = ChaosRunner(rt, chaos)
        runner.start()

    await rt.start()
    for i in range(n_tasks):
        await rt.scheduler.submit(Task(payload={"type": "demo", "n": i}))

    # Under heavy chaos progress is slow (workers die constantly), so scale
    # the timeout with kill pressure rather than failing legitimate runs.
    timeout_s = 30.0 if chaos is None else 120.0
    try:
        await asyncio.wait_for(
            _wait_until(lambda: rt.stats.completed >= n_tasks, timeout=timeout_s),
            timeout=timeout_s + 5,
        )
    finally:
        await rt.stop()
        if runner:
            await runner.stop()
    return rt, runner


async def _wait_until(pred: Any, timeout: float) -> bool:
    deadline = asyncio.get_event_loop().time() + timeout
    while not pred():
        if asyncio.get_event_loop().time() > deadline:
            raise TimeoutError()
        await asyncio.sleep(0.01)
    return True


async def demo_kernel(kill_rate: float, n_tasks: int, seed: int) -> dict[str, Any]:
    """Run clean vs chaos and return both hashes plus stats."""
    clean_rt, _ = await run_once(n_tasks)
    clean_hash = integrity_hash(clean_rt)

    chaos = ChaosHook(seed=seed, kill_rate=kill_rate)
    chaos_rt, runner = await run_once(n_tasks, chaos=chaos)
    chaos_hash = integrity_hash(chaos_rt)

    return {
        "tasks": n_tasks,
        "kill_rate": kill_rate,
        "seed": seed,
        "clean_hash": clean_hash,
        "chaos_hash": chaos_hash,
        "match": clean_hash == chaos_hash,
        "kills": chaos_rt.stats.killed,
        "requeues": chaos_rt.stats.requeued,
        "chaos_ticks": runner.kills_done if runner else 0,
    }
