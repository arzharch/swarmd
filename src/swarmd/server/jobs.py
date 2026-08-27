"""Long-running work, tracked uniformly.

A run, an eval sweep and a session are the same shape from the service's point
of view: submitted over HTTP, executed in the background for minutes to hours,
progress watched over the websocket, cancellable, and finishing with a report.
Modelling them three different ways would mean three status endpoints, three
cancel semantics, and a dashboard that has to special-case each.

WHY NOT HOLD THE HTTP CONNECTION. An eval sweep is ~4.5 hours (docs/CAPACITY.md).
No client, proxy or load balancer will hold that open, and an accepted-then-poll
shape is the only one that survives a rolling deploy of the thing in front of
it.

WHY THIS IS NOT THE SOURCE OF TRUTH. It is an in-process index so the dashboard
can list what is happening without a database round trip. Losing it on restart
costs the listing, not the work: the ledger and Postgres hold the record, and
`swarmd ledger report` reads it back without the service running at all.
"""

from __future__ import annotations

import asyncio
import builtins
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)

# Finished jobs retained in the index. Why 100: enough to review a working
# session's worth of history in the dashboard, bounded so a long-lived control
# plane does not accumulate reports forever.
MAX_FINISHED = 100


class JobKind(str, Enum):
    RUN = "run"
    EVAL = "eval"
    SESSION = "session"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Job:
    job_id: str
    kind: JobKind
    label: str
    params: dict[str, Any] = field(default_factory=dict)
    state: JobState = JobState.QUEUED
    submitted_ts: float = field(default_factory=time.time)
    started_ts: float | None = None
    finished_ts: float | None = None
    # Coarse progress so the dashboard can show a bar for work that takes
    # hours. Deliberately (done, total) rather than a percentage: a percentage
    # hides whether "50%" means 1 of 2 or 500 of 1000.
    done: int = 0
    total: int = 0
    report: dict[str, Any] | None = None
    error: str = ""
    task: asyncio.Task[Any] | None = None

    @property
    def duration_s(self) -> float:
        if self.started_ts is None:
            return 0.0
        return (self.finished_ts or time.time()) - self.started_ts

    @property
    def terminal(self) -> bool:
        return self.state in {
            JobState.COMPLETED, JobState.FAILED, JobState.CANCELLED
        }

    def to_dict(self, *, include_report: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "job_id": self.job_id,
            "kind": self.kind.value,
            "label": self.label,
            "params": self.params,
            "state": self.state.value,
            "submitted_ts": self.submitted_ts,
            "duration_s": round(self.duration_s, 2),
            "done": self.done,
            "total": self.total,
            "error": self.error,
        }
        if include_report:
            payload["report"] = self.report
        return payload


class JobRegistry:
    """Tracks background work for the dashboard."""

    def __init__(self, *, hub: Any = None, max_finished: int = MAX_FINISHED) -> None:
        self.hub = hub
        self.max_finished = max_finished
        self._jobs: dict[str, Job] = {}

    # -- lifecycle ----------------------------------------------------------

    def submit(
        self,
        kind: JobKind,
        label: str,
        coro_factory: Any,
        *,
        params: dict[str, Any] | None = None,
        total: int = 0,
        job_id: str | None = None,
    ) -> Job:
        """Start work in the background and return immediately.

        `coro_factory(job)` receives the Job so long work can report progress
        without the registry having to know anything about what it is doing.
        """
        job = Job(
            job_id=job_id or f"{kind.value}-{uuid.uuid4().hex[:10]}",
            kind=kind,
            label=label,
            params=params or {},
            total=total,
        )
        self._jobs[job.job_id] = job
        self._emit(job, "job_submitted")

        async def execute() -> None:
            job.state = JobState.RUNNING
            job.started_ts = time.time()
            self._emit(job, "job_started")
            try:
                job.report = await coro_factory(job)
                job.state = JobState.COMPLETED
            except asyncio.CancelledError:
                job.state = JobState.CANCELLED
                # Re-raised so the event loop sees a genuine cancellation
                # rather than a task that swallowed one and looks completed.
                raise
            except Exception as exc:
                job.state = JobState.FAILED
                job.error = f"{type(exc).__name__}: {exc}"
                logger.exception("job %s failed", job.job_id)
            finally:
                job.finished_ts = time.time()
                self._emit(job, "job_finished")
                self._prune()

        job.task = asyncio.create_task(execute())
        return job

    def cancel(self, job_id: str) -> Job:
        job = self.require(job_id)
        if job.task is not None and not job.task.done():
            job.task.cancel()
        job.state = JobState.CANCELLED
        job.finished_ts = time.time()
        self._emit(job, "job_finished")
        return job

    def progress(self, job: Job, done: int, total: int | None = None) -> None:
        job.done = done
        if total is not None:
            job.total = total
        self._emit(job, "job_progress")

    # -- access -------------------------------------------------------------

    def require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            raise KeyError(f"unknown job: {job_id}")
        return job

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, kind: JobKind | None = None) -> builtins.list[Job]:
        jobs = [j for j in self._jobs.values() if kind is None or j.kind == kind]
        return sorted(jobs, key=lambda j: j.submitted_ts, reverse=True)

    def active(self) -> builtins.list[Job]:
        return [j for j in self._jobs.values() if not j.terminal]

    # -- internals ----------------------------------------------------------

    def _emit(self, job: Job, kind: str) -> None:
        """Publish to the event hub. Never blocks or raises into the job.

        An observability path that can fail a job is a liability, so a broken
        hub costs a dashboard update and nothing else.
        """
        if self.hub is None:
            return
        try:
            self.hub.publish({"kind": kind, **job.to_dict(include_report=False)})
        except Exception:
            logger.debug("hub raised while publishing job event", exc_info=True)

    def _prune(self) -> None:
        finished = sorted(
            (j for j in self._jobs.values() if j.terminal),
            key=lambda j: j.finished_ts or 0.0,
        )
        for job in finished[: max(0, len(finished) - self.max_finished)]:
            del self._jobs[job.job_id]
