"""Session rations: a day's allowance spread evenly over the day's sittings.

THE PROBLEM THIS SOLVES. A daily quota is a cliff, not a rate. A run started at
09:00 can legally spend the whole of Groq's day in forty minutes, and every run
after it until midnight gets nothing. The budget tracker in `budget.py` will
report that correctly and stop the second run, which is honest and useless: the
operator wanted both runs, and the capacity for both existed.

So the day is cut into SITTINGS and each sitting gets a slice:

    ration = (effective daily limit - what earlier sittings spent) / sittings left

and a call is admitted only if this sitting's spend plus the call still fits.
Two properties fall out of that one expression, and they are the whole design:

  A sitting can never consume the day. Whatever happens, at most 1/N of what
  remains is reachable before the next boundary, so a run cannot leave the rest
  of the day empty.

  Nothing is stranded. An under-used sitting's slice is not lost -- it is back
  in the numerator when the next sitting divides by a smaller N, and the last
  sitting before a reset gets everything that is left.

WHY IT IS DERIVED, NEVER COUNTED. Every figure here is a sum over the append-only
usage journal, for the reason ADR-007 gives for cost: a counter that drifts from
what happened reports a number nobody can check. It also makes restarts free --
there is no ration state to persist, because the journal already holds it.

RESERVE THEN SETTLE. Tokens are unknown until the response arrives, so admission
charges an estimate and a second row corrects it. Doing it the other way round
-- send, then account -- means N concurrent agents each admit themselves against
the same headroom and the account is over-drawn by N-1 calls before the first
one returns.

SIX HOURS, NOT FIVE. `budget.SESSION` is five hours because that is the unit an
operator plans in. A ration needs sittings that TILE the day exactly, or the
last one straddles the reset and can spend on both sides of it. Six hours is
the largest interval inside the operator's stated 5-6 hour window that divides
24 evenly, so four of them tile a day.
"""

from __future__ import annotations

import asyncio
import logging
import math
import os
import time
import uuid
from dataclasses import dataclass, replace
from typing import Any, Protocol

from swarmd.router.budget import (
    DAY,
    BudgetSpec,
    BudgetTracker,
    Limit,
    UsageRow,
    day_reset_at,
)

logger = logging.getLogger(__name__)

# --- constants -------------------------------------------------------------

SESSION_LEN = 6 * 3600.0
# Four sittings a day, so SESSION_LEN * SESSIONS_PER_DAY == DAY exactly. Derived
# rather than declared: the two figures disagreeing would silently ration
# against a day that is not 24 hours long.
SESSIONS_PER_DAY = round(DAY / SESSION_LEN)

# Fraction of a declared limit we are willing to spend. The same 0.9 the minute
# bucket uses (`quota.py` safety_margin) and for the same reason: the provider
# enforces with its clock, not ours, and the last request of a window can land
# inside their previous one. Kept literally equal so a future correction to one
# is an obvious lie about the other.
SAFETY = 0.9

# How far above the observed mean a reservation charges. Under-reserving is the
# expensive error -- it is what lets a batch of concurrent calls overshoot the
# ration by the shortfall -- and the settle row gives the 25% straight back if
# the call was ordinary.
ESTIMATE_HEADROOM = 1.25

# What a call is assumed to cost before this deployment has ever measured one.
# Matches `BudgetTracker.observed_tokens_per_request`'s default so a cold start
# and a warm one disagree about the number, not about where it came from.
DEFAULT_TOKENS_PER_REQUEST = 1_000

# Dead zone before a known reset in which nothing is admitted. Two minutes,
# because that is a generous bound on clock skew between us and a provider, and
# a request that we think lands after the reset but they think lands before it
# is charged to a day we believe is already empty.
GUARD_BAND_S = 120.0

# When a reservation with no settle row is written off. Ten minutes is several
# times the longest observed completion, so anything older belongs to a process
# that died mid-flight rather than to a call still in the air.
REAP_AFTER_S = 600.0

# How long a caller is told to wait when the journal cannot be written. Long
# enough that a permissions problem is not retried in a hot loop, short enough
# that fixing the permission is noticed within a minute.
UNWRITABLE_WAIT_S = 60.0


# --- what a call costs -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class Cost:
    """One call, in both dimensions. Requests and tokens are separate ceilings
    and the first one reached is what stops the call."""

    requests: int = 1
    tokens: int = 0


@dataclass(frozen=True, slots=True)
class SessionSlot:
    """Where `now` falls in the provider's day.

    ANATOMY: rolling
      True when the provider does not publish a reset instant. There is then no
      boundary to align to, so the "sitting" is the trailing SESSION_LEN and the
      "day" is the trailing 24 hours -- both of which drain continuously instead
      of refilling at once.
    """

    day_start: float
    slot_start: float
    slot_end: float
    slots_remaining: int
    reset_at: float
    rolling: bool

    @property
    def index(self) -> int:
        return SESSIONS_PER_DAY - self.slots_remaining


def session_slot(
    spec: BudgetSpec, now: float, *, sessions_per_day: int = SESSIONS_PER_DAY
) -> SessionSlot:
    """Cut the provider's day into sittings and say which one `now` is in.

    Boundaries are anchored on the provider's OWN reset instant, never on when
    this process started: two replicas that anchor differently compute different
    rations from the same journal and between them spend more than one.
    """
    reset_at = day_reset_at(spec, now)
    if spec.reset_kind == "rolling":
        return SessionSlot(
            day_start=now - DAY,
            slot_start=now - SESSION_LEN,
            slot_end=now + SESSION_LEN,
            slots_remaining=sessions_per_day,
            reset_at=reset_at,
            rolling=True,
        )
    day_start = reset_at - DAY
    length = DAY / sessions_per_day
    index = int((now - day_start) // length)
    index = max(0, min(sessions_per_day - 1, index))
    return SessionSlot(
        day_start=day_start,
        slot_start=day_start + index * length,
        slot_end=day_start + (index + 1) * length,
        slots_remaining=sessions_per_day - index,
        reset_at=reset_at,
        rolling=False,
    )


# --- the admission decision ------------------------------------------------


@dataclass(frozen=True, slots=True)
class Decision:
    """Whether this call may go, and if not, when to come back and why.

    The refusal fields are not decoration. A pause that says "waiting" and
    nothing else is indistinguishable from a hang, and the run may be waiting
    for hours: `reason`, `dimension`, `used` and `envelope` are what let the
    event stream say WHICH provider ran out of WHAT, and by how much.
    """

    admitted: bool
    wait_s: float = 0.0
    reason: str = ""
    dimension: str = ""
    used: int = 0
    envelope: int = 0
    resumes_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "admitted": self.admitted,
            "wait_s": round(self.wait_s, 3),
            "reason": self.reason,
            "dimension": self.dimension,
            "used": self.used,
            "envelope": self.envelope,
            "resumes_at": round(self.resumes_at, 3),
        }


ADMITTED = Decision(admitted=True)


def _totals(rows: list[UsageRow], since: float) -> tuple[int, int]:
    """(attempts, tokens) charged at or after `since`.

    ATTEMPTS, not requests, for the request dimension. A provider that counts a
    429'd call against the daily allowance -- and several do -- would otherwise
    see us overshoot by exactly the number of retries, which is the worst moment
    to be spending more than we think.
    """
    attempts = 0
    tokens = 0
    for row in rows:
        if row.ts < since or row.kind == "exhausted":
            continue
        attempts += row.attempts
        tokens += row.tokens
    return attempts, tokens


def _drain_wait(
    rows: list[UsageRow],
    *,
    since: float,
    window: float,
    used: int,
    limit: int,
    cost: int,
    now: float,
    tokens: bool,
) -> float:
    """When a rolling window will have aged out enough spend to fit this call.

    Walks the window's rows oldest-first and finds the first one whose departure
    leaves room. Returns the wait until that row falls out of the window.

    A rolling window is the only case that needs this: a wall-clock window has
    one answer, its reset instant, but a rolling one frees capacity continuously
    and sleeping to the full window length would waste almost all of it.
    """
    if cost > limit:
        # This call cannot fit the window however long we wait. Bounded rather
        # than infinite so the pause is still reported with an ETA an operator
        # can read, and so the next evaluation gets a chance to see a corrected
        # limit.
        return window
    ordered = sorted(
        (r for r in rows if r.ts >= since and r.kind != "exhausted"),
        key=lambda r: r.ts,
    )
    freed = 0
    for row in ordered:
        freed += row.tokens if tokens else row.attempts
        if used - freed + cost <= limit:
            return max(0.0, (row.ts + window) - now)
    return window


def evaluate(
    rows: list[UsageRow],
    spec: BudgetSpec,
    cost: Cost,
    *,
    now: float,
    slot: SessionSlot | None = None,
    sessions_per_day: int = SESSIONS_PER_DAY,
) -> Decision:
    """Whether this credential may spend `cost` right now.

    Pure: everything it needs is in `rows`, so the same function serves the
    in-process backend, the forecast, and a test with a fake clock and a
    hand-written journal.
    """
    slot = slot or session_slot(spec, now, sessions_per_day=sessions_per_day)

    # The provider's own word first. A 429 that named a daily metric outranks
    # every figure in the table (ADR-008), and it outranks them until the
    # instant IT gave rather than until we feel better about it.
    until = max(
        (r.until for r in rows if r.kind == "exhausted" and r.until > now),
        default=0.0,
    )
    if until:
        return Decision(
            admitted=False,
            wait_s=until - now,
            reason="observed_day_limit",
            dimension="requests",
            resumes_at=until,
        )

    daily = spec.limit_for("day")
    if daily is None or (daily.requests is None and daily.tokens is None):
        # No day to spread. Rate-kind providers (mistral) live entirely under
        # the minute bucket and their month cap, and rationing them would invent
        # a scarcity the provider does not have.
        return ADMITTED

    if not slot.rolling and slot.reset_at - now <= GUARD_BAND_S:
        return Decision(
            admitted=False,
            wait_s=slot.reset_at - now,
            reason="reset_guard_band",
            dimension="requests",
            resumes_at=slot.reset_at,
        )

    used_day_attempts, used_day_tokens = _totals(rows, slot.day_start)
    used_slot_attempts, used_slot_tokens = _totals(rows, slot.slot_start)

    refusals: list[Decision] = []
    dimensions = (
        ("requests", daily.requests, cost.requests, False),
        ("tokens", daily.tokens, cost.tokens, True),
    )
    for name, declared, want, is_tokens in dimensions:
        if declared is None:
            continue
        effective = int(declared * SAFETY)
        used_day = used_day_tokens if is_tokens else used_day_attempts
        used_slot = used_slot_tokens if is_tokens else used_slot_attempts

        # Earlier sittings' spend leaves the numerator; this sitting's own spend
        # does not, or a sitting would shrink its own ration as it used it and
        # never reach the slice it was promised.
        carried = max(0, used_day - used_slot)
        envelope = max(0, (effective - carried) // max(1, slot.slots_remaining))

        if used_day + want > effective:
            wait = (
                _drain_wait(
                    rows, since=slot.day_start, window=DAY, used=used_day,
                    limit=effective, cost=want, now=now, tokens=is_tokens,
                )
                if slot.rolling
                else slot.reset_at - now
            )
            refusals.append(
                Decision(
                    admitted=False, wait_s=max(0.0, wait), reason="day_exhausted",
                    dimension=name, used=used_day, envelope=effective,
                    resumes_at=now + max(0.0, wait),
                )
            )
            continue
        if used_slot + want > envelope:
            wait = (
                _drain_wait(
                    rows, since=slot.slot_start, window=SESSION_LEN, used=used_slot,
                    limit=envelope, cost=want, now=now, tokens=is_tokens,
                )
                if slot.rolling
                else slot.slot_end - now
            )
            refusals.append(
                Decision(
                    admitted=False, wait_s=max(0.0, wait), reason="session_ration",
                    dimension=name, used=used_slot, envelope=envelope,
                    resumes_at=now + max(0.0, wait),
                )
            )

    if not refusals:
        return ADMITTED
    # EVERY refusal must clear, so the latest one is the answer. Reporting the
    # soonest would wake the run into a second pause it could have predicted.
    return max(refusals, key=lambda d: d.wait_s)


# --- reservations ----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Grant:
    """The receipt for an admitted call, or the refusal that replaced it."""

    rid: str
    provider: str
    credential: str
    model: str
    cost: Cost
    decision: Decision

    @property
    def admitted(self) -> bool:
        return self.decision.admitted

    @property
    def wait_s(self) -> float:
        return self.decision.wait_s


class RationBackend(Protocol):
    """Where the ration is evaluated: this process, or the whole cluster.

    Mirrors `QuotaBackend` deliberately. Admission and reservation are ONE
    operation in both implementations, because a check followed by a separate
    write is a race that hundreds of agents will find.
    """

    async def admit(
        self, spec: BudgetSpec, credential: str, model: str, cost: Cost, now: float
    ) -> Grant: ...

    async def settle(
        self, grant: Grant, *, requests: int, tokens: int, attempts: int
    ) -> None: ...

    def rows(
        self, spec: BudgetSpec, credential: str, model: str, since: float
    ) -> list[UsageRow]: ...


class InProcessRation:
    """Rations derived from the local journal. Correct with one process per key.

    The lock is what makes reserve atomic against the other agents in this run.
    It is not what makes it atomic against another PROCESS sharing the same
    credential -- that needs `RedisRation`, for exactly the reason
    `quota.py` gives about three pods each politely holding to 45 RPM.
    """

    def __init__(
        self,
        tracker: BudgetTracker,
        *,
        sessions_per_day: int = SESSIONS_PER_DAY,
    ) -> None:
        self.tracker = tracker
        self.sessions_per_day = sessions_per_day
        self._lock = asyncio.Lock()

    def rows(
        self, spec: BudgetSpec, credential: str, model: str, since: float
    ) -> list[UsageRow]:
        return self.tracker.journal.rows_for(
            provider=spec.provider,
            credential=credential,
            # Only when the provider meters per model. Filtering unconditionally
            # would give a run that rotates models a fresh ration per rotation,
            # which is a way to spend a day in an afternoon.
            model=model if spec.per_model else "",
            since=since,
        )

    async def admit(
        self, spec: BudgetSpec, credential: str, model: str, cost: Cost, now: float
    ) -> Grant:
        async with self._lock:
            slot = session_slot(spec, now, sessions_per_day=self.sessions_per_day)
            rows = self.rows(spec, credential, model, slot.day_start)
            decision = evaluate(
                rows, spec, cost, now=now, slot=slot,
                sessions_per_day=self.sessions_per_day,
            )
            if not decision.admitted:
                return Grant("", spec.provider, credential, model, cost, decision)
            rid = uuid.uuid4().hex[:12]
            self.tracker.journal.record(
                provider=spec.provider,
                credential=credential,
                model=model,
                requests=cost.requests,
                tokens=cost.tokens,
                ts=now,
                kind="reserve",
                rid=rid,
                attempts=cost.requests,
            )
            if not self.tracker.journal.last_write_ok:
                # FAIL CLOSED. An unwritable journal means a restart would not
                # see this spend, and a ration that forgets is a ration that
                # hands out the same day twice. Waiting is the cheap error.
                return Grant(
                    "", spec.provider, credential, model, cost,
                    Decision(
                        admitted=False, wait_s=UNWRITABLE_WAIT_S,
                        reason="journal_unwritable", dimension="requests",
                        resumes_at=now + UNWRITABLE_WAIT_S,
                    ),
                )
            return Grant(rid, spec.provider, credential, model, cost, decision)

    async def settle(
        self, grant: Grant, *, requests: int, tokens: int, attempts: int
    ) -> None:
        if not grant.rid:
            return
        async with self._lock:
            self.tracker.journal.record(
                provider=grant.provider,
                credential=grant.credential,
                model=grant.model,
                requests=requests,
                tokens=tokens,
                kind="settle",
                rid=grant.rid,
                attempts=attempts,
            )


# Admission and reservation in one round trip, for the same reason the quota
# bucket is a script: check-then-write across two calls is a race, and with
# replicas sharing a credential it is not a rare one.
#
# The script is deliberately arithmetic only. Deciding WHEN to come back needs
# the rows themselves, and that read happens outside the script on the refusal
# path -- which is cold, and where an extra round trip costs nothing.
_REDIS_RESERVE = """
local key   = KEYS[1]
local now         = tonumber(ARGV[1])
local day_start   = tonumber(ARGV[2])
local slot_start  = tonumber(ARGV[3])
local slots_left  = tonumber(ARGV[4])
local eff_req     = tonumber(ARGV[5])   -- -1 when undeclared
local eff_tok     = tonumber(ARGV[6])
local cost_req    = tonumber(ARGV[7])
local cost_tok    = tonumber(ARGV[8])
local ttl         = tonumber(ARGV[9])
local member      = ARGV[10]

redis.call('ZREMRANGEBYSCORE', key, '-inf', day_start - 1)

local day_req, day_tok, slot_req, slot_tok = 0, 0, 0, 0
local rows = redis.call('ZRANGEBYSCORE', key, day_start, '+inf', 'WITHSCORES')
for i = 1, #rows, 2 do
  local parts = {}
  for field in string.gmatch(rows[i], '([^|]*)') do parts[#parts + 1] = field end
  local attempts = tonumber(parts[3]) or 0
  local tokens   = tonumber(parts[4]) or 0
  local score    = tonumber(rows[i + 1])
  day_req = day_req + attempts
  day_tok = day_tok + tokens
  if score >= slot_start then
    slot_req = slot_req + attempts
    slot_tok = slot_tok + tokens
  end
end

local function fits(eff, day_used, slot_used, want)
  if eff < 0 then return true end
  if day_used + want > eff then return false end
  local carried = day_used - slot_used
  if carried < 0 then carried = 0 end
  local envelope = math.floor((eff - carried) / slots_left)
  return slot_used + want <= envelope
end

local ok = fits(eff_req, day_req, slot_req, cost_req)
           and fits(eff_tok, day_tok, slot_tok, cost_tok)
if ok then
  redis.call('ZADD', key, now, member)
  redis.call('EXPIRE', key, ttl)
end
return {ok and 1 or 0, day_req, day_tok, slot_req, slot_tok}
"""


class RedisRation:
    """One ration shared by every replica holding the same credential.

    Same failure policy as `RedisQuota`, and for the same reason: fail-open
    would let every replica spend a full day's ration during an outage, which is
    precisely the over-spend this class exists to prevent, and fail-fully-closed
    would turn a Redis blip into an outage of a system whose point is degrading
    gracefully. Degraded means each replica rations against its LOCAL journal at
    one Nth of the declared limit, N being what the operator declared in
    SWARMD_REPLICAS.

    Rows are mirrored to the local journal either way, so `swarmd providers
    budget` and the cost report keep working while Redis is down.
    """

    def __init__(
        self,
        url: str,
        tracker: BudgetTracker,
        *,
        namespace: str = "swarmd:ration",
        sessions_per_day: int = SESSIONS_PER_DAY,
        replicas: int | None = None,
    ) -> None:
        self.url = url
        self.tracker = tracker
        self.namespace = namespace
        self.sessions_per_day = sessions_per_day
        self.replicas = replicas or max(1, int(os.environ.get("SWARMD_REPLICAS", "1")))
        self._client: Any | None = None
        self._script: Any | None = None
        self._local = InProcessRation(tracker, sessions_per_day=sessions_per_day)
        self._degraded = False

    @property
    def degraded(self) -> bool:
        return self._degraded

    def rows(
        self, spec: BudgetSpec, credential: str, model: str, since: float
    ) -> list[UsageRow]:
        return self._local.rows(spec, credential, model, since)

    def _key(self, spec: BudgetSpec, credential: str, model: str) -> str:
        suffix = f":{model}" if spec.per_model and model else ""
        return f"{self.namespace}:{credential}{suffix}"

    def _shared(self, spec: BudgetSpec) -> BudgetSpec:
        """The spec a single replica may ration against with Redis unreachable.

        Dividing rather than guessing: if SWARMD_REPLICAS is honest the account
        is respected, and if it is understated the account is over-driven by
        exactly that ratio -- which a 429 then corrects, and which is a smaller
        error than N replicas each believing they own the whole day.
        """
        if self.replicas <= 1:
            return spec
        limits = tuple(
            Limit(
                window=limit.window,
                requests=(
                    None if limit.requests is None
                    else max(1, limit.requests // self.replicas)
                ),
                tokens=(
                    None if limit.tokens is None
                    else max(1, limit.tokens // self.replicas)
                ),
            )
            for limit in spec.limits
        )
        return replace(spec, limits=limits)

    async def _connect(self) -> Any:
        if self._client is None:
            import redis.asyncio as redis  # deferred: optional dependency

            self._client = redis.from_url(self.url, decode_responses=True)
            self._script = self._client.register_script(_REDIS_RESERVE)
        return self._client

    async def admit(
        self, spec: BudgetSpec, credential: str, model: str, cost: Cost, now: float
    ) -> Grant:
        daily = spec.limit_for("day")
        if daily is None or (daily.requests is None and daily.tokens is None):
            return Grant("", spec.provider, credential, model, cost, ADMITTED)
        try:
            client = await self._connect()
            server_s, server_us = await client.time()
            # The REDIS clock, not ours. Replica clocks drift, and two replicas
            # that disagree about which sitting it is compute two different
            # rations from the same rows.
            now = float(server_s) + float(server_us) / 1_000_000
            slot = session_slot(spec, now, sessions_per_day=self.sessions_per_day)
            if not slot.rolling and slot.reset_at - now <= GUARD_BAND_S:
                return Grant(
                    "", spec.provider, credential, model, cost,
                    Decision(
                        admitted=False, wait_s=slot.reset_at - now,
                        reason="reset_guard_band", dimension="requests",
                        resumes_at=slot.reset_at,
                    ),
                )
            rid = uuid.uuid4().hex[:12]
            assert self._script is not None
            raw = await self._script(
                keys=[self._key(spec, credential, model)],
                args=[
                    now, slot.day_start, slot.slot_start, slot.slots_remaining,
                    int(daily.requests * SAFETY) if daily.requests else -1,
                    int(daily.tokens * SAFETY) if daily.tokens else -1,
                    cost.requests, cost.tokens,
                    int(DAY * 1.1),
                    f"{rid}|reserve|{cost.requests}|{cost.tokens}",
                ],
            )
            if self._degraded:
                logger.info("ration backend recovered: redis reachable again")
                self._degraded = False
            if int(raw[0]) != 1:
                # Refused. The rows are needed to say WHEN, and this path is
                # cold, so the second read is free where it matters.
                rows = self.rows(spec, credential, model, slot.day_start)
                decision = evaluate(
                    rows, spec, cost, now=now, slot=slot,
                    sessions_per_day=self.sessions_per_day,
                )
                if decision.admitted:
                    # Redis refused where the local journal would not: another
                    # replica spent the slice. Wait for the boundary rather than
                    # trusting the local view, which is the smaller of the two.
                    decision = Decision(
                        admitted=False,
                        wait_s=max(0.0, slot.slot_end - now),
                        reason="session_ration",
                        dimension="requests",
                        used=int(raw[3]),
                        envelope=int(raw[1]),
                        resumes_at=slot.slot_end,
                    )
                return Grant("", spec.provider, credential, model, cost, decision)
            self.tracker.journal.record(
                provider=spec.provider, credential=credential, model=model,
                requests=cost.requests, tokens=cost.tokens, ts=now,
                kind="reserve", rid=rid, attempts=cost.requests,
            )
            return Grant(rid, spec.provider, credential, model, cost, ADMITTED)
        except Exception as exc:  # noqa: BLE001 - any Redis failure degrades
            if not self._degraded:
                logger.warning(
                    "ration backend degraded to the local journal at 1/%d of "
                    "each limit: %s",
                    self.replicas, exc,
                )
                self._degraded = True
            return await self._local.admit(
                self._shared(spec), credential, model, cost, now
            )

    async def settle(
        self, grant: Grant, *, requests: int, tokens: int, attempts: int
    ) -> None:
        if not grant.rid:
            return
        await self._local.settle(
            grant, requests=requests, tokens=tokens, attempts=attempts
        )
        if self._degraded:
            return
        try:
            client = await self._connect()
            server_s, server_us = await client.time()
            now = float(server_s) + float(server_us) / 1_000_000
            spec = self.tracker.budgets.get(grant.provider)
            key = (
                self._key(spec, grant.credential, grant.model) if spec
                else f"{self.namespace}:{grant.credential}"
            )
            await client.zadd(
                key, {f"{grant.rid}|settle|{attempts}|{tokens}": now}
            )
        except Exception:
            # Deliberately silent past a debug line: failing to settle leaves
            # the reservation standing, which over-states our spend. That is the
            # direction that stops early rather than the one that overdraws.
            logger.debug("ration settle not mirrored to redis", exc_info=True)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


# --- the front door --------------------------------------------------------


class Ration:
    """The gate the pool asks before every call.

    Holds the estimation policy, the reaper, and the forecast; the arithmetic
    lives in `evaluate` and the storage in a backend, so a test can exercise the
    maths with a hand-written journal and no event loop.
    """

    def __init__(
        self,
        tracker: BudgetTracker,
        *,
        backend: RationBackend | None = None,
        sessions_per_day: int = SESSIONS_PER_DAY,
    ) -> None:
        self.tracker = tracker
        self.sessions_per_day = sessions_per_day
        self.backend: RationBackend = backend or InProcessRation(
            tracker, sessions_per_day=sessions_per_day
        )
        self._last_reap = 0.0

    # -- estimation -----------------------------------------------------

    def estimate_tokens(self, provider: str, *, now: float | None = None) -> int:
        """What to charge a call before it has happened.

        Measured, not assumed, for the reason `observed_tokens_per_request`
        gives: the figure depends on prompt size, which depends on the schema
        hints and retrieved skills, which change.
        """
        observed = self.tracker.observed_tokens_per_request(
            provider, default=DEFAULT_TOKENS_PER_REQUEST, now=now
        )
        return math.ceil(observed * ESTIMATE_HEADROOM)

    # -- the gate -------------------------------------------------------

    async def reserve(
        self,
        *,
        provider: str,
        credential: str,
        model: str = "",
        now: float | None = None,
    ) -> Grant:
        now = now if now is not None else time.time()
        spec = self.tracker.budgets.get(provider)
        if spec is None:
            # A provider with no declared budget is not rationed. Inventing one
            # would be a limit with no provenance, which is the thing this
            # module's docstring says every number here must have.
            return Grant("", provider, credential, model, Cost(), ADMITTED)
        self.reap(now=now)
        cost = Cost(requests=1, tokens=self.estimate_tokens(provider, now=now))
        return await self.backend.admit(spec, credential, model, cost, now)

    async def settle(self, grant: Grant, *, tokens: int, outcome: str = "ok") -> None:
        """Correct the reservation once the call's real cost is known.

        The three outcomes charge differently because the provider does:

          ok              tokens move to the actual figure; the request stands.
          rate_limited    the token estimate comes back, the REQUEST does not:
                          a 429 means the provider saw the call and, on several
                          of them, counted it.
          error           both come back, but the attempt stays charged --
                          something was sent, and the ration must know it.
        """
        if not grant.rid:
            return
        if outcome == "ok":
            await self.backend.settle(
                grant, requests=0, tokens=tokens - grant.cost.tokens, attempts=0
            )
        elif outcome == "rate_limited":
            await self.backend.settle(
                grant, requests=0, tokens=-grant.cost.tokens, attempts=0
            )
        else:
            await self.backend.settle(
                grant, requests=-grant.cost.requests,
                tokens=-grant.cost.tokens, attempts=0,
            )

    async def release(self, grant: Grant) -> None:
        """Give a reservation back untouched. Nothing was sent.

        Used when a later gate -- the minute bucket, the cost ceiling -- refuses
        a call the ration already admitted. Without it, every bucket wait would
        quietly burn a slice of the day's allowance.
        """
        if not grant.rid:
            return
        await self.backend.settle(
            grant, requests=-grant.cost.requests,
            tokens=-grant.cost.tokens, attempts=-grant.cost.requests,
        )

    # -- housekeeping ---------------------------------------------------

    def reap(self, *, now: float | None = None) -> int:
        """Write off reservations whose settle row never arrived.

        A process that dies between reserve and settle leaves its estimate
        charged forever, and after enough crashes the ration is spent by calls
        that never happened. The write-off returns the TOKEN estimate and the
        success, and leaves the ATTEMPT charged: the call almost certainly left
        the machine, so pretending it did not is the one direction that could
        over-spend the account.

        Rate-limited to once a minute -- it walks the journal, and the pool
        calls it before every request.
        """
        now = now if now is not None else time.time()
        if now - self._last_reap < 60.0:
            return 0
        self._last_reap = now
        settled: set[str] = set()
        pending: dict[str, UsageRow] = {}
        for row in self.tracker.journal.rows_since(now - DAY):
            if not row.rid:
                continue
            if row.kind == "settle":
                settled.add(row.rid)
            elif row.kind == "reserve" and row.ts <= now - REAP_AFTER_S:
                pending[row.rid] = row
        stranded = [row for rid, row in pending.items() if rid not in settled]
        for row in stranded:
            self.tracker.journal.record(
                provider=row.provider, credential=row.credential, model=row.model,
                requests=-row.requests, tokens=-row.tokens, ts=now,
                kind="settle", rid=row.rid, attempts=0,
            )
        if stranded:
            logger.info(
                "released %d stranded reservation(s) older than %.0fs",
                len(stranded), REAP_AFTER_S,
            )
        return len(stranded)

    # -- introspection --------------------------------------------------

    def envelope(
        self,
        provider: str,
        *,
        credential: str = "",
        model: str = "",
        now: float | None = None,
    ) -> dict[str, Any]:
        """This credential's current slice, in both dimensions.

        The shape the CLI and the dashboard both read, so the number an operator
        sees on a pause banner is the number the gate actually applied.
        """
        now = now if now is not None else time.time()
        spec = self.tracker.budgets.get(provider)
        if spec is None:
            return {"provider": provider, "rationed": False}
        slot = session_slot(spec, now, sessions_per_day=self.sessions_per_day)
        rows = self.backend.rows(spec, credential, model, slot.day_start)
        daily = spec.limit_for("day")
        used_day_req, used_day_tok = _totals(rows, slot.day_start)
        used_slot_req, used_slot_tok = _totals(rows, slot.slot_start)
        out: dict[str, Any] = {
            "provider": provider,
            "credential": credential,
            "model": model if spec.per_model else "",
            "rationed": daily is not None,
            "reset_kind": spec.reset_kind,
            "session": [slot.index + 1, self.sessions_per_day],
            "slot_end": round(slot.slot_end, 3),
            "reset_at": round(slot.reset_at, 3),
            "used_requests": used_slot_req,
            "used_tokens": used_slot_tok,
            "used_today_requests": used_day_req,
            "used_today_tokens": used_day_tok,
        }
        if daily is not None:
            for name, declared, used_day, used_slot in (
                ("requests", daily.requests, used_day_req, used_slot_req),
                ("tokens", daily.tokens, used_day_tok, used_slot_tok),
            ):
                if declared is None:
                    out[f"envelope_{name}"] = None
                    continue
                effective = int(declared * SAFETY)
                carried = max(0, used_day - used_slot)
                out[f"envelope_{name}"] = max(
                    0, (effective - carried) // max(1, slot.slots_remaining)
                )
        return out

    def session_capacity(
        self, *, now: float | None = None, credentials: dict[str, list[str]] | None = None
    ) -> int:
        """Calls the whole pool can still make before the next boundary.

        Summed over providers because the pool routes across them: a run is not
        paused by one provider running out, only by all of them. Rate-kind
        providers are EXCLUDED for the reason `remaining_today` excludes them --
        a per-minute rate multiplied out is not a plannable allowance, and
        counting it made every preflight answer "fits".
        """
        now = now if now is not None else time.time()
        total = 0
        for name, spec in self.tracker.budgets.items():
            daily = spec.limit_for("day")
            if daily is None or spec.kind != "quota":
                continue
            if self.tracker.blocked(name, now=now):
                continue
            for credential in (credentials or {}).get(name, [""]):
                env = self.envelope(name, credential=credential, now=now)
                by_requests = env.get("envelope_requests")
                by_tokens = env.get("envelope_tokens")
                headroom = []
                if by_requests is not None:
                    headroom.append(max(0, by_requests - env["used_requests"]))
                if by_tokens is not None:
                    per_call = max(1, self.estimate_tokens(name, now=now))
                    headroom.append(
                        max(0, (by_tokens - env["used_tokens"]) // per_call)
                    )
                if headroom:
                    total += min(headroom)
        return total

    def forecast(
        self,
        estimated_calls: int,
        *,
        now: float | None = None,
        horizon_days: int = 7,
        credentials: dict[str, list[str]] | None = None,
    ) -> dict[str, Any]:
        """Session by session, where this run's calls land and when it finishes.

        Replaces a yes/no verdict, which was the wrong shape once a run pauses
        instead of stopping: "does not fit" and "finishes at 18:40 after one
        pause" are the same answer to `fits`, and only one of them is something
        an operator can act on.

        The first sitting uses the REAL headroom; later ones assume a full slice,
        which is the best that can be said before the plan exists. Rate-kind
        providers are excluded throughout, so the projection is a floor.
        """
        now = now if now is not None else time.time()
        remaining = max(0, estimated_calls)
        timeline: list[dict[str, Any]] = []
        first_pause_at: float | None = None
        cursor = now
        # A full sitting's capacity with nothing spent yet: what every future
        # window is assumed to carry.
        fresh = self._fresh_session_capacity(now=now, credentials=credentials)
        for step in range(horizon_days * SESSIONS_PER_DAY):
            capacity = (
                self.session_capacity(now=now, credentials=credentials)
                if step == 0 else fresh
            )
            end = self._next_boundary(cursor)
            planned = min(remaining, capacity)
            remaining -= planned
            timeline.append(
                {
                    "start": round(cursor, 3),
                    "end": round(end, 3),
                    "capacity": capacity,
                    "planned": planned,
                }
            )
            if remaining > 0 and first_pause_at is None:
                first_pause_at = end
            if remaining <= 0:
                break
            cursor = end
        used_windows = [w for w in timeline if w["planned"] > 0]
        if remaining > 0:
            verdict = "exceeds_horizon"
        elif len(used_windows) <= 1 and first_pause_at is None:
            verdict = "fits_this_session"
        elif timeline[-1]["end"] <= now + DAY:
            verdict = "fits_today_with_pauses"
        else:
            verdict = "spans_days"
        return {
            "verdict": verdict,
            "estimated_calls": estimated_calls,
            "sessions_needed": max(1, len(used_windows)),
            "expected_pauses": max(0, len(used_windows) - 1),
            "first_pause_at": (
                round(first_pause_at, 3) if first_pause_at is not None else None
            ),
            "projected_finish": round(timeline[-1]["end"], 3) if timeline else None,
            "session_capacity": timeline[0]["capacity"] if timeline else 0,
            "timeline": timeline,
        }

    def _fresh_session_capacity(
        self, *, now: float, credentials: dict[str, list[str]] | None
    ) -> int:
        total = 0
        for name, spec in self.tracker.budgets.items():
            daily = spec.limit_for("day")
            if daily is None or spec.kind != "quota":
                continue
            keys = (credentials or {}).get(name, [""])
            headroom = []
            if daily.requests is not None:
                headroom.append(int(daily.requests * SAFETY) // self.sessions_per_day)
            if daily.tokens is not None:
                per_call = max(1, self.estimate_tokens(name, now=now))
                headroom.append(
                    int(daily.tokens * SAFETY) // self.sessions_per_day // per_call
                )
            if headroom:
                total += min(headroom) * max(1, len(keys))
        return total

    def _next_boundary(self, now: float) -> float:
        """The soonest sitting boundary across every rationed provider.

        The soonest, because that is when the picture changes: one provider's
        slice refilling is enough to restart a paused run.
        """
        boundaries = []
        for spec in self.tracker.budgets.values():
            if spec.limit_for("day") is None:
                continue
            slot = session_slot(spec, now, sessions_per_day=self.sessions_per_day)
            boundaries.append(
                now + SESSION_LEN if slot.rolling else slot.slot_end
            )
        return min(boundaries, default=now + SESSION_LEN)


def build_ration(
    tracker: BudgetTracker, redis_url: str | None = None
) -> Ration:
    """Pick a backend. Redis when a URL is configured, local otherwise.

    Same switch as `build_quota`, and deliberately the same switch: a deployment
    where the minute bucket is shared and the daily ration is not would hold the
    rate correctly and still over-spend the day N times over.
    """
    if redis_url:
        return Ration(tracker, backend=RedisRation(redis_url, tracker))
    return Ration(tracker)
