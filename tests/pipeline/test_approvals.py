"""Tests for durable HITL approvals: state machine, restart survival, audit."""

import pytest

from swarmd.hitl.approvals import (
    ApprovalManager,
    ApprovalState,
    InMemoryApprovalStore,
)


async def test_submit_and_approve_flow() -> None:
    store = InMemoryApprovalStore()
    mgr = ApprovalManager(store)

    req = await mgr.submit({"lead": "acme", "draft": "hello"}, stage="review")
    assert req.state is ApprovalState.PENDING
    assert len(await mgr.pending()) == 1

    decided = await mgr.decide(req.request_id, "approve", actor="arsh")
    assert decided.state is ApprovalState.APPROVED
    assert len(await mgr.pending()) == 0


async def test_reject_and_edit_paths() -> None:
    store = InMemoryApprovalStore()
    mgr = ApprovalManager(store)

    r1 = await mgr.submit({"a": 1}, stage="review")
    assert (await mgr.decide(r1.request_id, "reject", "q")).state is ApprovalState.REJECTED

    r2 = await mgr.submit({"a": 1}, stage="review")
    edited = await mgr.decide(r2.request_id, "edit", "q", edited_item={"a": 2})
    assert edited.state is ApprovalState.EDITED
    assert edited.item == {"a": 2}


async def test_double_decision_is_rejected() -> None:
    store = InMemoryApprovalStore()
    mgr = ApprovalManager(store)
    req = await mgr.submit({}, stage="r")
    await mgr.decide(req.request_id, "approve", "a")

    with pytest.raises(ValueError, match="already decided"):
        await mgr.decide(req.request_id, "reject", "b")


async def test_unknown_action_and_request() -> None:
    mgr = ApprovalManager(InMemoryApprovalStore())
    with pytest.raises(KeyError):
        await mgr.decide("ghost", "approve", "a")
    req = await mgr.submit({}, stage="r")
    with pytest.raises(ValueError, match="unknown action"):
        await mgr.decide(req.request_id, "maybe", "a")


async def test_state_survives_store_restart() -> None:
    """The durability contract: kill everything, keep the store, resume."""
    store = InMemoryApprovalStore()
    mgr1 = ApprovalManager(store)
    req = await mgr1.submit({"lead": "acme"}, stage="review")

    # Simulate process restart: new manager instance over the same persisted store.
    mgr2 = ApprovalManager(store)
    pending = await mgr2.pending()
    assert len(pending) == 1 and pending[0].request_id == req.request_id

    decided = await mgr2.decide(req.request_id, "approve", "post-restart-human")
    assert decided.state is ApprovalState.APPROVED


async def test_audit_trail_is_append_only_and_complete() -> None:
    store = InMemoryApprovalStore()
    mgr = ApprovalManager(store)

    req = await mgr.submit({"x": 1}, stage="review")
    await mgr.decide(req.request_id, "edit", "alice", edited_item={"x": 2})

    trail = await mgr.audit()
    actions = [e.action for e in trail]
    assert actions == ["submit", "edit"]
    assert trail[1].actor == "alice"
