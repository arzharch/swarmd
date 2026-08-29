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

TWO DIMENSIONS, NOT ONE. Every provider here publishes a requests-per-minute
AND a tokens-per-minute, and the second is usually the one that binds. Groq
allows 30 RPM and 8,000 TPM; this deployment's calls average a little over
1,000 tokens, so a limiter that paced only requests would happily send 30 legal
requests carrying 30,000 tokens into an 8,000-token minute and collect 22
rejections for it. Pacing requests alone is not a conservative approximation of
pacing both -- against these numbers it is barely a limiter at all.

The two buckets are taken TOGETHER or not at all. Spending a request permit and
then discovering the token bucket is dry would drain the request allowance at
the token bucket's refill rate, and the limiter would throttle itself on a
dimension that had capacity to spare.

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

    async def acquire(self, key: str, *, cost: int = 1, tokens: int = 0) -> float:
        """Reserve `cost` request permits and `tokens` token permits for `key`.

        Returns 0.0 when granted immediately, otherwise the number of seconds
        the caller should wait before BOTH would be available. Returning a wait
        rather than sleeping keeps the scheduling decision with the pool, which
        may prefer to route to a different provider instead of waiting.

        Nothing is spent on a refusal. A partial take -- requests granted,
        tokens not -- would leak the request allowance every time the token
        bucket was the binding one.
        """
        ...

    async def configure(
        self,
        key: str,
        *,
        rate_per_min: float,
        burst: int,
        tokens_per_min: float = 0.0,
        token_burst: int = 0,
    ) -> None: ...

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

    def wait_for(self, cost: int, now: float | None = None) -> float:
        """How long until `cost` permits exist. Spends nothing.

        Split from `take` so a two-dimension acquire can ask both buckets
        before committing to either.
        """
        now = time.monotonic() if now is None else now
        self._refill(now)
        if self.tokens >= cost:
            return 0.0
        deficit = cost - self.tokens
        rate = self.effective_rate_per_s
        return float("inf") if rate <= 0 else deficit / rate

    def spend(self, cost: int) -> None:
        self.tokens -= cost

    def take(self, cost: int, now: float | None = None) -> float:
        """Spend permits, or return how long until they exist."""
        wait = self.wait_for(cost, now)
        if wait == 0.0:
            self.spend(cost)
        return wait


class InProcessQuota:
    """Token buckets in memory. Correct for exactly one process per credential.

    Two buckets per key -- requests and tokens -- because providers meter both
    and the token one usually binds first. A key with no declared TPM keeps only
    the request bucket, so nothing is invented for a provider that publishes
    nothing.

    Deliberately NOT thread-safe beyond the asyncio lock: swarmd's runtime is
    single-loop, and adding thread locks would imply a concurrency model the
    kernel does not have.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._tokens: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def configure(
        self,
        key: str,
        *,
        rate_per_min: float,
        burst: int,
        tokens_per_min: float = 0.0,
        token_burst: int = 0,
    ) -> None:
        async with self._lock:
            self._tighten(self._buckets, key, rate_per_min, burst)
            if tokens_per_min > 0:
                # A whole minute's tokens as the burst, unlike requests. One
                # call can legitimately be a large share of a minute's token
                # allowance -- 1,000 of Groq's 8,000 -- so a quartered burst
                # would refuse the very first call of an idle minute and then
                # serialise every call behind the refill.
                self._tighten(
                    self._tokens, key, tokens_per_min,
                    token_burst or int(tokens_per_min),
                )

    @staticmethod
    def _tighten(
        buckets: dict[str, _Bucket], key: str, rate_per_min: float, burst: int
    ) -> None:
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = _Bucket(rate_per_min=rate_per_min, burst=max(1, burst))
            return
        # Corrections only ever tighten. A provider that just 429'd is
        # telling us our estimate was too high; believing a later, looser
        # estimate would re-learn the same lesson every window.
        existing.rate_per_min = min(existing.rate_per_min, rate_per_min)
        existing.burst = min(existing.burst, max(1, burst))

    async def acquire(self, key: str, *, cost: int = 1, tokens: int = 0) -> float:
        async with self._lock:
            now = time.monotonic()
            pairs: list[tuple[_Bucket, int]] = []
            requests = self._buckets.get(key)
            if requests is not None:
                pairs.append((requests, cost))
            token_bucket = self._tokens.get(key)
            if token_bucket is not None and tokens > 0:
                pairs.append((token_bucket, tokens))
            if not pairs:
                # Unconfigured keys are unlimited. The pool configures every
                # credential at construction, so this is the free-provider case.
                return 0.0
            # Ask both, then spend both. The worst wait is the answer: waiting
            # for the sooner one and re-asking would spin against the later.
            waits = [bucket.wait_for(want, now) for bucket, want in pairs]
            worst = max(waits)
            if worst > 0:
                return worst
            for bucket, want in pairs:
                bucket.spend(want)
            return 0.0

    async def snapshot(self) -> dict[str, dict[str, float]]:
        async with self._lock:
            now = time.monotonic()
            out: dict[str, dict[str, float]] = {}
            for key, bucket in self._buckets.items():
                bucket._refill(now)
                entry = {
                    "tokens": round(bucket.tokens, 3),
                    "burst": float(bucket.burst),
                    "rate_per_min": bucket.rate_per_min,
                    "effective_rate_per_min": round(
                        bucket.effective_rate_per_s * 60.0, 3
                    ),
                }
                token_bucket = self._tokens.get(key)
                if token_bucket is not None:
                    token_bucket._refill(now)
                    entry["token_permits"] = round(token_bucket.tokens, 3)
                    entry["token_burst"] = float(token_bucket.burst)
                    entry["tokens_per_min"] = token_bucket.rate_per_min
                out[key] = entry
            return out


# Atomic two-dimension token bucket. Returns 0 when granted, else seconds to
# wait. Written as one script because check-then-decrement over two round trips
# is a race, and with hundreds of agents contending it is not a rare one -- and
# because the two dimensions must be decided together: granting requests and
# refusing tokens would leak the request allowance on every refusal.
_REDIS_TAKE = """
local key        = KEYS[1]
local rate_per_s = tonumber(ARGV[1])
local burst      = tonumber(ARGV[2])
local cost       = tonumber(ARGV[3])
local now        = tonumber(ARGV[4])
local ttl        = tonumber(ARGV[5])
local tok_per_s  = tonumber(ARGV[6])   -- 0 when the provider declares no TPM
local tok_burst  = tonumber(ARGV[7])
local tok_cost   = tonumber(ARGV[8])

local state = redis.call('HMGET', key, 'tokens', 'ts', 'tok', 'tok_ts')

local function refill(level, updated, cap, rate)
  if level == nil then return cap, now end
  local elapsed = math.max(0, now - updated)
  return math.min(cap, level + elapsed * rate), now
end

local function need(level, cap, rate, want)
  if level >= want then return 0 end
  if rate <= 0 then return -1 end
  return (want - level) / rate
end

local tokens = refill(tonumber(state[1]), tonumber(state[2]), burst, rate_per_s)
local wait = need(tokens, burst, rate_per_s, cost)

local toks, tok_wait = nil, 0
if tok_per_s > 0 and tok_cost > 0 then
  toks = refill(tonumber(state[3]), tonumber(state[4]), tok_burst, tok_per_s)
  tok_wait = need(toks, tok_burst, tok_per_s, tok_cost)
end

if wait < 0 or tok_wait < 0 then
  wait = -1
elseif wait > 0 or tok_wait > 0 then
  wait = math.max(wait, tok_wait)
else
  tokens = tokens - cost
  if toks ~= nil then toks = toks - tok_cost end
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', now)
if toks ~= nil then
  redis.call('HMSET', key, 'tok', toks, 'tok_ts', now)
end
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
        self._config: dict[str, tuple[float, int, float, int]] = {}
        self._fallback = InProcessQuota()
        self._degraded = False

    async def _connect(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis  # deferred: optional dependency

            self._client = redis.from_url(self.url, decode_responses=True)
            self._script = self._client.register_script(_REDIS_TAKE)
        return self._client

    async def configure(
        self,
        key: str,
        *,
        rate_per_min: float,
        burst: int,
        tokens_per_min: float = 0.0,
        token_burst: int = 0,
    ) -> None:
        self._config[key] = (
            rate_per_min, burst, tokens_per_min, token_burst or int(tokens_per_min)
        )
        # The degraded path runs at a fraction of the real rate, sized so that
        # a plausible number of pods running blind still stays under the real
        # account limit. Both dimensions are scaled: a pod that kept the full
        # TPM while dropping to a quarter of the RPM would run four pods'
        # worth of tokens through a quarter of the requests, which is the
        # bigger of the two limits to breach.
        await self._fallback.configure(
            key,
            rate_per_min=rate_per_min * self.degraded_fraction,
            burst=max(1, int(burst * self.degraded_fraction)),
            tokens_per_min=tokens_per_min * self.degraded_fraction,
            token_burst=max(1, int((token_burst or tokens_per_min) * self.degraded_fraction)),
        )

    async def acquire(self, key: str, *, cost: int = 1, tokens: int = 0) -> float:
        rate_per_min, burst, tokens_per_min, token_burst = self._config.get(
            key, (0.0, 0, 0.0, 0)
        )
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
                    (tokens_per_min * 0.9) / 60.0,
                    token_burst,
                    tokens,
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
            return await self._fallback.acquire(key, cost=cost, tokens=tokens)

    @property
    def degraded(self) -> bool:
        return self._degraded

    async def snapshot(self) -> dict[str, dict[str, float]]:
        if self._degraded or self._client is None:
            return await self._fallback.snapshot()
        out: dict[str, dict[str, float]] = {}
        try:
            client = await self._connect()
            for key, (rate_per_min, burst, tpm, tok_burst) in self._config.items():
                state = await client.hgetall(f"{self.namespace}:{key}")
                entry = {
                    "tokens": float(state.get("tokens", burst)),
                    "burst": float(burst),
                    "rate_per_min": rate_per_min,
                    "effective_rate_per_min": round(rate_per_min * 0.9, 3),
                }
                if tpm > 0:
                    entry["token_permits"] = float(state.get("tok", tok_burst))
                    entry["token_burst"] = float(tok_burst)
                    entry["tokens_per_min"] = tpm
                out[key] = entry
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
