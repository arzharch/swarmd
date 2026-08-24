"""Tests for AgentHandle lifecycle (agent.py)."""

import pytest

from swarmd.agent import AgentHandle, AgentState, IllegalTransitionError
from swarmd.events import EventBus, EventType
from swarmd.task import Checkpoint


def test_happy_path_lifecycle() -> None:
    a = AgentHandle()
    assert a.state is AgentState.SPAWNED

    a.start()
    a.park()
    a.resume()
    a.complete()
    assert a.state is AgentState.DONE
    assert a.terminal


def test_illegal_transitions_raise() -> None:
    a = AgentHandle()
    with pytest.raises(IllegalTransitionError):
        a.complete()  # SPAWNED -> DONE not allowed

    b = AgentHandle()
    b.start()
    b.complete()
    with pytest.raises(IllegalTransitionError):
        b.start()  # terminal states have no outgoing edges


def test_kill_from_any_active_state() -> None:
    for setup in ("spawned", "running", "parked"):
        a = AgentHandle()
        if setup == "running":
            a.start()
        elif setup == "parked":
            a.start()
            a.park()
        a.kill(reason="chaos")
        assert a.state is AgentState.KILLED
        assert a.terminal


def test_checkpoint_attachment() -> None:
    a = AgentHandle()
    cp = Checkpoint(task_id="t1", agent_id=a.agent_id).with_step("s1", 42)
    a.save_checkpoint(cp)
    assert a.checkpoint is not None
    assert a.checkpoint.completed_steps == ["s1"]

    with pytest.raises(ValueError):
        a.save_checkpoint(Checkpoint(task_id="", agent_id=a.agent_id))


async def test_lifecycle_emits_events() -> None:
    bus = EventBus()
    qid, q = bus.subscribe(maxsize=16)
    a = AgentHandle()

    a.start(bus)
    a.kill("chaos", bus)

    types = []
    while not q.empty():
        types.append(q.get_nowait().type)
    bus.unsubscribe(qid)

    assert types == [EventType.AGENT_RUNNING, EventType.AGENT_KILLED]
