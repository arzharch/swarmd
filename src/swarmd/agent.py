"""AgentHandle and lifecycle state machine.

Design notes:

- Explicit state machine over ad-hoc flags: illegal transitions raise immediately,
  which turns a whole class of "how did it end up in that state?" bugs into loud
  test failures.
- KILLED vs FAILED semantics: KILLED means an external force (chaos hook, operator)
  terminated the agent mid-work — its task is expected to be requeued. FAILED means
  the agent itself errored after exhausting retries. Downstream requeue logic keys
  off this distinction.
- The handle owns no business logic; it is identity + state + checkpoint pointer.
"""

from __future__ import annotations

import uuid
from enum import Enum

from swarmd.events import Event, EventBus, EventType
from swarmd.task import Checkpoint


class AgentState(str, Enum):
    SPAWNED = "SPAWNED"
    RUNNING = "RUNNING"
    PARKED = "PARKED"
    DONE = "DONE"
    FAILED = "FAILED"
    KILLED = "KILLED"


# Legal transitions: from -> set of allowed targets.
_TRANSITIONS: dict[AgentState, frozenset[AgentState]] = {
    AgentState.SPAWNED: frozenset({AgentState.RUNNING, AgentState.KILLED}),
    AgentState.RUNNING: frozenset(
        {AgentState.PARKED, AgentState.DONE, AgentState.FAILED, AgentState.KILLED}
    ),
    AgentState.PARKED: frozenset({AgentState.RUNNING, AgentState.KILLED}),
    # Terminal states have no outgoing edges.
    AgentState.DONE: frozenset(),
    AgentState.FAILED: frozenset(),
    AgentState.KILLED: frozenset(),
}


class IllegalTransitionError(RuntimeError):
    """Raised when a lifecycle transition is not permitted."""


class AgentHandle:
    """Identity + lifecycle state + latest checkpoint for one agent instance."""

    def __init__(self, agent_id: str | None = None) -> None:
        self.agent_id = agent_id or uuid.uuid4().hex[:10]
        self.state = AgentState.SPAWNED
        self.checkpoint: Checkpoint | None = None

    def transition(self, target: AgentState) -> AgentState:
        if target not in _TRANSITIONS[self.state]:
            raise IllegalTransitionError(
                f"agent {self.agent_id}: {self.state.value} -> {target.value} is illegal"
            )
        self.state = target
        return self.state

    @property
    def terminal(self) -> bool:
        return not _TRANSITIONS[self.state]

    def save_checkpoint(self, cp: Checkpoint) -> None:
        if cp.task_id == "":
            raise ValueError("checkpoint must reference a task")
        self.checkpoint = cp

    # Convenience methods that also emit lifecycle events when a bus is attached.

    def start(self, bus: EventBus | None = None) -> None:
        self.transition(AgentState.RUNNING)
        if bus:
            bus.emit(Event.now(EventType.AGENT_RUNNING, agent=self.agent_id))

    def park(self, bus: EventBus | None = None) -> None:
        self.transition(AgentState.PARKED)
        if bus:
            bus.emit(Event.now(EventType.AGENT_PARKED, agent=self.agent_id))

    def resume(self, bus: EventBus | None = None) -> None:
        self.transition(AgentState.RUNNING)
        if bus:
            bus.emit(Event.now(EventType.AGENT_RUNNING, agent=self.agent_id))

    def complete(self, bus: EventBus | None = None) -> None:
        self.transition(AgentState.DONE)
        if bus:
            bus.emit(Event.now(EventType.AGENT_DONE, agent=self.agent_id))

    def fail(self, error: str, bus: EventBus | None = None) -> None:
        self.transition(AgentState.FAILED)
        if bus:
            bus.emit(
                Event.now(EventType.AGENT_FAILED, agent=self.agent_id, error=error)
            )

    def kill(self, reason: str, bus: EventBus | None = None) -> None:
        self.transition(AgentState.KILLED)
        if bus:
            bus.emit(
                Event.now(EventType.AGENT_KILLED, agent=self.agent_id, reason=reason)
            )
