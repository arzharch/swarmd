"""Pacing tokens as well as requests.

Every provider here publishes a requests-per-minute AND a tokens-per-minute,
and the second is usually the one that binds. Groq allows 30 requests and 8,000
tokens a minute; this deployment's calls average a little over 1,000 tokens, so
a limiter pacing only requests would send 30 legal requests carrying 30,000
tokens into an 8,000-token minute and collect 22 rejections for it.
"""

from __future__ import annotations

import pytest

from swarmd.router.quota import InProcessQuota

KEY = "groq#0"


@pytest.fixture
def quota():
    return InProcessQuota()


async def test_requests_alone_no_longer_decide(quota):
    """The failure this exists to prevent: enough request permits to send far
    more tokens than the minute allows."""
    await quota.configure(
        KEY, rate_per_min=30, burst=30, tokens_per_min=8_000
    )
    granted = 0
    for _ in range(30):
        if await quota.acquire(KEY, tokens=1_000) == 0:
            granted += 1
    assert granted <= 8, f"{granted} calls x 1,000 tokens into an 8,000 minute"


async def test_a_provider_with_no_token_limit_is_paced_on_requests_only(quota):
    """Most providers publish no tokens-per-minute. Treating an undeclared
    limit as a limit of zero would refuse every call forever."""
    await quota.configure(KEY, rate_per_min=60, burst=10)
    assert await quota.acquire(KEY, tokens=5_000) == 0


async def test_an_unconfigured_key_is_unlimited(quota):
    assert await quota.acquire("never-configured", tokens=1_000_000) == 0


async def test_the_first_call_of_an_idle_minute_is_not_refused(quota):
    """The burst trap. One call is legitimately a large share of a minute's
    token allowance -- 1,000 of Groq's 8,000 -- so quartering the token burst
    the way the request burst is quartered refuses the very first call and
    then serialises every later one behind the refill."""
    await quota.configure(
        KEY, rate_per_min=30, burst=7, tokens_per_min=8_000
    )
    assert await quota.acquire(KEY, tokens=1_035) == 0


async def test_a_refusal_says_how_long_to_wait(quota):
    await quota.configure(KEY, rate_per_min=30, burst=30, tokens_per_min=6_000)
    await quota.acquire(KEY, tokens=6_000)
    wait = await quota.acquire(KEY, tokens=6_000)
    assert wait > 0


async def test_nothing_is_spent_when_the_token_bucket_refuses(quota):
    """A partial take -- requests granted, tokens not -- leaks the request
    allowance every time the token bucket is the binding one, and the limiter
    then throttles itself on a dimension with capacity to spare."""
    await quota.configure(
        KEY, rate_per_min=600, burst=600, tokens_per_min=1_000
    )
    await quota.acquire(KEY, tokens=1_000)          # drains tokens, not requests
    for _ in range(20):
        await quota.acquire(KEY, tokens=1_000)      # each must refuse

    snapshot = await quota.snapshot()
    # 21 refusals must not have consumed 21 request permits.
    assert snapshot[KEY]["tokens"] > 500


async def test_a_zero_token_call_is_not_blocked_by_an_empty_token_bucket(quota):
    await quota.configure(KEY, rate_per_min=60, burst=60, tokens_per_min=1_000)
    await quota.acquire(KEY, tokens=1_000)
    assert await quota.acquire(KEY, tokens=0) == 0


async def test_corrections_only_ever_tighten(quota):
    """A provider that just 429'd is saying our estimate was too high.
    Believing a later, looser estimate re-learns the same lesson every
    window."""
    await quota.configure(KEY, rate_per_min=30, burst=30, tokens_per_min=8_000)
    await quota.configure(KEY, rate_per_min=15, burst=1, tokens_per_min=4_000)
    await quota.configure(KEY, rate_per_min=60, burst=60, tokens_per_min=99_000)

    snapshot = await quota.snapshot()
    assert snapshot[KEY]["rate_per_min"] == 15
    assert snapshot[KEY]["tokens_per_min"] == 4_000


async def test_the_snapshot_reports_both_dimensions(quota):
    """An operator debugging a stall needs to see which bucket is empty."""
    await quota.configure(KEY, rate_per_min=30, burst=30, tokens_per_min=8_000)
    entry = (await quota.snapshot())[KEY]
    assert entry["rate_per_min"] == 30
    assert entry["tokens_per_min"] == 8_000
    assert "token_permits" in entry


async def test_the_pool_configures_the_token_dimension_from_the_spec(tmp_path):
    """The limiter is only as good as what the pool tells it."""
    from swarmd.router.pool import ProviderPool, ProviderSpec, _Slot
    from tests.router.test_pool import FakeProvider

    spec = ProviderSpec(
        name="groq", base_url="http://x", api_key_env="X",
        models=("m",), tier="free", hint_rpm=30, hint_tpm=8_000,
    )
    pool = ProviderPool(
        [_Slot(FakeProvider("groq", ["m"], None), spec, credential_id="groq#0")],  # type: ignore[arg-type]
    )
    await pool._ensure_quota_configured()
    entry = (await pool.quota.snapshot())["groq#0"]
    assert entry["tokens_per_min"] == 8_000
    # A whole minute's tokens, not a quarter of them.
    assert entry["token_burst"] == 8_000
    await pool.aclose()
