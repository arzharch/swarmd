"""Postgres approval store, against a real database.

These tests exist because the Postgres path had never once executed. `asyncpg`
was referenced throughout the module and in the mypy overrides but was not a
declared dependency, so every import of it would have failed at runtime -- and
nothing noticed, because every test used SQLite or the in-memory store.

That is the failure mode a test suite is supposed to prevent: a code path that
looks finished, type-checks, is documented, and has never run.

Skipped unless a database is reachable, so CI stays hermetic. `docker compose
up -d postgres` makes them run.
"""

from __future__ import annotations

import os

import pytest

from swarmd.hitl.approvals import ApprovalManager, ApprovalState
from swarmd.hitl.skill_gate import SkillGate
from swarmd.hitl.stores import PostgresApprovalStore
from swarmd.swarm.skills import SkillLibrary

DATABASE_URL = os.environ.get(
    "SWARMD_TEST_DATABASE_URL",
    "postgres://swarmd:swarmd_dev@localhost:5435/swarmd",
)

asyncpg = pytest.importorskip("asyncpg", reason="postgres extra not installed")


async def _reachable() -> bool:
    try:
        conn = await asyncpg.connect(DATABASE_URL, timeout=3)
    except Exception:  # noqa: BLE001 - any failure to connect means "skip"
        return False
    await conn.close()
    return True


@pytest.fixture
async def store():
    if not await _reachable():
        pytest.skip("no Postgres at DATABASE_URL; `docker compose up -d postgres`")
    store = PostgresApprovalStore(DATABASE_URL)
    yield store
    # Leave no residue: these tests share a database with local development.
    pool = await store._get_pool()
    async with pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM approval_audit WHERE actor LIKE 'pgtest-%'"
        )
        await conn.execute(
            "DELETE FROM approval_requests WHERE stage = 'pgtest'"
        )
    await store.aclose()


# --- the path that had never run -------------------------------------------


async def test_a_request_survives_a_new_connection(store):
    """What a second process, or a second replica, actually sees."""
    manager = ApprovalManager(store)
    request = await manager.submit({"draft": "x"}, stage="pgtest")

    other = PostgresApprovalStore(DATABASE_URL)
    try:
        reloaded = await other.get_request(request.request_id)
        assert reloaded is not None
        assert reloaded.item == {"draft": "x"}
    finally:
        await other.aclose()


async def test_two_replicas_see_one_queue(store):
    """The reason Postgres exists at all: SQLite cannot do this safely."""
    a = ApprovalManager(store)
    other = PostgresApprovalStore(DATABASE_URL)
    try:
        b = ApprovalManager(other)
        request = await a.submit({"n": 1}, stage="pgtest")
        pending = [r.request_id for r in await b.pending() if r.stage == "pgtest"]
        assert request.request_id in pending

        await b.decide(request.request_id, "approve", actor="pgtest-b")
        decided = await a.store.get_request(request.request_id)
        assert decided is not None
        assert decided.state is ApprovalState.APPROVED
    finally:
        await other.aclose()


async def test_decisions_are_immutable_in_postgres_too(store):
    manager = ApprovalManager(store)
    request = await manager.submit({"n": 1}, stage="pgtest")
    await manager.decide(request.request_id, "approve", actor="pgtest-a")
    with pytest.raises(ValueError, match="already decided"):
        await manager.decide(request.request_id, "reject", actor="pgtest-b")


async def test_the_audit_trail_is_append_only_and_ordered(store):
    manager = ApprovalManager(store)
    request = await manager.submit({"n": 1}, stage="pgtest")
    await manager.decide(request.request_id, "reject", actor="pgtest-reviewer")

    mine = [
        e for e in await manager.audit() if e.request_id == request.request_id
    ]
    assert [e.action for e in mine] == ["submit", "reject"]


async def test_nested_json_items_round_trip_through_jsonb(store):
    """Items are documents; JSONB must not flatten them."""
    manager = ApprovalManager(store)
    item = {
        "kind": "skill",
        "instruction": "use csv.DictReader",
        "evidence": {"successes": 3, "nodes": ["read", "verify"]},
        "score": 0.94,
        "ok": True,
    }
    request = await manager.submit(item, stage="pgtest")
    reloaded = await store.get_request(request.request_id)
    assert reloaded is not None
    assert reloaded.item == item


async def test_pending_is_ordered_oldest_first(store):
    manager = ApprovalManager(store)
    first = await manager.submit({"n": 1}, stage="pgtest")
    second = await manager.submit({"n": 2}, stage="pgtest")

    pending = [r for r in await manager.pending() if r.stage == "pgtest"]
    ids = [r.request_id for r in pending]
    assert ids.index(first.request_id) < ids.index(second.request_id)


async def test_the_skill_gate_works_against_postgres(store, tmp_path):
    """The full gate, on the backend a deployment actually uses."""
    library = SkillLibrary(tmp_path / "skills.json")
    gate = SkillGate(ApprovalManager(store), library)

    skill, request = await gate.submit(
        name="pg skill",
        task_pattern="parse csv",
        instruction="use DictReader with an explicit dialect",
        run_id="pgtest-run",
        criterion_hash="deadbeef",
        evidence=2,
    )
    assert not skill.usable

    decision = await gate.decide(
        request.request_id, "approve", actor="pgtest-reviewer"
    )
    assert decision.applied
    assert library.get(skill.skill_id).usable


async def test_schema_creation_is_idempotent(store):
    """Every replica runs it at startup; the second must not fail."""
    await store._get_pool()
    other = PostgresApprovalStore(DATABASE_URL)
    try:
        await other._get_pool()   # would raise if CREATE TABLE were unguarded
    finally:
        await other.aclose()


def test_construction_never_touches_the_network():
    """`swarmd --help` must not require a reachable database."""
    store = PostgresApprovalStore("postgres://nobody@10.255.255.1:5432/nope")
    assert store._pool is None
