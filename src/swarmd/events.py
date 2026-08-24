"""Event bus — lifecycle events for tests and observability.

Phase 1.2. Design notes:

- Fan-out via asyncio.Queue per subscriber, not direct callbacks: a slow subscriber
  must never block or corrupt an agent's control flow (backpressure is contained to
  that subscriber's queue).
- Subscribers choose their own queue depth. When a subscriber's queue is full we DROP
  events for it (never block the emitter) and count the drops — observability data is
  best-effort by design; correctness data flows through checkpoints, not this bus.
- emit() is synchronous from the caller's perspective (enqueue only), so agents can
  emit from anywhere without awaiting.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventType(str, Enum):
    """Lifecycle and pipeline events emitted by the kernel."""

    AGENT_SPAWNED = "agent.spawned"
    AGENT_RUNNING = "agent.running"
    AGENT_PARKED = "agent.parked"
    AGENT_DONE = "agent.done"
    AGENT_FAILED = "agent.failed"
    AGENT_KILLED = "agent.killed"  # chaos-injected death
    TASK_CLAIMED = "task.claimed"
    TASK_COMPLETED = "task.completed"
    TASK_REQUEUED = "task.requeued"  # heartbeat expiry / failure requeue
    CHECKPOINT_SAVED = "checkpoint.saved"


@dataclass(frozen=True, slots=True)
class Event:
    """A single immutable event record."""

    type: EventType
    ts: float
    payload: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def now(event_type: EventType, **payload: Any) -> Event:
        return Event(type=event_type, ts=time.monotonic(), payload=payload)

_CLOSE = Event(type=EventType.AGENT_DONE, ts=-1.0)  # internal stream terminator

class EventBus:
    """Async fan-out event bus with bounded per-subscriber queues."""

    def __init__(self) -> None:
        self._subscribers: dict[int, asyncio.Queue[Event]] = {}
        self._next_id = 0
        self.dropped: int = 0  # total events dropped due to full subscriber queues

    def subscribe(self, maxsize: int = 1024) -> tuple[int, asyncio.Queue[Event]]:
        """Register a subscriber; returns (id, queue) to consume from."""
        qid = self._next_id
        self._next_id += 1
        self._subscribers[qid] = asyncio.Queue(maxsize=maxsize)
        return qid, self._subscribers[qid]

    def unsubscribe(self, qid: int) -> None:
        q = self._subscribers.pop(qid, None)
        if q is not None:
            # Wake any stream() consumer blocked on an empty queue.
            try:
                q.put_nowait(_CLOSE)
            except asyncio.QueueFull:
                pass  # consumer will drain remaining events and see the removal check

    def emit(self, event: Event) -> None:
        """Fan out to all subscribers. Never blocks; drops on full queues."""
        for q in list(self._subscribers.values()):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                self.dropped += 1

    async def stream(self, qid: int) -> AsyncIterator[Event]:
        """Consume events from a subscribed queue until it is unsubscribed."""
        q = self._subscribers.get(qid)
        if q is None:
            return
        while True:
            event = await q.get()
            if event is _CLOSE:
                return
            yield event
