"""Tests for StoreHarness: upsert idempotency, protocol conformance, factory."""

import pytest

from swarmd.harnesses.store import InMemoryStore, Record, make_store


async def test_upsert_is_idempotent_by_key() -> None:
    s = InMemoryStore()
    await s.upsert(Record(key="lead:1", data={"company": "acme"}, stage="enrich"))
    await s.upsert(
        Record(key="lead:1", data={"company": "acme", "score": 8}, stage="score")
    )
    assert await s.count() == 1
    got = await s.get("lead:1")
    assert got is not None and got.data["score"] == 8 and got.stage == "score"


async def test_list_all_sorted_and_complete() -> None:
    s = InMemoryStore()
    for k in ["c", "a", "b"]:
        await s.upsert(Record(key=k, data={}))
    keys = [r.key for r in await s.list_all()]
    assert keys == ["a", "b", "c"]


async def test_get_missing_returns_none() -> None:
    s = InMemoryStore()
    assert await s.get("nope") is None


def test_factory_modes() -> None:
    assert isinstance(make_store("memory"), InMemoryStore)
    with pytest.raises(ValueError, match="database_url"):
        make_store("postgres")
    with pytest.raises(ValueError, match="unknown store mode"):
        make_store("sqlite")


def test_postgres_store_constructs_without_network() -> None:
    """Lazy connect: construction must not touch the network."""
    from swarmd.harnesses.store import PostgresStore

    # Port 5434 = swarmd's compose mapping; never the shared 5432/5433.
    store = PostgresStore("postgres://user:pw@localhost:5434/db")
    assert store._pool is None
