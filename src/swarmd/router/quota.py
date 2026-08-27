"""Rate-limit quota: correct in one process, correct across a cluster.

The problem Kubernetes creates. Provider limits are per ACCOUNT, not per
process. One pod politely holding itself to 45 RPM is correct. Three pods each
holding themselves to 45 RPM collectively hit the account at 135 RPM, get
everything throttled, and the backoff logic then makes it worse by treating a
self-inflicted 429 as a signal about the provider. Horizontal scaling silently
breaks a limiter that was correct when written.

So quota is an interface with two implementations:

  InProcessQuota  -- a token bucket in memory. Correct iff exactly one process
                     shares the credential. The default, because that is the
                     single-node case and paying for Redis to talk to yourself
                     is absurd.
  RedisQuota      -- the same token bucket, evaluated inside Redis as one
                     atomic script. Correct for any number of pods sharing a
                     credential. Required in the Kubernetes deployment.

Why a token bucket rather than a fixed window: a fixed window lets a caller
spend the entire minute's allowance in the first second, which reads to the
provider as a burst and earns a 429 despite the average being legal. A bucket
that refills continuously spreads the same allowance evenly, which is what
"30 requests per minute" actually means to the thing enforcing it.

Why the Redis logic is a Lua script: check-then-decrement across two round
trips is a race, and under contention from hundreds of agents it is not a rare
race. Redis runs the script atomically, so the check and the spend cannot be
interleaved by another pod.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class QuotaBackend(Protocol):
    """Shared-nothing interface so the pool never knows which backend it has."""

    async def acquire(self, key: str, *, cost: int = 1) -> float:
        """Reserve `cost` permits for `key`.

        Returns 0.0 when granted immediately, otherwise the number of seconds
        the caller should wait before the permits would be available. Returning
        a wait rather than sleeping keeps the scheduling decision with the pool,
        which may prefer to route to a different provider instead of waiting.
        """
        ...

    async def configure(self, key: str, *, rate_per_min: float, burst: int) -> None: ...

    async def snapshot(self) -> dict[str, dict[str, float]]: ...


@dataclass
class _Bucket:
    """Continuously-refilling token bucket.

    ANATOMY: rate_per_min
      Sustained permits per minute. Set from the provider's published limit at
      startup and corrected downward by observed 429s -- the provider's actual
      behaviour outranks its documentation (ADR-008).

    ANATOMY: burst
      How many permits may be spent instantaneously. Why default = rate/4:
      enough that a handful of agents starting together do not serialise behind
      the refill, small enough that a burst cannot consume the whole window and
      trip a provider that is measuring more finely than per-minute. Setting
      burst == rate reproduces the fixed-window behaviour this class exists to
      avoid.

    ANATOMY: safety_margin
      Fraction of the published rate we decline to use. Why 0.9: published
      limits are enforced with the provider's clock, not ours, and network
      jitter means our 30th request in a minute can arrive inside their
      previous window. Spending 90% keeps a margin for that skew. Raising it to
      1.0 measurably increases 429s for no extra throughput, because the
      rejected calls have to be retried anyway.
    """

    rate_per_min: float
    burst: int
    safety_margin: float = 0.9
    tokens: float = 0.0
    updated_at: float = field(default_factory=time.monotonic)

    def __post_init__(self) -> None:
        self.tokens = float(self.burst)

    @property
    def effective_rate_per_s(self) -> float:
        return (self.rate_per_min * self.safety_margin) / 60.0

    def _refill(self, now: float) -> None:
        elapsed = max(0.0, now - self.updated_at)
        self.tokens = min(
            float(self.burst), self.tokens + elapsed * self.effective_rate_per_s
        )
        self.updated_at = now

    def take(self, cost: int, now: float | None = None) -> float:
        """Spend permits, or return how long until they exist."""
        now = time.monotonic() if now is None else now
        self._refill(now)
        if self.tokens >= cost:
            self.tokens -= cost
            return 0.0
        deficit = cost - self.tokens
        rate = self.effective_rate_per_s
        return float("inf") if rate <= 0 else deficit / rate


class InProcessQuota:
    """Token buckets in memory. Correct for exactly one process per credential.

    Deliberately NOT thread-safe beyond the asyncio lock: swarmd's runtime is
    single-loop, and adding thread locks would imply a concurrency model the
    kernel does not have.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def configure(self, key: str, *, rate_per_min: float, burst: int) -> None:
        async with self._lock:
            existing = self._buckets.get(key)
            if existing is None:
                self._buckets[key] = _Bucket(rate_per_min=rate_per_min, burst=burst)
                return
            # Corrections only ever tighten. A provider that just 429'd is
            # telling us our estimate was too high; believing a later, looser
            # estimate would re-learn the same lesson every window.
            existing.rate_per_min = min(existing.rate_per_min, rate_per_min)
            existing.burst = min(existing.burst, burst)

    async def acquire(self, key: str, *, cost: int = 1) -> float:
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                # Unconfigured keys are unlimited. The pool configures every
                # credential at construction, so this is the free-provider case.
                return 0.0
            return bucket.take(cost)

    async def snapshot(self) -> dict[str, dict[str, float]]:
        async with self._lock:
            now = time.monotonic()
            out: dict[str, dict[str, float]] = {}
            for key, bucket in self._buckets.items():
                bucket._refill(now)
                out[key] = {
                    "tokens": round(bucket.tokens, 3),
                    "burst": float(bucket.burst),
                    "rate_per_min": bucket.rate_per_min,
                    "effective_rate_per_min": round(
                        bucket.effective_rate_per_s * 60.0, 3
                    ),
                }
            return out


# Atomic token bucket. Returns 0 when granted, else seconds to wait.
# Written as one script because check-then-decrement over two round trips is a
# race, and with hundreds of agents contending it is not a rare one.
_REDIS_TAKE = """
local key        = KEYS[1]
local rate_per_s = tonumber(ARGV[1])
local burst      = tonumber(ARGV[2])
local cost       = tonumber(ARGV[3])
local now        = tonumber(ARGV[4])
local ttl        = tonumber(ARGV[5])

local state   = redis.call('HMGET', key, 'tokens', 'ts')
local tokens  = tonumber(state[1])
local updated = tonumber(state[2])

if tokens == nil then
  tokens = burst
  updated = now
end

local elapsed = math.max(0, now - updated)
tokens = math.min(burst, tokens + elapsed * rate_per_s)

local wait = 0
if tokens >= cost then
  tokens = tokens - cost
else
  if rate_per_s <= 0 then
    wait = -1
  else
    wait = (cost - tokens) / rate_per_s
  end
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
redis.call('EXPIRE', key, ttl)
return tostring(wait)
"""


class RedisQuota:
    """Cluster-wide token bucket. Required whenever pods share a credential.

    Uses the Redis server clock (TIME) rather than each pod's clock, because
    pod clocks drift and a bucket refilled against a fast clock hands out
    permits that do not exist.

    Failure policy is FAIL-CLOSED-ISH: if Redis is unreachable, fall back to a
    local bucket at a fraction of the real rate. Fail-open would let every pod
    run unlimited during a Redis outage, which is precisely the stampede this
    class exists to prevent. Fail-fully-closed would turn a Redis blip into a
    total outage of a system whose whole point is degrading gracefully.
    """

    def __init__(
        self,
        url: str,
        *,
        namespace: str = "swarmd:quota",
        key_ttl_s: int = 300,
        degraded_fraction: float = 0.25,
    ) -> None:
        self.url = url
        self.namespace = namespace
        self.key_ttl_s = key_ttl_s
        self.degraded_fraction = degraded_fraction
        self._client: Any | None = None
        self._script: Any | None = None
        self._config: dict[str, tuple[float, int]] = {}
        self._fallback = InProcessQuota()
        self._degraded = False

    async def _connect(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis  # deferred: optional dependency

            self._client = redis.from_url(self.url, decode_responses=True)
            self._script = self._client.register_script(_REDIS_TAKE)
        return self._client

    async def configure(self, key: str, *, rate_per_min: float, burst: int) -> None:
        self._config[key] = (rate_per_min, burst)
        # The degraded path runs at a fraction of the real rate, sized so that
        # a plausible number of pods running blind still stays under the real
        # account limit.
        await self._fallback.configure(
            key,
            rate_per_min=rate_per_min * self.degraded_fraction,
            burst=max(1, int(burst * self.degraded_fraction)),
        )

    async def acquire(self, key: str, *, cost: int = 1) -> float:
        rate_per_min, burst = self._config.get(key, (0.0, 0))
        if rate_per_min <= 0:
            return 0.0
        try:
            client = await self._connect()
            server_s, server_us = await client.time()
            now = float(server_s) + float(server_us) / 1_000_000
            assert self._script is not None
            raw = await self._script(
                keys=[f"{self.namespace}:{key}"],
                args=[
                    (rate_per_min * 0.9) / 60.0,
                    burst,
                    cost,
                    now,
                    self.key_ttl_s,
                ],
            )
            if self._degraded:
                logger.info("quota backend recovered: redis reachable again")
                self._degraded = False
            wait = float(raw)
            return float("inf") if wait < 0 else wait
        except Exception as exc:  # noqa: BLE001 - any Redis failure degrades
            if not self._degraded:
                logger.warning(
                    "quota backend degraded to local buckets at %.0f%% rate: %s",
                    self.degraded_fraction * 100,
                    exc,
                )
                self._degraded = True
            return await self._fallback.acquire(key, cost=cost)

    @property
    def degraded(self) -> bool:
        return self._degraded

    async def snapshot(self) -> dict[str, dict[str, float]]:
        if self._degraded or self._client is None:
            return await self._fallback.snapshot()
        out: dict[str, dict[str, float]] = {}
        try:
            client = await self._connect()
            for key, (rate_per_min, burst) in self._config.items():
                state = await client.hgetall(f"{self.namespace}:{key}")
                out[key] = {
                    "tokens": float(state.get("tokens", burst)),
                    "burst": float(burst),
                    "rate_per_min": rate_per_min,
                    "effective_rate_per_min": round(rate_per_min * 0.9, 3),
                }
        except Exception:  # noqa: BLE001
            return await self._fallback.snapshot()
        return out

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_quota(redis_url: str | None = None) -> QuotaBackend:
    """Pick a backend. Redis when a URL is configured, in-process otherwise.

    The Kubernetes deployment always sets SWARMD_REDIS_URL; local runs do not,
    and paying for a network hop to coordinate with yourself would be silly.
    """
    if redis_url:
        return RedisQuota(redis_url)
    return InProcessQuota()
