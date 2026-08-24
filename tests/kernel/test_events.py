"""Tests for the event bus (Phase 1.2)."""

import asyncio

from swarmd.events import Event, EventBus, EventType


async def test_fan_out_to_all_subscribers() -> None:
    bus = EventBus()
    q1_id, q1 = bus.subscribe()
    q2_id, q2 = bus.subscribe()

    bus.emit(Event.now(EventType.AGENT_SPAWNED, agent="a1"))

    e1 = await asyncio.wait_for(q1.get(), timeout=1)
    e2 = await asyncio.wait_for(q2.get(), timeout=1)
    assert e1.type is EventType.AGENT_SPAWNED
    assert e2.type is EventType.AGENT_SPAWNED
    assert e1.payload["agent"] == "a1"

    bus.unsubscribe(q1_id)
    bus.unsubscribe(q2_id)


async def test_full_queue_drops_and_counts_never_blocks() -> None:
    bus = EventBus()
    _, q = bus.subscribe(maxsize=1)

    # First fits, second overflows the depth-1 queue.
    bus.emit(Event.now(EventType.TASK_CLAIMED, task=1))
    bus.emit(Event.now(EventType.TASK_CLAIMED, task=2))

    assert bus.dropped == 1
    first = await asyncio.wait_for(q.get(), timeout=1)
    assert first.payload["task"] == 1


async def test_unsubscribe_stops_delivery() -> None:
    bus = EventBus()
    qid, _q = bus.subscribe()
    bus.unsubscribe(qid)

    bus.emit(Event.now(EventType.AGENT_DONE))
    assert bus.dropped == 0  # no subscriber left, nothing to drop


async def test_stream_yields_until_unsubscribed() -> None:
    bus = EventBus()
    qid, _ = bus.subscribe()

    async def consume() -> list[Event]:
        return [e async for e in bus.stream(qid)]

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0)  # let consumer start waiting on the queue

    bus.emit(Event.now(EventType.CHECKPOINT_SAVED, step=1))
    bus.emit(Event.now(EventType.CHECKPOINT_SAVED, step=2))

    await asyncio.sleep(0)
    bus.unsubscribe(qid)
    events = await asyncio.wait_for(consumer, timeout=1)

    assert [e.payload["step"] for e in events] == [1, 2]
