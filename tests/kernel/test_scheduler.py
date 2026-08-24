"""Tests for the scheduler (scheduler.py)."""

import asyncio

import pytest

from swarmd.scheduler import Scheduler
from swarmd.task import Task


async def test_priority_ordering_low_value_first() -> None:
    s = Scheduler()
    await s.submit(Task(payload={"n": "low"}, priority=10))
    await s.submit(Task(payload={"n": "high"}, priority=1))
    await s.submit(Task(payload={"n": "mid"}, priority=5))

    out = [await s.claim() for _ in range(3)]
    assert [t.payload["n"] for t in out] == ["high", "mid", "low"]


async def test_fifo_within_same_priority() -> None:
    s = Scheduler()
    for i in range(5):
        await s.submit(Task(payload={"i": i}, priority=0))

    out = [await s.claim() for _ in range(5)]
    assert [t.payload["i"] for t in out] == [0, 1, 2, 3, 4]


async def test_claim_waits_until_task_submitted() -> None:
    s = Scheduler()

    async def producer() -> None:
        await asyncio.sleep(0.01)
        await s.submit(Task(payload={"late": True}))

    consumer = asyncio.create_task(s.claim())
    asyncio.create_task(producer())
    task = await asyncio.wait_for(consumer, timeout=1)
    assert task.payload == {"late": True}


async def test_backpressure_blocks_at_capacity() -> None:
    s = Scheduler(maxsize=2)
    await s.submit(Task(payload={"a": 1}))
    await s.submit(Task(payload={"b": 2}))

    submitted = asyncio.Event()

    async def blocked_producer() -> None:
        await s.submit(Task(payload={"c": 3}))
        submitted.set()

    producer_task = asyncio.create_task(blocked_producer())
    await asyncio.sleep(0.01)
    assert not submitted.is_set(), "producer should be blocked at capacity"

    await s.claim()  # free one slot -> producer unblocks
    await asyncio.wait_for(submitted.wait(), timeout=1)
    producer_task.cancel()


async def test_capacity_released_after_claims() -> None:
    s = Scheduler(maxsize=1)
    await s.submit(Task(payload={}))
    await s.claim()
    # Queue drained; capacity must be free again — this would block otherwise.
    await asyncio.wait_for(s.submit(Task(payload={})), timeout=1)
    assert len(s) == 1


@pytest.mark.parametrize("count", [50])
async def test_bulk_ordering_stress(count: int) -> None:
    s = Scheduler()
    priorities = [i % 7 for i in range(count)]
    for p in priorities:
        await s.submit(Task(payload={}, priority=p))

    claimed = [(await s.claim()).priority for _ in range(count)]
    assert claimed == sorted(priorities)
