"""Durability tests for the HITL approval store.

This is the file that makes Phase 3's gate real. The gate says: reach the review
queue, kill the process, restart, approve via CLI. Previously the CLI built a
fresh in-memory store per invocation, so the gate passed on paper while
approvals vanished between processes.

The tests below simulate the process boundary by constructing an entirely new
store object over the same file -- which is exactly what a second `swarmd`
invocation does.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from swarmd.hitl.approvals import ApprovalManager, ApprovalState
from swarmd.hitl.stores import (
    PostgresApprovalStore,
    SqliteApprovalStore,
    build_approval_store,
)


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "approvals.db"


# --- the gate --------------------------------------------------------------


async def test_approval_survives_a_process_boundary(db_path):
    """THE Phase 3 gate: queue in one process, decide in another."""
    # Process A: a run reaches the review queue and dies.
    mgr_a = ApprovalManager(SqliteApprovalStore(db_path))
    req = await mgr_a.submit({"draft": "hello"}, stage="review")
    del mgr_a

    # Process B: a fresh CLI invocation, nothing shared but the file.
    mgr_b = ApprovalManager(SqliteApprovalStore(db_path))
    pending = await mgr_b.pending()
    assert [r.request_id for r in pending] == [req.request_id]

    await mgr_b.decide(req.request_id, "approve", actor="cli-user")
    del mgr_b

    # Process C: the decision is still there.
    mgr_c = ApprovalManager(SqliteApprovalStore(db_path))
    reloaded = await mgr_c.store.get_request(req.request_id)
    assert reloaded is not None
    assert reloaded.state is ApprovalState.APPROVED
    assert await mgr_c.pending() == []


async def test_audit_trail_survives_a_process_boundary(db_path):
    mgr_a = ApprovalManager(SqliteApprovalStore(db_path))
    req = await mgr_a.submit({"draft": "x"}, stage="review")
    await mgr_a.decide(req.request_id, "reject", actor="reviewer-1")

    audit = await ApprovalManager(SqliteApprovalStore(db_path)).audit()
    assert [(e.action, e.actor) for e in audit] == [
        ("submit", "system"),
        ("reject", "reviewer-1"),
    ]


async def test_the_cli_actually_uses_a_durable_store(db_path, monkeypatch):
    """Regression guard for the original bug.

    The state machine was always correct; the defect was the CLI wiring an
    in-memory store. A test of ApprovalManager alone would not have caught it.
    """
    monkeypatch.setenv("SWARMD_APPROVALS_DB", str(db_path))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    # The CLI loads .env, which carries a DATABASE_URL. Deleting the variable
    # says "unset"; without this the file would put it back and the subprocess
    # would look at Postgres instead of the SQLite file under test.
    monkeypatch.setenv("SWARMD_NO_DOTENV", "1")

    mgr = ApprovalManager(build_approval_store())
    req = await mgr.submit({"draft": "from a run"}, stage="review")

    # A genuinely separate OS process, not just a new object. Run through
    # the loop's executor so the blocking wait does not stall the event loop.
    def _list() -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "swarmd.cli", "list"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    result = await asyncio.get_running_loop().run_in_executor(None, _list)
    assert result.returncode == 0, result.stderr
    assert req.request_id in result.stdout, (
        f"queued approval invisible to a second process.\n{result.stdout}"
    )


async def test_decisions_are_immutable_across_processes(db_path):
    """A decided request must not be re-decided by a later invocation."""
    mgr_a = ApprovalManager(SqliteApprovalStore(db_path))
    req = await mgr_a.submit({"draft": "x"}, stage="review")
    await mgr_a.decide(req.request_id, "approve", actor="a")

    mgr_b = ApprovalManager(SqliteApprovalStore(db_path))
    with pytest.raises(ValueError, match="already decided"):
        await mgr_b.decide(req.request_id, "reject", actor="b")


async def test_edited_items_persist(db_path):
    mgr_a = ApprovalManager(SqliteApprovalStore(db_path))
    req = await mgr_a.submit({"draft": "original"}, stage="review")
    await mgr_a.decide(
        req.request_id, "edit", actor="editor", edited_item={"draft": "corrected"}
    )

    reloaded = await SqliteApprovalStore(db_path).get_request(req.request_id)
    assert reloaded is not None
    assert reloaded.item == {"draft": "corrected"}
    assert reloaded.state is ApprovalState.EDITED


# --- store behaviour -------------------------------------------------------


async def test_pending_is_ordered_oldest_first(db_path):
    """A review queue that reorders itself is a queue nobody can work through."""
    store = SqliteApprovalStore(db_path)
    mgr = ApprovalManager(store)
    first = await mgr.submit({"n": 1}, stage="review")
    second = await mgr.submit({"n": 2}, stage="review")
    third = await mgr.submit({"n": 3}, stage="review")

    pending = await mgr.pending()
    assert [r.request_id for r in pending] == [
        first.request_id, second.request_id, third.request_id
    ]


async def test_decided_requests_leave_the_pending_queue(db_path):
    mgr = ApprovalManager(SqliteApprovalStore(db_path))
    a = await mgr.submit({"n": 1}, stage="review")
    await mgr.submit({"n": 2}, stage="review")
    await mgr.decide(a.request_id, "approve", actor="x")

    assert [r.item["n"] for r in await mgr.pending()] == [2]


async def test_nested_item_payloads_round_trip(db_path):
    """Items are JSON documents, not flat strings."""
    store = SqliteApprovalStore(db_path)
    item = {"draft": {"subject": "hi", "tags": ["a", "b"]}, "score": 7, "ok": True}
    mgr = ApprovalManager(store)
    req = await mgr.submit(item, stage="review")

    reloaded = await SqliteApprovalStore(db_path).get_request(req.request_id)
    assert reloaded is not None
    assert reloaded.item == item


async def test_unknown_request_raises_rather_than_silently_passing(db_path):
    mgr = ApprovalManager(SqliteApprovalStore(db_path))
    with pytest.raises(KeyError):
        await mgr.decide("does-not-exist", "approve", actor="x")


async def test_wal_mode_lets_a_reader_and_writer_coexist(db_path):
    """Without WAL, `swarmd list` during a live run hits 'database is locked'."""
    writer = SqliteApprovalStore(db_path)
    reader = SqliteApprovalStore(db_path)
    mgr = ApprovalManager(writer)

    for i in range(5):
        await mgr.submit({"n": i}, stage="review")
        assert len(await reader.list_pending()) == i + 1


async def test_audit_is_append_only_and_ordered(db_path):
    store = SqliteApprovalStore(db_path)
    mgr = ApprovalManager(store)
    req = await mgr.submit({"n": 1}, stage="review")
    await mgr.decide(req.request_id, "approve", actor="a")

    audit = await store.read_audit()
    assert [e.action for e in audit] == ["submit", "approve"]
    # Ordering comes from the autoincrement sequence, not from timestamps,
    # which can collide or move backwards under NTP correction.
    assert audit[0].ts <= audit[1].ts


# --- backend selection -----------------------------------------------------


def test_build_store_defaults_to_sqlite_not_in_memory(monkeypatch, tmp_path):
    """Defaulting to in-memory is precisely the bug this module fixes."""
    monkeypatch.delenv("DATABASE_URL", raising=False)
    store = build_approval_store(path=tmp_path / "a.db")
    assert isinstance(store, SqliteApprovalStore)


def test_build_store_prefers_postgres_when_configured(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgres://user:pw@localhost:5434/swarmd")
    store = build_approval_store()
    assert isinstance(store, PostgresApprovalStore)


def test_postgres_store_construction_never_touches_the_network(monkeypatch):
    """`swarmd --help` must not require a reachable database."""
    monkeypatch.setenv("DATABASE_URL", "postgres://nobody@10.255.255.1:5432/nope")
    store = PostgresApprovalStore()   # must not hang or raise
    assert store._pool is None


def test_postgres_store_refuses_construction_without_a_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="DATABASE_URL"):
        PostgresApprovalStore()
