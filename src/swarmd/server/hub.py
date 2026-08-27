"""Event hub: fan a live run out to every watching dashboard.

THE RULE THIS MODULE EXISTS TO ENFORCE: a slow browser must never slow a run.

That sounds obvious and is easy to get wrong. The natural implementation is an
`asyncio.Queue` per subscriber with `await queue.put(...)`, which blocks the
producer when a queue fills. One viewer on hotel wifi then applies backpressure
all the way into the agent loop, and the run takes longer because someone
opened a tab. So every subscriber queue is bounded and DROPS on overflow, and
the drop is counted and reported rather than hidden -- SLO-4 has a 5% error
budget precisely to permit this.

The second rule: the hub holds the recent history so a dashboard that connects
mid-run sees the run so far rather than an empty screen and a slow trickle. A
run is 12-18 minutes; arriving at minute 10 to a blank page would make the
dashboard useless for exactly the case it is for.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Per-subscriber buffer. Why 256: several seconds of a busy run at the observed
# event rate, so an ordinary network hiccup loses nothing, while a genuinely
# stuck client is cut loose before it can consume meaningful memory. With
# hundreds of agents emitting, an unbounded queue on one dead client is a leak.
SUBSCRIBER_QUEUE = 256

# Replay buffer for late joiners. Why 500: enough to reconstruct the shape of a
# run in progress -- criterion, plan, the last few nodes -- without holding an
# entire run in memory for every idle hub.
HISTORY_LIMIT = 500


# eq=False so instances hash by identity. A mutable dataclass with the
# default eq is unhashable, and subscribers live in a set.
@dataclass(eq=False)
class Subscriber:
    queue: asyncio.Queue[dict[str, Any]]
    dropped: int = 0
    delivered: int = 0
    tags: set[str] = field(default_factory=set)


class EventHub:
    """One hub per process. Runs publish; websockets subscribe."""

    def __init__(
        self,
        *,
        queue_size: int = SUBSCRIBER_QUEUE,
        history_limit: int = HISTORY_LIMIT,
    ) -> None:
        self.queue_size = queue_size
        self._subscribers: set[Subscriber] = set()
        self._history: deque[dict[str, Any]] = deque(maxlen=history_limit)
        self._seq = 0
        self.total_published = 0
        self.total_dropped = 0

    # -- publishing ---------------------------------------------------------

    def publish(self, event: dict[str, Any]) -> None:
        """Non-blocking by construction. Called from the run's own loop.

        Deliberately synchronous: making this `async` would let a caller
        `await` it, and the first person to do so would reintroduce exactly the
        backpressure this design forbids.
        """
        self._seq += 1
        enriched = {**event, "seq": self._seq}
        self._history.append(enriched)
        self.total_published += 1

        for subscriber in list(self._subscribers):
            try:
                subscriber.queue.put_nowait(enriched)
                subscriber.delivered += 1
            except asyncio.QueueFull:
                # Drop the OLDEST, keep the newest. A dashboard showing stale
                # events while the run has moved on is worse than one with a
                # gap: the gap is visible, the staleness is not.
                with contextlib.suppress(asyncio.QueueEmpty):
                    subscriber.queue.get_nowait()
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait(enriched)
                subscriber.dropped += 1
                self.total_dropped += 1

    # -- subscribing --------------------------------------------------------

    def subscribe(self, *, replay: bool = True) -> Subscriber:
        subscriber = Subscriber(queue=asyncio.Queue(maxsize=self.queue_size))
        if replay:
            # Newest events matter most, so if history exceeds the queue the
            # tail is what survives.
            for event in list(self._history)[-self.queue_size :]:
                with contextlib.suppress(asyncio.QueueFull):
                    subscriber.queue.put_nowait(event)
        self._subscribers.add(subscriber)
        return subscriber

    def unsubscribe(self, subscriber: Subscriber) -> None:
        self._subscribers.discard(subscriber)

    # -- introspection ------------------------------------------------------

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    def history(self, limit: int = 200) -> list[dict[str, Any]]:
        return list(self._history)[-limit:]

    def stats(self) -> dict[str, Any]:
        """Feeds SLO-4 (dashboard freshness), which budgets for these drops."""
        return {
            "subscribers": len(self._subscribers),
            "published": self.total_published,
            "dropped": self.total_dropped,
            "drop_rate": (
                round(self.total_dropped / self.total_published, 4)
                if self.total_published else 0.0
            ),
            "history_size": len(self._history),
        }

    def reset(self) -> None:
        """Clear history between runs. Subscribers stay connected."""
        self._history.clear()
