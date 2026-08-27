"""Durable approval stores: SQLite by default, Postgres for deployment.

Phase 3's gate reads: start a pipeline, reach the review queue, kill the
process, restart, approve via CLI. That gate never actually passed, because the
CLI constructed a fresh `InMemoryApprovalStore` on every invocation -- so
`swarmd list` after a run showed nothing, and the durability the whole HITL
story rests on was a property of a single process's memory.

Two backends, one protocol:

  SqliteApprovalStore   -- default. Durable across processes with zero setup,
                           which means the gate passes on any machine rather
                           than only where Postgres happens to be running. A
                           gate that requires infrastructure to demonstrate is
                           a gate people stop running.
  PostgresApprovalStore -- deployment. Multiple control-plane replicas share
                           one queue, which SQLite cannot do safely over a
                           network filesystem.

The audit trail is append-only in both: `INSERT` only, no `UPDATE`, no
`DELETE`. Decisions are superseded by later entries rather than edited, because
an audit trail that can be rewritten is not evidence of anything.
"""

from __future__ import annotations

import asyncio
import json
import os
import sqlite3
from pathlib import Path
from typing import Any

from swarmd.hitl.approvals import ApprovalRequest, ApprovalState, AuditEntry

DEFAULT_SQLITE_PATH = Path.home() / ".swarmd" / "approvals.db"

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id  TEXT PRIMARY KEY,
    item        TEXT NOT NULL,
    stage       TEXT NOT NULL,
    created_ts  REAL NOT NULL,
    state       TEXT NOT NULL
);
-- Partial index: list_pending is the hot path and PENDING is a shrinking
-- fraction of the table over time, so indexing only those rows keeps the index
-- small no matter how much decided history accumulates.
CREATE INDEX IF NOT EXISTS idx_pending
    ON approval_requests (created_ts) WHERE state = 'PENDING';

CREATE TABLE IF NOT EXISTS approval_audit (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          REAL NOT NULL,
    request_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON approval_audit (request_id);
"""


class SqliteApprovalStore:
    """File-backed store. Durable across processes, no server required.

    Runs SQLite calls in a thread executor rather than blocking the event loop.
    sqlite3 is synchronous, and a blocking write inside an async runtime stalls
    every agent in the process -- which for a system whose selling point is
    concurrency would be an unusually embarrassing bug.

    WAL mode is on so a reader (`swarmd list`) does not block a writer (a run
    queueing an approval). Without it the CLI and a live run contend for a
    single lock and one of them fails with "database is locked", which reads as
    a bug in the pipeline rather than a journaling mode.
    """

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("SWARMD_APPROVALS_DB")
                         or DEFAULT_SQLITE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        # NORMAL rather than FULL: WAL already survives process death, and FULL
        # fsyncs on every commit. Approvals are human-paced, so the durability
        # difference only matters on OS crash, which is not the failure this
        # store exists to survive.
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)

    async def _run(self, fn: Any, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    # -- requests -----------------------------------------------------------

    async def save_request(self, req: ApprovalRequest) -> None:
        def _save() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO approval_requests
                           (request_id, item, stage, created_ts, state)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(request_id) DO UPDATE SET
                           item = excluded.item,
                           state = excluded.state""",
                    (
                        req.request_id,
                        json.dumps(req.item),
                        req.stage,
                        req.created_ts,
                        req.state.value,
                    ),
                )

        await self._run(_save)

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        def _get() -> ApprovalRequest | None:
            with self._connect() as conn:
                row = conn.execute(
                    "SELECT * FROM approval_requests WHERE request_id = ?",
                    (request_id,),
                ).fetchone()
            return _row_to_request(row) if row else None

        result: ApprovalRequest | None = await self._run(_get)
        return result

    async def list_pending(self) -> list[ApprovalRequest]:
        def _list() -> list[ApprovalRequest]:
            with self._connect() as conn:
                rows = conn.execute(
                    """SELECT * FROM approval_requests
                       WHERE state = 'PENDING' ORDER BY created_ts"""
                ).fetchall()
            return [_row_to_request(r) for r in rows]

        result: list[ApprovalRequest] = await self._run(_list)
        return result

    # -- audit --------------------------------------------------------------

    async def append_audit(self, entry: AuditEntry) -> None:
        def _append() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO approval_audit (ts, request_id, action, actor, detail)
                       VALUES (?, ?, ?, ?, ?)""",
                    (entry.ts, entry.request_id, entry.action, entry.actor, entry.detail),
                )

        await self._run(_append)

    async def read_audit(self) -> list[AuditEntry]:
        def _read() -> list[AuditEntry]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM approval_audit ORDER BY seq"
                ).fetchall()
            return [
                AuditEntry(
                    ts=r["ts"],
                    request_id=r["request_id"],
                    action=r["action"],
                    actor=r["actor"],
                    detail=r["detail"],
                )
                for r in rows
            ]

        result: list[AuditEntry] = await self._run(_read)
        return result


def _row_to_request(row: Any) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=row["request_id"],
        item=json.loads(row["item"]),
        stage=row["stage"],
        created_ts=row["created_ts"],
        state=ApprovalState(row["state"]),
    )


_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_requests (
    request_id  TEXT PRIMARY KEY,
    item        JSONB NOT NULL,
    stage       TEXT NOT NULL,
    created_ts  DOUBLE PRECISION NOT NULL,
    state       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pending
    ON approval_requests (created_ts) WHERE state = 'PENDING';

CREATE TABLE IF NOT EXISTS approval_audit (
    seq         BIGSERIAL PRIMARY KEY,
    ts          DOUBLE PRECISION NOT NULL,
    request_id  TEXT NOT NULL,
    action      TEXT NOT NULL,
    actor       TEXT NOT NULL,
    detail      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_request ON approval_audit (request_id);
"""


class PostgresApprovalStore:
    """asyncpg-backed store for deployments with more than one replica.

    Connects lazily. Construction must never touch the network, or importing
    the CLI would require a database to be reachable -- which would make
    `swarmd --help` fail during an outage.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get("DATABASE_URL", "")
        if not self._url:
            raise RuntimeError("PostgresApprovalStore requires DATABASE_URL")
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg  # deferred: keeps offline paths dependency-light

                self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
                async with self._pool.acquire() as conn:
                    await conn.execute(_PG_SCHEMA)
            return self._pool

    async def save_request(self, req: ApprovalRequest) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO approval_requests
                       (request_id, item, stage, created_ts, state)
                   VALUES ($1, $2::jsonb, $3, $4, $5)
                   ON CONFLICT (request_id) DO UPDATE SET
                       item = EXCLUDED.item, state = EXCLUDED.state""",
                req.request_id,
                json.dumps(req.item),
                req.stage,
                req.created_ts,
                req.state.value,
            )

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT * FROM approval_requests WHERE request_id = $1", request_id
            )
        return _pg_row_to_request(row) if row else None

    async def list_pending(self) -> list[ApprovalRequest]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                """SELECT * FROM approval_requests
                   WHERE state = 'PENDING' ORDER BY created_ts"""
            )
        return [_pg_row_to_request(r) for r in rows]

    async def append_audit(self, entry: AuditEntry) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            await conn.execute(
                """INSERT INTO approval_audit (ts, request_id, action, actor, detail)
                   VALUES ($1, $2, $3, $4, $5)""",
                entry.ts, entry.request_id, entry.action, entry.actor, entry.detail,
            )

    async def read_audit(self) -> list[AuditEntry]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch("SELECT * FROM approval_audit ORDER BY seq")
        return [
            AuditEntry(
                ts=r["ts"], request_id=r["request_id"], action=r["action"],
                actor=r["actor"], detail=r["detail"],
            )
            for r in rows
        ]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def _pg_row_to_request(row: Any) -> ApprovalRequest:
    item = row["item"]
    return ApprovalRequest(
        request_id=row["request_id"],
        item=json.loads(item) if isinstance(item, str) else item,
        stage=row["stage"],
        created_ts=row["created_ts"],
        state=ApprovalState(row["state"]),
    )


def build_approval_store(url: str | None = None, path: str | Path | None = None) -> Any:
    """Pick a backend: Postgres when DATABASE_URL is set, SQLite otherwise.

    Never returns the in-memory store. That one exists for unit tests, and
    defaulting to it is exactly the bug that made Phase 3's gate pass on paper
    while approvals silently vanished between processes.
    """
    database_url = url or os.environ.get("DATABASE_URL", "")
    if database_url:
        return PostgresApprovalStore(database_url)
    return SqliteApprovalStore(path)
