"""Postgres + SQLite store for browser session audit records.

Every browser session a swarmd agent runs is persisted here so it can:
  - Be reviewed in the dashboard
  - Be queried for pattern analysis ("which sites does the agent visit most?")
  - Be replayed as eval fixtures ("re-run this session, grade its extraction")
  - Feed the HITL queue when a session parks for human input

Schema:

  browser_sessions   -- one row per session (summary + artifacts)
  browser_actions    -- one row per action within a session (audit trail)

Both tables use Postgres JSONB where appropriate so the dashboard can filter
on action kind, outcome, run_id, etc. without deserialising in Python.

SQLite fallback is provided for local dev (no DATABASE_URL configured).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_SQLITE_PATH = Path.home() / ".swarmd" / "browser_sessions.db"

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL DEFAULT '',
    agent_id     TEXT NOT NULL DEFAULT '',
    created_ts   REAL NOT NULL,
    duration_s   REAL NOT NULL DEFAULT 0,
    ok           INTEGER NOT NULL DEFAULT 0,
    hitl_request TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    artifacts    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_bs_run ON browser_sessions (run_id);
CREATE INDEX IF NOT EXISTS idx_bs_agent ON browser_sessions (agent_id);

CREATE TABLE IF NOT EXISTS browser_actions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id   TEXT NOT NULL REFERENCES browser_sessions(session_id),
    ts           REAL NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ba_session ON browser_actions (session_id);
CREATE INDEX IF NOT EXISTS idx_ba_kind    ON browser_actions (kind);
"""

_PG_SCHEMA = """
CREATE TABLE IF NOT EXISTS browser_sessions (
    session_id   TEXT PRIMARY KEY,
    run_id       TEXT NOT NULL DEFAULT '',
    agent_id     TEXT NOT NULL DEFAULT '',
    created_ts   DOUBLE PRECISION NOT NULL,
    duration_s   DOUBLE PRECISION NOT NULL DEFAULT 0,
    ok           BOOLEAN NOT NULL DEFAULT FALSE,
    hitl_request TEXT NOT NULL DEFAULT '',
    error        TEXT NOT NULL DEFAULT '',
    artifacts    JSONB NOT NULL DEFAULT '{}'::jsonb
);
CREATE INDEX IF NOT EXISTS idx_bs_run   ON browser_sessions (run_id);
CREATE INDEX IF NOT EXISTS idx_bs_agent ON browser_sessions (agent_id);
CREATE INDEX IF NOT EXISTS idx_bs_hitl  ON browser_sessions (created_ts) WHERE hitl_request != '';

CREATE TABLE IF NOT EXISTS browser_actions (
    id           BIGSERIAL PRIMARY KEY,
    session_id   TEXT NOT NULL REFERENCES browser_sessions(session_id),
    ts           DOUBLE PRECISION NOT NULL,
    kind         TEXT NOT NULL,
    detail       TEXT NOT NULL DEFAULT '',
    outcome      TEXT NOT NULL DEFAULT '',
    data         TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_ba_session ON browser_actions (session_id);
CREATE INDEX IF NOT EXISTS idx_ba_kind    ON browser_actions (kind);
"""


# ---------------------------------------------------------------------------
# SQLite backend (default / local dev)
# ---------------------------------------------------------------------------


class SqliteBrowserAuditStore:
    """File-backed session store. Runs in a thread pool (sqlite3 is sync)."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path or os.environ.get("SWARMD_BROWSER_DB") or DEFAULT_SQLITE_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = asyncio.Lock()
        self._init()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=10.0, isolation_level=None)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init(self) -> None:
        with self._connect() as conn:
            conn.executescript(_SQLITE_SCHEMA)

    async def _run(self, fn: Any, *args: Any) -> Any:
        async with self._lock:
            return await asyncio.get_running_loop().run_in_executor(None, fn, *args)

    async def record_session(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_id: str,
        ok: bool,
        hitl_request: str,
        error: str,
        duration_s: float,
        artifacts: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> None:
        def _write() -> None:
            with self._connect() as conn:
                conn.execute(
                    """INSERT INTO browser_sessions
                           (session_id, run_id, agent_id, created_ts, duration_s,
                            ok, hitl_request, error, artifacts)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(session_id) DO UPDATE SET
                           ok=excluded.ok, hitl_request=excluded.hitl_request,
                           error=excluded.error, duration_s=excluded.duration_s,
                           artifacts=excluded.artifacts""",
                    (
                        session_id, run_id, agent_id, time.time(), duration_s,
                        int(ok), hitl_request, error,
                        json.dumps(artifacts, default=str),
                    ),
                )
                conn.executemany(
                    """INSERT INTO browser_actions
                           (session_id, ts, kind, detail, outcome, data)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    [
                        (
                            session_id,
                            a["ts"], a["kind"], a["detail"], a["outcome"], a["data"],
                        )
                        for a in actions
                    ],
                )

        await self._run(_write)

    async def list_sessions(
        self, *, run_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._connect() as conn:
                if run_id:
                    rows = conn.execute(
                        "SELECT * FROM browser_sessions WHERE run_id=? ORDER BY created_ts DESC LIMIT ?",
                        (run_id, limit),
                    ).fetchall()
                else:
                    rows = conn.execute(
                        "SELECT * FROM browser_sessions ORDER BY created_ts DESC LIMIT ?",
                        (limit,),
                    ).fetchall()
                return [dict(r) for r in rows]

        result: list[dict[str, Any]] = await self._run(_read)
        return result

    async def list_actions(self, session_id: str) -> list[dict[str, Any]]:
        def _read() -> list[dict[str, Any]]:
            with self._connect() as conn:
                rows = conn.execute(
                    "SELECT * FROM browser_actions WHERE session_id=? ORDER BY id",
                    (session_id,),
                ).fetchall()
                return [dict(r) for r in rows]

        result: list[dict[str, Any]] = await self._run(_read)
        return result


# ---------------------------------------------------------------------------
# Postgres backend (deployment)
# ---------------------------------------------------------------------------


class PostgresBrowserAuditStore:
    """asyncpg-backed store for multi-replica deployments.

    Lazy connection: construction never touches the network, so importing this
    module during startup does not fail if Postgres is temporarily unreachable.
    """

    def __init__(self, url: str | None = None) -> None:
        self._url = url or os.environ.get("DATABASE_URL", "")
        if not self._url:
            raise RuntimeError("PostgresBrowserAuditStore requires DATABASE_URL")
        self._pool: Any | None = None
        self._lock = asyncio.Lock()

    async def _get_pool(self) -> Any:
        async with self._lock:
            if self._pool is None:
                import asyncpg  # deferred

                self._pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
                async with self._pool.acquire() as conn:
                    await conn.execute(_PG_SCHEMA)
            return self._pool

    async def record_session(
        self,
        *,
        session_id: str,
        run_id: str,
        agent_id: str,
        ok: bool,
        hitl_request: str,
        error: str,
        duration_s: float,
        artifacts: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """INSERT INTO browser_sessions
                           (session_id, run_id, agent_id, created_ts, duration_s,
                            ok, hitl_request, error, artifacts)
                       VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9::jsonb)
                       ON CONFLICT (session_id) DO UPDATE SET
                           ok=EXCLUDED.ok, hitl_request=EXCLUDED.hitl_request,
                           error=EXCLUDED.error, duration_s=EXCLUDED.duration_s,
                           artifacts=EXCLUDED.artifacts""",
                    session_id, run_id, agent_id, time.time(), duration_s,
                    ok, hitl_request, error,
                    json.dumps(artifacts, default=str),
                )
                if actions:
                    await conn.executemany(
                        """INSERT INTO browser_actions
                               (session_id, ts, kind, detail, outcome, data)
                           VALUES ($1, $2, $3, $4, $5, $6)""",
                        [
                            (
                                session_id,
                                a["ts"], a["kind"], a["detail"], a["outcome"], a["data"],
                            )
                            for a in actions
                        ],
                    )

    async def list_sessions(
        self, *, run_id: str = "", limit: int = 100
    ) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            if run_id:
                rows = await conn.fetch(
                    "SELECT * FROM browser_sessions WHERE run_id=$1 ORDER BY created_ts DESC LIMIT $2",
                    run_id, limit,
                )
            else:
                rows = await conn.fetch(
                    "SELECT * FROM browser_sessions ORDER BY created_ts DESC LIMIT $1", limit
                )
            return [dict(r) for r in rows]

    async def list_actions(self, session_id: str) -> list[dict[str, Any]]:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                "SELECT * FROM browser_actions WHERE session_id=$1 ORDER BY id",
                session_id,
            )
            return [dict(r) for r in rows]

    async def aclose(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def build_browser_audit_store(
    url: str | None = None,
    path: str | Path | None = None,
) -> SqliteBrowserAuditStore | PostgresBrowserAuditStore:
    """Postgres when DATABASE_URL is set, SQLite otherwise."""
    database_url = url or os.environ.get("DATABASE_URL", "")
    if database_url:
        return PostgresBrowserAuditStore(database_url)
    return SqliteBrowserAuditStore(path)
