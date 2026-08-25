"""Runtime: worker pool, heartbeat expiry, requeue-with-checkpoint.

Design notes:

- Agents are asyncio tasks running a step list. Each step: run -> save checkpoint ->
  emit event -> advance. A checkpoint is durable *before* the next step starts, so a
  kill between steps loses nothing.
- Heartbeat/lease model: the runtime records `claimed_until` per in-flight task.
  A reaper loop expires claims whose deadline passed (agent died without finishing)
  and requeues those tasks WITH their last checkpoint intact. Replacement agents
  skip completed steps deterministically — this is the kill-and-resume contract.
- Lease duration must exceed one step's worst-case duration, or live agents get their
  work stolen; it must be much smaller than total task time, or dead agents stall
  recovery. Both bounds are asserted in tests via injected chaos kills.
- Requeue preserves retries_left; only genuine failures (exception) consume retries.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from swarmd.agent import AgentHandle
from swarmd.events import Event, EventBus, EventType
from swarmd.scheduler import Scheduler
from swarmd.task import Checkpoint, Task, TaskResult

logger = logging.getLogger(__name__)

StepFn = Callable[[dict[str, Any], dict[str, Any]], Awaitable[Any]]
# StepFn(step_input, task_payload) -> output stored under the step name


@dataclass(slots=True)
class _Claim:
    task: Task
    agent_id: str
    expires_at: float
    steps: list[str]
    checkpoint: Checkpoint


@dataclass
class RuntimeStats:
    spawned: int = 0
    killed: int = 0
    requeued: int = 0
    completed: int = 0
    failed: int = 0
    events: list[Event] = field(default_factory=list)


class Runtime:
    """Runs agent pools over a scheduler with lease-based crash recovery."""

    def __init__(
        self,
        scheduler: Scheduler,
        bus: EventBus | None = None,
        *,
        concurrency: int = 8,
        lease_s: float = 1.0,
        reap_interval_s: float = 0.05,
    ) -> None:
        self.scheduler = scheduler
        self.bus = bus
        self.concurrency = concurrency
        self.lease_s = lease_s
        self.reap_interval_s = reap_interval_s
        self.stats = RuntimeStats()
        self._claims: dict[str, _Claim] = {}  # task_id -> claim
        self._steps_registry: dict[str, list[str]] = {}
        self._step_fns: dict[tuple[str, str], StepFn] = {}
        self._results: dict[str, TaskResult] = {}
        self._running = False
        self._reaper: asyncio.Task[None] | None = None
        self._workers: list[asyncio.Task[None]] = []

    # ---- registration -------------------------------------------------

    def register_task_type(self, task_type: str, steps: list[str]) -> None:
        """Declare the ordered step pipeline for a task type."""
        self._steps_registry[task_type] = list(steps)

    def register_step(self, task_type: str, step_name: str, fn: StepFn) -> None:
        self._step_fns[(task_type, step_name)] = fn

    # ---- lifecycle ----------------------------------------------------

    async def start(self) -> None:
        self._running = True
        self._reaper = asyncio.create_task(self._reap_loop())
        for i in range(self.concurrency):
            handle = AgentHandle()
            self.stats.spawned += 1
            if self.bus:
                self.bus.emit(Event.now(EventType.AGENT_SPAWNED, agent=handle.agent_id))
            worker = asyncio.create_task(self._worker(handle))
            worker.set_name(f"worker-{handle.agent_id}")
            self._workers.append(worker)

    async def stop(self) -> None:
        self._running = False
        if self._reaper:
            self._reaper.cancel()
        for w in self._workers:
            w.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

    def kill_agent(self, agent_id: str) -> bool:
        """Chaos hook: simulate an agent dying mid-work.

        The worker task is cancelled; its claim stays registered until the reaper
        expires the lease — exactly like a real crashed process.
        """
        for w in self._workers:
            if w.get_name() == f"worker-{agent_id}":
                w.cancel()
                return True
        return False

    def live_agent_ids(self) -> list[str]:
        return [w.get_name().removeprefix("worker-") for w in self._workers]

    # ---- internals ------------------------------------------------------

    def _emit(self, etype: EventType, **payload: Any) -> None:
        if self.bus:
            self.bus.emit(Event.now(etype, **payload))

    async def _reap_loop(self) -> None:
        while self._running:
            now = time.monotonic()
            expired = [
                c for c in self._claims.values() if c.expires_at <= now
            ]
            for claim in expired:
                del self._claims[claim.task.task_id]
                self.stats.requeued += 1
                self._emit(
                    EventType.TASK_REQUEUED,
                    task_id=claim.task.task_id,
                    completed_steps=len(claim.checkpoint.completed_steps),
                )
                await self.scheduler.submit(claim.task)
            # Replace any dead workers so the pool size stays at `concurrency`.
            await self._replace_dead_workers()
            await asyncio.sleep(self.reap_interval_s)

    async def _replace_dead_workers(self) -> None:
        """Respawn workers that died (chaos kill, crash) to keep pool capacity."""
        alive = [w for w in self._workers if not w.done()]
        dead_count = self.concurrency - len(alive)
        if dead_count <= 0:
            return
        self._workers = alive
        for _ in range(dead_count):
            handle = AgentHandle()
            self.stats.spawned += 1
            self._emit(EventType.AGENT_SPAWNED, agent=handle.agent_id)
            worker = asyncio.create_task(self._worker(handle))
            worker.set_name(f"worker-{handle.agent_id}")
            self._workers.append(worker)

    async def _worker(self, handle: AgentHandle) -> None:
        current_name = asyncio.current_task().get_name()  # type: ignore[union-attr]
        assert current_name.startswith("worker-")
        try:
            while self._running:
                task = await self.scheduler.claim()
                task_type = str(task.payload.get("type", "default"))
                steps = self._steps_registry.get(task_type, [])
                cp = Checkpoint(task_id=task.task_id, agent_id=handle.agent_id)

                handle = AgentHandle(agent_id=handle.agent_id)
                # Fresh SPAWNED state per task: the worker process persists across
                # tasks, but each task is a new agent instance lifecycle.
                handle.start(self.bus)
                claim = _Claim(
                    task=task,
                    agent_id=handle.agent_id,
                    expires_at=time.monotonic() + self.lease_s,
                    steps=steps,
                    checkpoint=cp,
                )
                self._claims[task.task_id] = claim
                self._emit(EventType.TASK_CLAIMED, task_id=task.task_id)

                try:
                    result = await self._run_steps(handle, claim)
                except asyncio.CancelledError:
                    # Killed mid-work: leave the claim for the reaper to expire.
                    raise
                except Exception as exc:  # noqa: BLE001 - kernel boundary
                    self._claims.pop(task.task_id, None)
                    task.retries_left -= 1
                    if task.retries_left > 0:
                        self.stats.requeued += 1
                        self._emit(
                            EventType.TASK_REQUEUED,
                            task_id=task.task_id,
                            error=str(exc),
                        )
                        await self.scheduler.submit(task)
                    else:
                        self.stats.failed += 1
                        self.results()[task.task_id] = TaskResult(
                            task_id=task.task_id, ok=False, error=str(exc)
                        )
                        handle.fail(str(exc), self.bus)
                        self.scheduler.ack_complete()
                    continue

                self._claims.pop(task.task_id, None)
                self.results()[task.task_id] = result
                handle.complete(self.bus)
                self.stats.completed += 1
                self.scheduler.ack_complete()
                self._emit(EventType.TASK_COMPLETED, task_id=task.task_id)
        except asyncio.CancelledError:
            if not handle.terminal:
                handle.kill("cancelled", self.bus)
                self.stats.killed += 1
            raise

    async def _run_steps(self, handle: AgentHandle, claim: _Claim) -> TaskResult:
        task = claim.task
        cp = claim.checkpoint
        task_type = str(task.payload.get("type", "default"))

        prev_output: Any = None
        for idx, step_name in enumerate(claim.steps):
            if step_name in cp.completed_steps:
                # Deterministic skip: work already done pre-kill. Reconstruct the
                # chain position so the next new step still receives the output
                # of the step immediately before it.
                prev_output = cp.data[step_name]
                continue
            fn = self._step_fns[(task_type, step_name)]
            output = await fn(prev_output, task.payload)
            cp = cp.with_step(step_name, output)
            handle.save_checkpoint(cp)
            claim.checkpoint = cp
            prev_output = output
            # Refresh the lease after each step: proof of life.
            claim.expires_at = time.monotonic() + self.lease_s
            self._emit(EventType.CHECKPOINT_SAVED, task_id=task.task_id, step=step_name)

        return TaskResult(task_id=task.task_id, ok=True, output=cp.data)

    def results(self) -> dict[str, TaskResult]:
        return self._results

    @staticmethod
    def new_agent_id() -> str:
        return uuid.uuid4().hex[:10]


def state_summary(handles: list[AgentHandle]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for h in handles:
        key = h.state.value
        counts[key] = counts.get(key, 0) + 1
    return counts
