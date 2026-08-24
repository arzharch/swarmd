"""Tests for Task/Checkpoint models (Phase 1.3)."""

import pytest

from swarmd.task import CHECKPOINT_SCHEMA_VERSION, Checkpoint, Task, TaskResult


def test_task_round_trip() -> None:
    t = Task(payload={"lead": "acme"}, priority=3, max_retries=4, retries_left=4)
    restored = Task.from_dict(t.to_dict())

    assert restored.task_id == t.task_id
    assert restored.payload == {"lead": "acme"}
    assert restored.priority == 3
    assert restored.retries_left == 4


def test_task_retries_capped_at_max() -> None:
    t = Task(payload={}, max_retries=2, retries_left=9)
    assert t.retries_left == 2


def test_checkpoint_step_advancement_is_pure() -> None:
    cp = Checkpoint(task_id="t1", agent_id="a1")
    cp2 = cp.with_step("fetch", {"raw": "x"})
    cp3 = cp2.with_step("normalize", {"clean": "y"})

    # Original untouched; chain accumulates in order.
    assert cp.completed_steps == []
    assert cp2.completed_steps == ["fetch"]
    assert cp3.completed_steps == ["fetch", "normalize"]
    assert cp3.data["normalize"] == {"clean": "y"}


def test_checkpoint_round_trip_preserves_order_and_data() -> None:
    cp = (
        Checkpoint(task_id="t1", agent_id="a1")
        .with_step("s1", 1)
        .with_step("s2", {"k": [1, 2]})
    )
    restored = Checkpoint.from_dict(cp.to_dict())

    assert restored.completed_steps == ["s1", "s2"]
    assert restored.data == {"s1": 1, "s2": {"k": [1, 2]}}
    assert restored.schema_version == CHECKPOINT_SCHEMA_VERSION


def test_checkpoint_rejects_unknown_schema_version() -> None:
    bad = Checkpoint(task_id="t", agent_id="a").to_dict()
    bad["schema_version"] = 999

    with pytest.raises(ValueError, match="schema mismatch"):
        Checkpoint.from_dict(bad)


def test_task_result_defaults_ok_false_path() -> None:
    r = TaskResult(task_id="t1", ok=False, error="boom")
    assert not r.ok
    assert r.error == "boom"
