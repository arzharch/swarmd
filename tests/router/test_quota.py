"""Quota tests.

The property that matters: the same credential used from N places must not
exceed the account's rate. In one process that is a bucket; across pods it is
Redis. Both are tested against a controlled clock, because a quota test that
depends on wall-clock sleep is slow and flaky in equal measure.
"""

from __future__ import annotations

import pytest

from swarmd.router.quota import (
    InProcessQuota,
    RedisQuota,
    _Bucket,
    build_quota,
)

# --- bucket mechanics ------------------------------------------------------


def test_bucket_starts_full_so_a_cold_run_is_not_penalised():
    b = _Bucket(rate_per_min=60, burst=10)
    assert b.tokens == 10


def test_bucket_grants_until_burst_is_spent_then_makes_you_wait():
    b = _Bucket(rate_per_min=60, burst=3, safety_margin=1.0)
    now = 1000.0
    assert b.take(1, now) == 0.0
    assert b.take(1, now) == 0.0
    assert b.take(1, now) == 0.0
    wait = b.take(1, now)
    assert wait == pytest.approx(1.0)  # 60/min = 1/s


def test_bucket_refills_continuously_rather_than_in_a_step():
    """A fixed window would grant nothing until the boundary, then everything."""
    b = _Bucket(rate_per_min=60, burst=10, safety_margin=1.0)
    now = 1000.0
    for _ in range(10):
        b.take(1, now)
    assert b.take(1, now) > 0
    assert b.take(1, now + 0.5) > 0  # half a token back, still short
    assert b.take(1, now + 1.0) == 0.0  # one full token back


def test_bucket_never_refills_past_burst():
    """Idle time must not bank unlimited permits into a stampede."""
    b = _Bucket(rate_per_min=600, burst=5, safety_margin=1.0)
    now = 1000.0
    granted = sum(1 for _ in range(20) if b.take(1, now + 3600) == 0.0)
    assert granted == 5


def test_safety_margin_declines_to_use_the_full_published_rate():
    """Provider clocks differ from ours; the margin absorbs the skew."""
    b = _Bucket(rate_per_min=60, burst=1, safety_margin=0.9)
    assert b.effective_rate_per_s == pytest.approx(0.9)


def test_a_zero_rate_bucket_reports_infinite_wait_rather_than_dividing_by_zero():
    b = _Bucket(rate_per_min=0, burst=0, safety_margin=1.0)
    assert b.take(1, 1000.0) == float("inf")


# --- in-process backend ----------------------------------------------------


async def test_unconfigured_keys_are_unlimited():
    q = InProcessQuota()
    assert await q.acquire("groq:key-1") == 0.0


async def test_configured_key_is_limited():
    q = InProcessQuota()
    await q.configure("groq:key-1", rate_per_min=60, burst=2)
    assert await q.acquire("groq:key-1") == 0.0
    assert await q.acquire("groq:key-1") == 0.0
    assert await q.acquire("groq:key-1") > 0.0


async def test_separate_credentials_have_separate_quotas():
    """Two keys means two accounts means twice the allowance."""
    q = InProcessQuota()
    await q.configure("groq:key-1", rate_per_min=60, burst=1)
    await q.configure("groq:key-2", rate_per_min=60, burst=1)
    assert await q.acquire("groq:key-1") == 0.0
    assert await q.acquire("groq:key-2") == 0.0
    assert await q.acquire("groq:key-1") > 0.0


async def test_reconfiguration_only_ever_tightens():
    """A 429 says our estimate was too high; a later looser guess must not win."""
    q = InProcessQuota()
    await q.configure("groq:k", rate_per_min=60, burst=10)
    await q.configure("groq:k", rate_per_min=10, burst=2)  # observed a 429
    await q.configure("groq:k", rate_per_min=600, burst=100)  # stale optimism
    snap = await q.snapshot()
    assert snap["groq:k"]["rate_per_min"] == 10
    assert snap["groq:k"]["burst"] == 2


async def test_snapshot_reports_effective_rate_not_published_rate():
    q = InProcessQuota()
    await q.configure("groq:k", rate_per_min=100, burst=5)
    snap = await q.snapshot()
    assert snap["groq:k"]["rate_per_min"] == 100
    assert snap["groq:k"]["effective_rate_per_min"] == pytest.approx(90.0)


async def test_cost_greater_than_one_consumes_proportionally():
    q = InProcessQuota()
    await q.configure("groq:k", rate_per_min=60, burst=10)
    assert await q.acquire("groq:k", cost=10) == 0.0
    assert await q.acquire("groq:k", cost=1) > 0.0


# --- redis backend ---------------------------------------------------------


class _BrokenRedis:
    """Stands in for an unreachable Redis."""

    async def _connect(self):
        raise ConnectionError("redis unreachable")


async def test_redis_backend_degrades_to_local_buckets_when_unreachable(monkeypatch):
    """Fail-open would let every pod run unlimited -- the exact stampede to avoid."""
    q = RedisQuota("redis://nowhere:6379", degraded_fraction=0.25)
    monkeypatch.setattr(
        q, "_connect", _BrokenRedis()._connect
    )
    await q.configure("groq:k", rate_per_min=100, burst=8)

    assert await q.acquire("groq:k") == 0.0
    assert q.degraded is True

    # Degraded rate is a fraction of the real one, sized so several blind pods
    # still stay under the account limit.
    snap = await q.snapshot()
    assert snap["groq:k"]["rate_per_min"] == pytest.approx(25.0)
    assert snap["groq:k"]["burst"] == 2


async def test_degraded_redis_still_enforces_a_limit(monkeypatch):
    q = RedisQuota("redis://nowhere:6379", degraded_fraction=0.25)
    monkeypatch.setattr(q, "_connect", _BrokenRedis()._connect)
    await q.configure("groq:k", rate_per_min=60, burst=4)  # degraded: 15/min, burst 1

    assert await q.acquire("groq:k") == 0.0
    assert await q.acquire("groq:k") > 0.0


async def test_unconfigured_key_on_redis_backend_is_unlimited():
    q = RedisQuota("redis://nowhere:6379")
    assert await q.acquire("never-configured") == 0.0


async def test_redis_script_is_a_single_atomic_unit():
    """Check-then-decrement across round trips is a race under contention."""
    from swarmd.router.quota import _REDIS_TAKE

    assert "HMGET" in _REDIS_TAKE
    assert "HMSET" in _REDIS_TAKE
    # Both the read and the write live in one script, not two calls.
    assert _REDIS_TAKE.count("redis.call") >= 3


async def test_redis_script_uses_the_server_clock_not_the_pod_clock():
    """Pod clocks drift; a bucket refilled against a fast clock overspends."""
    import inspect

    source = inspect.getsource(RedisQuota.acquire)
    assert "client.time()" in source


# --- selection -------------------------------------------------------------


def test_build_quota_defaults_to_in_process():
    assert isinstance(build_quota(None), InProcessQuota)


def test_build_quota_uses_redis_when_a_url_is_configured():
    assert isinstance(build_quota("redis://localhost:6379"), RedisQuota)
