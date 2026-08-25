"""Scheduler: priority ordering, bounded queues, backpressure.

Design notes:

- Single min-heap keyed by (priority, seq): lower priority value runs sooner; `seq`
  preserves FIFO order within the same priority so equal-priority tasks never
  starve each other. A heapq over tuples is auditable stdlib code — no framework.
- Backpressure policy: put() BLOCKS when the queue is full (asyncio-friendly),
  rather than dropping or spilling to disk. Dropping loses work; blocking applies
  natural backpressure to producers, which is what a bounded pipeline wants.
  The bound exists to cap memory, not to reject work.
- claim() hands out work atomically (single await point) — no double-claim within
  one event loop.
"""

from __future__ import annotations

import asyncio
import heapq
import itertools
from dataclasses import dataclass, field

from swarmd.task import Task


@dataclass(order=True)
class _Entry:
    priority: int
    seq: int
    task: Task = field(compare=False)


class Scheduler:
    """Priority task queue with a concurrency cap and atomic claims."""

    def __init__(self, maxsize: int = 256) -> None:
        self._heap: list[_Entry] = []
        self._seq = itertools.count()
        self._not_empty = asyncio.Event()
        self._capacity = asyncio.Semaphore(maxsize)
        self.completed: int = 0

    async def submit(self, task: Task) -> None:
        """Enqueue a task; blocks while the queue is at capacity."""
        await self._capacity.acquire()
        heapq.heappush(
            self._heap,
            _Entry(priority=task.priority, seq=next(self._seq), task=task),
        )
        self._not_empty.set()

    async def claim(self) -> Task:
        """Atomically take the highest-priority task; waits if empty.

        The event may be consumed by a competing worker between wake and pop,
        so re-check emptiness after waking (standard condition-variable pattern).
        """
        while True:
            await self._not_empty.wait()
            if not self._heap:
                self._not_empty.clear()
                continue
            entry = heapq.heappop(self._heap)
            self._capacity.release()
            if not self._heap:
                self._not_empty.clear()
            return entry.task

    def ack_complete(self) -> None:
        """Record a finished task (bookkeeping for metrics/tests)."""
        self.completed += 1

    def __len__(self) -> int:
        return len(self._heap)
