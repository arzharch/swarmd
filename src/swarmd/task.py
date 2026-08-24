"""Task, TaskResult, and Checkpoint models.

Phase 1.3. Design notes:

- Plain dataclasses with hand-rolled to/from-dict serialization instead of pydantic:
  the kernel must stay dependency-light and auditable (ADR-004 spirit), and checkpoint
  payloads are small structured dicts where a validation library buys nothing.
- Checkpoints carry a schema_version from day one: once runs persist across process
  restarts (Phase 3), old checkpoints on disk must remain loadable or explicitly
  rejected — silent shape drift is how recovery bugs hide.
- `completed_steps` is an ordered list of step names, not a set: resume replays
  deterministically by skipping exactly these steps in order.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any

CHECKPOINT_SCHEMA_VERSION = 1


@dataclass(slots=True)
class Task:
    """A unit of work claimed by an agent.

    priority: lower value = scheduled sooner (heapq min-heap semantics).
    retries_left: decremented on failure; at zero the task dead-letters.
    """

    payload: dict[str, Any]
    task_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    priority: int = 0
    deadline_s: float | None = None
    max_retries: int = 2
    retries_left: int = 2
    created_ts: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.retries_left = min(self.retries_left, self.max_retries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "payload": self.payload,
            "priority": self.priority,
            "deadline_s": self.deadline_s,
            "max_retries": self.max_retries,
            "retries_left": self.retries_left,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Task:
        return cls(
            task_id=data["task_id"],
            payload=data["payload"],
            priority=data["priority"],
            deadline_s=data["deadline_s"],
            max_retries=data["max_retries"],
            retries_left=data["retries_left"],
        )


@dataclass(slots=True)
class Checkpoint:
    """Agent progress snapshot saved at every step boundary.

    completed_steps: ordered list of finished step names; resume skips these.
    data: opaque step outputs keyed by step name — enough for the next step to run
    without redoing prior ones.
    """

    task_id: str
    agent_id: str
    completed_steps: list[str] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    schema_version: int = CHECKPOINT_SCHEMA_VERSION
    saved_ts: float = field(default_factory=time.monotonic)

    def with_step(self, step_name: str, output: Any) -> Checkpoint:
        """Return an advanced copy — the caller decides when to persist it."""
        return Checkpoint(
            task_id=self.task_id,
            agent_id=self.agent_id,
            completed_steps=[*self.completed_steps, step_name],
            data={**self.data, step_name: output},
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "task_id": self.task_id,
            "agent_id": self.agent_id,
            "completed_steps": list(self.completed_steps),
            "data": self.data,
            "saved_ts": self.saved_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Checkpoint:
        version = data.get("schema_version", -1)
        if version != CHECKPOINT_SCHEMA_VERSION:
            raise ValueError(
                f"checkpoint schema mismatch: have {version}, "
                f"expect {CHECKPOINT_SCHEMA_VERSION}"
            )
        return cls(
            task_id=data["task_id"],
            agent_id=data["agent_id"],
            completed_steps=list(data["completed_steps"]),
            data=data["data"],
            saved_ts=data.get("saved_ts", 0.0),
        )


@dataclass(slots=True)
class TaskResult:
    """Outcome of a fully-completed task."""

    task_id: str
    ok: bool
    output: Any = None
    error: str | None = None
    finished_ts: float = field(default_factory=time.monotonic)
