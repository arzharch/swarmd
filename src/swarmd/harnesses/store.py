"""StoreHarness: persistence boundary for pipeline items.

Design notes:

- Protocol-first: `Store` defines upsert/get/list; PostgresStore implements it
  with asyncpg, InMemoryStore for tests/offline CI (ADR-004). Business code
  never knows which is behind it.
- Upserts are keyed by a caller-supplied unique key (e.g. lead email hash) so
  re-runs and chaos replays converge to the same rows — idempotency at the
  storage layer backs the integrity guarantees.
- PostgresStore connects lazily; construction never touches the network so
  importing/configuring it in tests stays free.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(slots=True)
class Record:
    key: str
    data: dict[str, Any]
    stage: str = ""
    updated_ts: float = field(default_factory=time.time)


class Store(Protocol):
    async def upsert(self, record: Record) -> None: ...
    async def get(self, key: str) -> Record | None: ...
    async def list_all(self) -> list[Record]: ...
    async def count(self) -> int: ...


class InMemoryStore:
    def __init__(self) -> None:
        self._rows: dict[str, Record] = {}

    async def upsert(self, record: Record) -> None:
        self._rows[record.key] = record

    async def get(self, key: str) -> Record | None:
        return self._rows.get(key)

    async def list_all(self) -> list[Record]:
        return sorted(self._rows.values(), key=lambda r: r.key)

    async def count(self) -> int:
        return len(self._rows)


class PostgresStore:
    """asyncpg-backed store. Requires DATABASE_URL (postgres://...).

    Schema is created on first connect (idempotent CREATE TABLE IF NOT EXISTS).
    """

    def __init__(self, database_url: str, table: str = "swarmd_records") -> None:
        self._url = database_url
        self._table = table
        self._pool: Any = None

    async def _connect(self) -> Any:
        if self._pool is None:
            import asyncpg  # deferred: keeps offline paths dependency-light

            self._pool = await asyncpg.create_pool(self._url)
            async with self._pool.acquire() as conn:
                await conn.execute(
                    f"""
                    CREATE TABLE IF NOT EXISTS {self._table} (
                        key TEXT PRIMARY KEY,
                        stage TEXT NOT NULL DEFAULT '',
                        data JSONB NOT NULL,
                        updated_ts DOUBLE PRECISION NOT NULL
                    )
                    """
                )
        return self._pool

    async def upsert(self, record: Record) -> None:
        pool = await self._connect()
        import json

        async with pool.acquire() as conn:
            await conn.execute(
                f"""
                INSERT INTO {self._table} (key, stage, data, updated_ts)
                VALUES ($1, $2, $3, $4)
                ON CONFLICT (key) DO UPDATE SET
                    stage = EXCLUDED.stage,
                    data = EXCLUDED.data,
                    updated_ts = EXCLUDED.updated_ts
                """,
                record.key,
                record.stage,
                json.dumps(record.data),
                record.updated_ts,
            )

    async def get(self, key: str) -> Record | None:
        pool = await self._connect()
        import json

        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"SELECT key, stage, data, updated_ts FROM {self._table} WHERE key = $1",
                key,
            )
        if row is None:
            return None
        return Record(
            key=row["key"],
            stage=row["stage"],
            data=json.loads(row["data"]),
            updated_ts=row["updated_ts"],
        )

    async def list_all(self) -> list[Record]:
        pool = await self._connect()
        import json

        async with pool.acquire() as conn:
            rows = await conn.fetch(
                f"SELECT key, stage, data, updated_ts FROM {self._table} ORDER BY key"
            )
        return [
            Record(
                key=r["key"],
                stage=r["stage"],
                data=json.loads(r["data"]),
                updated_ts=r["updated_ts"],
            )
            for r in rows
        ]

    async def count(self) -> int:
        pool = await self._connect()
        async with pool.acquire() as conn:
            n: int = await conn.fetchval(f"SELECT COUNT(*) FROM {self._table}")
            return n

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None


def make_store(mode: str = "memory", **kwargs: Any) -> Store:
    """Factory mirroring make_router's philosophy.

    ANATOMY: mode
      "memory"   -> dict-backed; tests, CI, offline demos (default)
      "postgres" -> durable; needs DATABASE_URL kwarg. Chosen when state must
                    survive process restarts (HITL durability, lead integrity).
    """
    if mode == "memory":
        return InMemoryStore()
    if mode == "postgres":
        url = kwargs.get("database_url") or ""
        if not url:
            raise ValueError("postgres store requires database_url")
        return PostgresStore(url, table=kwargs.get("table", "swarmd_records"))
    raise ValueError(f"unknown store mode: {mode!r}")
