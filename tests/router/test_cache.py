"""Tests for semantic cache and token budgets."""

import pytest

from swarmd.router.cache import (
    BudgetExceeded,
    SemanticCache,
    TokenBudget,
    cosine,
    hash_embedder,
)


def test_hash_embedder_is_deterministic_and_normalized() -> None:
    a1 = hash_embedder("enrich acme corp")
    a2 = hash_embedder("enrich acme corp")
    assert a1 == a2
    assert abs(sum(v * v for v in a1) - 1.0) < 1e-6


def test_similar_texts_score_higher_than_unrelated() -> None:
    base = hash_embedder("enrich the company acme corp with public signals")
    similar = hash_embedder("enrich the company acme corp using open data")
    unrelated = hash_embedder("draft a cold outreach email about pricing")
    assert cosine(base, similar) > cosine(base, unrelated)


async def test_exact_prompt_hits_cache() -> None:
    c = SemanticCache()
    await c.put("enrich acme", {"result": 1})
    assert await c.get("enrich acme") == {"result": 1}
    assert c.hits == 1 and c.misses == 0


async def test_different_prompts_miss() -> None:
    c = SemanticCache(threshold=0.95)
    await c.put("enrich acme", {"result": 1})
    assert await c.get("completely different topic about weather") is None
    assert c.misses == 1


async def test_lru_eviction_respects_capacity() -> None:
    c = SemanticCache(capacity=2)
    await c.put("p1", 1)
    await c.put("p2", 2)
    await c.get("p1")  # touch p1 so p2 becomes LRU
    await c.put("p3", 3)  # evicts p2
    assert len(c) == 2
    assert await c.get("p2") is None
    assert c.evictions == 1


async def test_ttl_expiry_sweeps_entries() -> None:
    c = SemanticCache(ttl_s=0.05)
    await c.put("p1", "old")
    import asyncio

    await asyncio.sleep(0.08)
    assert await c.get("p1") is None
    await c.put("p2", "new")  # triggers sweep
    assert len(c) == 1


def test_threshold_validation() -> None:
    with pytest.raises(ValueError):
        SemanticCache(threshold=0.0)
    with pytest.raises(ValueError):
        SemanticCache(threshold=1.5)


def test_token_budget_charges_and_breaches() -> None:
    b = TokenBudget(budget_tokens=100)
    b.charge(30, 20)
    assert b.remaining == 50
    with pytest.raises(BudgetExceeded):
        b.charge(40, 20)
    # Budget state reflects the breaching charge (fail-loud accounting).
    assert b.used == 110


def test_token_budget_boundary_exact_fit_passes() -> None:
    b = TokenBudget(budget_tokens=100)
    b.charge(60, 40)  # exactly at budget: allowed
    assert b.remaining == 0
