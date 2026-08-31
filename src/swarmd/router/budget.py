"""Long-window budgets: what is left this hour, this session, this week, this month.

The quota module (`router/quota.py`) answers "may I send a request in the next
second?" with a token bucket. That is the right shape for a per-minute limit and
the wrong shape for everything longer, for two reasons:

  A bucket holds no history. It cannot answer "how much of today's 1,500
  requests have we used", because it never knew there was a day.

  A bucket lives in memory. Restart the process and a per-minute bucket loses
  nothing worth having; a per-month budget loses the whole month.

So this module tracks usage in WINDOWS, and derives every figure by summing an
append-only journal rather than by incrementing counters -- the same decision
ADR-007 makes for cost, for the same reason. A counter that drifts from what
happened reports a number nobody can check, and here the number decides whether
a run is allowed to start.

WHY THESE WINDOWS. Minute and hour because providers enforce them. Day because
most free tiers reset daily. **Session (5 hours)** because that is the unit of
work an operator actually plans -- "can I run this afternoon" is not answerable
from a per-minute rate. Week and month because a finite grant has to last one,
and knowing on day three that the month is already spent is the difference
between rationing and discovering an outage.

THREE KINDS OF LIMIT, and conflating them is how a plan goes wrong:

  RATE       replenishes continuously (Cerebras' token bucket, Groq's RPM).
             Exhausting it costs latency, never capacity.
  QUOTA      resets on a schedule (Google's RPD at midnight Pacific). Exhausting
             it costs the rest of the period.
  GRANT      a finite pool that never replenishes and expires (NVIDIA's ~1,000
             credits, 30 days). Exhausting it costs the provider, permanently.

A GRANT is the one that punishes optimism. Spending it first because it is
"free" means it is gone in week one and the month has no burst capacity left.
That is why grant-backed providers sort behind rate-backed ones even though
both cost $0 -- see `TIER_RANK` in `pool.py`.

EVERY NUMBER HERE IS A HYPOTHESIS. Published limits disagree with observed
behaviour often enough that ADR-008 makes observation the authority. These
declared budgets carry the source and the date they were checked, and the pool
narrows them from real 429s. When the two disagree, the observation wins and
the declared figure is the thing that was wrong.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import threading
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger(__name__)

# --- windows ---------------------------------------------------------------

MINUTE = 60.0
HOUR = 3600.0
SESSION = 5 * HOUR          # the unit an operator plans in
DAY = 24 * HOUR
WEEK = 7 * DAY
MONTH = 30 * DAY            # 30 days, not a calendar month: grants expire on
                            # elapsed days, and "a month" that varies by 10%
                            # makes two runs incomparable.

WINDOWS: dict[str, float] = {
    "minute": MINUTE,
    "hour": HOUR,
    "session": SESSION,
    "day": DAY,
    "week": WEEK,
    "month": MONTH,
}

# Ordered longest-first for reporting: the binding constraint is usually the
# long one, and an operator reading top-down should meet it first.
WINDOW_ORDER = ("month", "week", "day", "session", "hour", "minute")

# Windows whose exhaustion means the provider has nothing left to give on any
# timescale a call can wait for. These are the ones that should remove a
# provider from routing and zero its contribution to "what is left today".
#
# The rest -- session, hour, minute -- refill on their own. Treating them the
# same way was a real and expensive bug: one busy minute made `blocked` report
# "minute budget exhausted", `remaining_today` skipped the provider entirely,
# and a preflight announced "0 left today" against 940 genuinely available
# requests. The run then proceeded on that false warning and failed.
DAY_SCALE_WINDOWS = ("month", "week", "day")
SHORT_WINDOWS = ("session", "hour", "minute")


def _pacific_offset(now: datetime) -> timedelta:
    """US Pacific offset without a tzdata dependency.

    Daylight saving in the US runs from the second Sunday in March to the first
    Sunday in November. Worth doing properly rather than hardcoding -8: an hour
    of error at the reset boundary is an hour in which the system believes it
    has a fresh daily quota and does not.
    """
    year = now.year

    def nth_sunday(month: int, nth: int) -> datetime:
        first = datetime(year, month, 1, tzinfo=UTC)
        # weekday(): Monday=0 .. Sunday=6
        days_ahead = (6 - first.weekday()) % 7
        return first + timedelta(days=days_ahead + 7 * (nth - 1))

    dst_start = nth_sunday(3, 2) + timedelta(hours=10)   # 2am PST = 10:00 UTC
    dst_end = nth_sunday(11, 1) + timedelta(hours=9)     # 2am PDT = 09:00 UTC
    return timedelta(hours=-7) if dst_start <= now < dst_end else timedelta(hours=-8)


def next_pacific_midnight(now_ts: float) -> float:
    """When a Google-style daily quota resets, as a unix timestamp."""
    now = datetime.fromtimestamp(now_ts, tz=UTC)
    offset = _pacific_offset(now)
    local = now + offset
    tomorrow = (local + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return (tomorrow - offset).timestamp()


def next_utc_midnight(now_ts: float) -> float:
    """When an OpenRouter-style daily quota resets, as a unix timestamp.

    A second function rather than a parameterised one because the two resets
    are different CLAIMS, each with its own provenance: Google documents
    midnight Pacific, OpenRouter documents midnight UTC, and a shared helper
    taking an offset would let a future edit change both by editing one.
    """
    now = datetime.fromtimestamp(now_ts, tz=UTC)
    tomorrow = (now + timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return tomorrow.timestamp()


# --- declared limits -------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Limit:
    """One ceiling on one window.

    `requests` and `tokens` are separate because providers enforce them
    separately and the first one reached is what stops the call. A budget that
    tracked only requests would report headroom while a token limit was already
    blocking.
    """

    window: str
    requests: int | None = None
    tokens: int | None = None


@dataclass(frozen=True, slots=True)
class BudgetSpec:
    """What a provider says it will give us, and where that claim came from.

    ANATOMY: kind
      "rate" | "quota" | "grant" -- see the module docstring. Decides what
      running out MEANS, which is what an operator needs to know before they
      decide whether to wait.

    ANATOMY: source / checked
      A URL and a date. A limit with no provenance cannot be re-verified when
      it stops matching reality, and these change without announcement.

    ANATOMY: resets_at_pacific_midnight
      Google's daily quota resets on a wall clock, not on a rolling window.
      Treating it as rolling under-uses it all morning and over-commits at
      night.

    ANATOMY: reset
      "rolling" | "pacific-midnight" | "utc-midnight". Generalises the older
      boolean, which could only express Google. It decides where a day BEGINS,
      and therefore how much of the day a session ration may claim.

      UNKNOWN RESETS ARE TREATED AS ROLLING, and that is the conservative
      direction rather than the lazy one: if a provider really resets on a wall
      clock, rolling accounting under-uses it for at most a day; if a rolling
      window is mistaken for a wall-clock one, the reset that was supposed to
      refill the bucket never happens and we double-spend it.

    ANATOMY: per_model
      True when the provider meters each model separately (Groq publishes a
      per-model TPD). With it set, rations are evaluated per (credential,
      model) rather than per credential, because summing two independently
      metered models reports one exhausted budget where there are two half-full
      ones.

    ANATOMY: grant_total / grant_expires_days
      For finite pools. `grant_total` is the whole allowance, not a per-window
      figure, and it never refills.
    """

    provider: str
    kind: str = "rate"
    limits: tuple[Limit, ...] = ()
    resets_at_pacific_midnight: bool = False
    reset: str = ""
    per_model: bool = False
    grant_total: int | None = None
    grant_expires_days: int | None = None
    source: str = ""
    checked: str = ""
    note: str = ""

    def limit_for(self, window: str) -> Limit | None:
        for limit in self.limits:
            if limit.window == window:
                return limit
        return None

    @property
    def reset_kind(self) -> str:
        """Where the day starts, with the old boolean still honoured.

        The boolean predates the field and is still what several call sites and
        tests construct specs with. Deriving from it rather than deleting it
        keeps one statement of the fact instead of two that can disagree.
        """
        if self.reset:
            return self.reset
        return "pacific-midnight" if self.resets_at_pacific_midnight else "rolling"


def day_reset_at(spec: BudgetSpec, now_ts: float) -> float:
    """When this provider's day next rolls over, as a unix timestamp.

    Rolling providers have no reset instant, so they get one DAY from now: a
    caller waiting on "the day" of a rolling window is really waiting for the
    oldest spend to age out, and a full day is the bound on that.
    """
    kind = spec.reset_kind
    if kind == "pacific-midnight":
        return next_pacific_midnight(now_ts)
    if kind == "utc-midnight":
        return next_utc_midnight(now_ts)
    return now_ts + DAY


# --- what the provider tells us on the way past ----------------------------
#
# A reset further out than this is a LONG window -- a day, or the remainder of
# one -- rather than the per-minute bucket. An hour because no provider here
# publishes a window between a minute and a day, so anything past an hour is
# unambiguously the long one, and mistaking a minute for a day would park a run
# for hours over a bucket that refills in seconds.
LONG_WINDOW_S = HOUR

_DURATION_UNITS = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}


def parse_duration(raw: str) -> float | None:
    """Seconds from a rate-limit header, in every shape providers send one.

    Groq answers `x-ratelimit-reset-tokens` with "2m59.56s", OpenRouter with a
    bare "7.66", and `retry-after` is documented as an integer. One parser
    rather than three because the alternative is three call sites each handling
    two of the three shapes and silently returning None for the third -- and a
    None here is a limit we do not learn.
    """
    text = str(raw).strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    number = ""
    unit = ""
    matched = False
    for char in text + "\0":
        if char.isdigit() or char == ".":
            if unit:
                total += _flush(number, unit)
                matched = True
                number, unit = "", ""
            number += char
        elif char.isalpha():
            unit += char
        else:
            break
    if number and unit:
        total += _flush(number, unit)
        matched = True
    return total if matched else None


def _flush(number: str, unit: str) -> float:
    scale = _DURATION_UNITS.get(unit)
    if scale is None:
        raise ValueError(f"unknown duration unit {unit!r}")
    return float(number) * scale


@dataclass(frozen=True, slots=True)
class RateHeaders:
    """What one response said about the allowance behind it.

    Every field is optional because every field is optional in practice:
    providers implement different subsets of the same de-facto header set, and
    a parser that required all six would learn nothing from any of them.
    """

    limit_requests: int | None = None
    remaining_requests: int | None = None
    reset_requests_s: float | None = None
    limit_tokens: int | None = None
    remaining_tokens: int | None = None
    reset_tokens_s: float | None = None

    def __bool__(self) -> bool:
        return any(
            value is not None
            for value in (
                self.limit_requests, self.remaining_requests, self.reset_requests_s,
                self.limit_tokens, self.remaining_tokens, self.reset_tokens_s,
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "limit_requests": self.limit_requests,
            "remaining_requests": self.remaining_requests,
            "reset_requests_s": self.reset_requests_s,
            "limit_tokens": self.limit_tokens,
            "remaining_tokens": self.remaining_tokens,
            "reset_tokens_s": self.reset_tokens_s,
        }


def parse_rate_headers(headers: Any) -> RateHeaders:
    """Read the `x-ratelimit-*` family off any response, 429 or not.

    Deliberately total: an unparseable or absent header becomes None rather
    than an exception. These are hints on a hot path, and a provider that ships
    a malformed header must not be able to fail every call that carries it.
    """

    def get(name: str) -> str:
        try:
            return str(headers.get(name) or "")
        except Exception:  # noqa: BLE001 - a header mapping we do not know
            return ""

    def integer(name: str) -> int | None:
        raw = get(name)
        if not raw:
            return None
        try:
            return int(float(raw))
        except ValueError:
            return None

    def seconds(name: str) -> float | None:
        raw = get(name)
        if not raw:
            return None
        try:
            return parse_duration(raw)
        except ValueError:
            return None

    return RateHeaders(
        limit_requests=integer("x-ratelimit-limit-requests"),
        remaining_requests=integer("x-ratelimit-remaining-requests"),
        reset_requests_s=seconds("x-ratelimit-reset-requests"),
        limit_tokens=integer("x-ratelimit-limit-tokens"),
        remaining_tokens=integer("x-ratelimit-remaining-tokens"),
        reset_tokens_s=seconds("x-ratelimit-reset-tokens"),
    )


# Declared budgets, researched 2026-08-28 and marked with where each came from.
#
# Cerebras is deliberately absent: its key returns 402 "Payment required to
# access this resource", so there is no free budget to track. Recording a
# generous free tier for a provider that refuses every call would be the most
# misleading entry in this table.
BUDGETS: dict[str, BudgetSpec] = {
    # RESEARCHED AND REFUTED 2026-08-29 by independent agents against the
    # official documentation AND against this deployment's own usage journal.
    # Two of yesterday's "BLOCKED" states turned out to be self-inflicted:
    # this table said groq allowed 100,000 tokens/day and openrouter 50
    # requests/day, and BudgetTracker refused further calls against those
    # figures. Groq had accepted 101,522 tokens with no 429; openrouter had
    # accepted 51 requests. The providers never said no. The table did.
    #
    # A declared limit that is tighter than the real one is not "safe": it
    # throws away capacity the operator paid for and reports an outage that
    # did not happen. The numbers below are what the official pages say,
    # marked where the official page says nothing.
    "groq": BudgetSpec(
        provider="groq",
        kind="quota",
        limits=(
            # Per MODEL, per the official table. gpt-oss-20b/120b: 8K TPM,
            # 200K TPD. qwen/qwen3.8-27b: 8K TPM, 2M TPD -- ten times the daily
            # tokens, which makes it the model to route to when the others are
            # spent. TPM binds first: at 8K/minute one reasoning-heavy reply
            # can be most of a minute's budget.
            #
            # 8,000 TPM is what the console publishes. This deployment has
            # observed larger minutes accepted, and that observation is left to
            # the header-clamp path (ADR-008) rather than written in here:
            # believing an observation that happens to be generous is how a
            # table stops being a floor.
            Limit("minute", requests=30, tokens=8_000),
            # 200,000 tokens per model per day, not 100,000 across the account.
            # The old single figure was measured on one model and applied to
            # all of them, which is why a run reported "day budget exhausted"
            # at 98 requests while two other models were untouched -- see
            # `per_model` below, which is the half of the fix that matters.
            Limit("day", requests=1_000, tokens=200_000),
        ),
        reset="rolling",
        per_model=True,
        source="https://console.groq.com/docs/rate-limits",
        checked="2026-08-29",
        note=(
            "Per-model, and confirmed by four independent fetches. The "
            "official page does NOT state how the daily window resets -- not "
            "rolling, not a timezone, nothing; blog claims of midnight UTC are "
            "unsourced. Accounted as ROLLING because that is the error which "
            "under-uses rather than the one that double-spends, and narrowed "
            "from x-ratelimit-reset-* headers, the only ground truth Groq "
            "actually provides. Yesterday's 100K/day figure was wrong by half."
        ),
    ),
    "google-aistudio": BudgetSpec(
        provider="google-aistudio",
        kind="quota",
        limits=(
            # Google publishes NO per-model numbers; it redirects to a private
            # AI Studio dashboard. The journal shows 16 successful
            # gemini-3.5-flash-lite calls inside one rolling minute, so 15 RPM
            # is wrong for the model actually in use. 30 is the figure secondary
            # sources give for the flash-lite line; the pacer learns the real
            # one from 429s and headers.
            Limit("minute", requests=30, tokens=250_000),
            Limit("day", requests=1_000),
        ),
        resets_at_pacific_midnight=True,
        reset="pacific-midnight",
        source="https://ai.google.dev/gemini-api/docs/rate-limits",
        checked="2026-08-29",
        note=(
            "Only the midnight-Pacific daily reset is officially stated. Every "
            "number here is secondary-source or observed, and the official "
            "page says as much: it directs you to the dashboard. 1,000/day is "
            "a conservative floor; the pacer widens it from observed headers."
        ),
    ),
    "openrouter": BudgetSpec(
        provider="openrouter",
        kind="quota",
        limits=(
            Limit("minute", requests=20),
            # 1,000/day, because THIS account is funded. 50/day is the unfunded
            # figure and was what the table carried while the account it
            # describes had already been topped up -- a twentyfold
            # understatement that made the pool treat a workhorse as a
            # tie-breaker.
            Limit("day", requests=1_000),
        ),
        reset="utc-midnight",
        source="https://openrouter.ai/docs/api-reference/limits",
        checked="2026-08-29",
        note=(
            "1,000/day: this account is FUNDED. Confirmed 2026-08-29 via "
            "GET /api/v1/key (is_free_tier=false, usage=$0), a metadata call "
            "that spends no tokens. `:free` models remain $0 on a funded "
            "account; the $10 unlocks the request cap and is not consumed by "
            "free-model calls. The daily reset at midnight UTC is documented "
            "rather than inferred; whether the 20/minute cap is a rolling or "
            "fixed minute is not stated."
        ),
    ),
    "mistral-free": BudgetSpec(
        provider="mistral-free",
        kind="rate",
        limits=(
            Limit("minute", requests=60, tokens=500_000),
            Limit("month", tokens=1_000_000_000),
        ),
        reset="rolling",
        source="https://help.mistral.ai/en/articles/225174",
        checked="2026-08-29",
        note=(
            "1 req/s sustained, 500K TPM, and NO daily cap -- which is why "
            "kind is rate: there is no day to spread over, so this provider is "
            "never session-rationed and only its minute bucket and month cap "
            "apply. The official docs publish limit CATEGORIES, not values; "
            "the numbers come from Mistral's help centre and are unverified "
            "against the admin dashboard. Enabled for this deployment: the "
            "operator has consented to the Experiment tier's data-training "
            "terms in exchange for capacity that never runs out mid-day."
        ),
    ),
    "nvidia-nim": BudgetSpec(
        provider="nvidia-nim",
        kind="grant",
        limits=(Limit("minute", requests=40),),
        grant_total=1_000,
        grant_expires_days=30,
        source="https://build.nvidia.com",
        checked="2026-08-28",
        note=(
            "A GRANT, not a tier: ~1,000 credits that never refill and expire "
            "30 days after they are issued, consumed at a variable rate per "
            "model. Spending it first because it is free is how a month's "
            "burst capacity disappears in week one. This deployment's grant is "
            "expected to be spent: it stays declared so the pool can SEE that "
            "and skip it, which is not the same as pretending it is not there."
        ),
    ),
}


# --- the journal -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageRow:
    """One movement in the account, not one call.

    ANATOMY: kind
      "reserve"    a call is about to be sent, charged at an ESTIMATE.
      "settle"     the difference between the estimate and what happened, so
                   reserve + settle for one `rid` sums to the truth.
      "exhausted"  the provider itself said the day is spent, until `until`.

      Rows are split in two because admission and knowledge arrive at different
      times: we must charge before sending (or N concurrent agents each admit
      themselves against the same headroom), and we only learn the token count
      afterwards. Older rows carry no kind and are read as "reserve", so a
      journal written before this existed still sums to the same figures.

    ANATOMY: attempts
      Sends, including the ones that 429'd or errored. `requests` counts
      SUCCESSES. They differ because a provider that charges a rejected request
      against the daily allowance -- several do -- would otherwise let a retry
      storm overshoot by exactly the retry count. Defaults to `requests` when
      absent so old rows are unchanged.

    ANATOMY: cached_tokens
      How many of `tokens` the provider served from its own prompt cache.
      OBSERVATIONAL ONLY, and never subtracted from `tokens`: providers
      discount cached prompt tokens on PRICE, not on quota, so a journal that
      netted them off would let the ration believe in headroom the account
      does not have. Everything that reads this file for admission decisions
      reads `tokens`.
    """

    ts: float
    provider: str
    credential: str
    model: str
    requests: int
    tokens: int
    kind: str = "reserve"
    rid: str = ""
    attempts: int = 0
    until: float = 0.0
    cached_tokens: int = 0

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "ts": round(self.ts, 3),
            "provider": self.provider,
            "credential": self.credential,
            "model": self.model,
            "requests": self.requests,
            "tokens": self.tokens,
        }
        # Written only when they carry information. A journal line per call is
        # the hot path of a long-lived file, and four always-present fields that
        # are usually defaults is a third of the file for nothing.
        if self.kind != "reserve":
            payload["kind"] = self.kind
        if self.rid:
            payload["rid"] = self.rid
        if self.attempts != self.requests:
            payload["attempts"] = self.attempts
        if self.until:
            payload["until"] = round(self.until, 3)
        if self.cached_tokens:
            payload["cached_tokens"] = self.cached_tokens
        return json.dumps(payload, separators=(",", ":"))

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UsageRow:
        requests = int(data.get("requests", 0))
        return UsageRow(
            ts=float(data["ts"]),
            provider=str(data.get("provider", "")),
            credential=str(data.get("credential", "")),
            model=str(data.get("model", "")),
            requests=requests,
            tokens=int(data.get("tokens", 0)),
            kind=str(data.get("kind", "reserve")),
            rid=str(data.get("rid", "")),
            attempts=int(data.get("attempts", requests)),
            until=float(data.get("until", 0.0)),
            cached_tokens=int(data.get("cached_tokens", 0)),
        )


DEFAULT_JOURNAL = ".swarmd/usage.jsonl"


class UsageJournal:
    """Append-only record of what each credential spent, across runs.

    SEPARATE FROM THE COST LEDGER on purpose. The ledger is per-run and answers
    "what did this run cost"; it is deleted or rotated with the run. A monthly
    budget has to survive that, and has to span every run on the machine, so it
    needs a store whose lifetime is the month rather than the run.

    Compacted on load to the longest window plus a margin. Without it the file
    grows without bound and every startup reads a year of history to answer a
    question about the last thirty days.
    """

    def __init__(self, path: str | pathlib.Path | None = None) -> None:
        self.path = pathlib.Path(
            path or os.environ.get("SWARMD_USAGE_JOURNAL", DEFAULT_JOURNAL)
        )
        self._lock = threading.Lock()
        self._rows: list[UsageRow] = []
        self._loaded = False
        # Size and mtime as of the last load. The cache used to be
        # load-once-forever, which is wrong the moment a second process shares
        # the journal -- and that is the normal case here: a long-lived
        # `swarmd serve` plus somebody running the CLI. The server would ration
        # against a snapshot taken at boot and never see a request the CLI
        # spent.
        self._stat: tuple[int, float] = (-1, -1.0)
        # Whether the last append reached the disk. The ration gate reads it and
        # refuses rather than admitting: in-memory accounting is enough to keep
        # a report honest, but a ration that forgets a restart's worth of spend
        # would hand the next process the whole day again.
        self.last_write_ok = True

    # -- reading --------------------------------------------------------

    def _fingerprint(self) -> tuple[int, float]:
        try:
            info = self.path.stat()
        except OSError:
            return (-1, -1.0)
        return (info.st_size, info.st_mtime)

    def load(self) -> list[UsageRow]:
        # One stat per read, against a file another process may be appending
        # to. Not a substitute for RedisRation when replicas share a credential
        # -- a stat cannot make read-then-write atomic -- but it is the
        # difference between "eventually correct" and "wrong until restart".
        if self._loaded and self._fingerprint() == self._stat:
            return self._rows
        self._loaded = False
        rows: list[UsageRow] = []
        try:
            raw = self.path.read_text(encoding="utf-8") if self.path.exists() else ""
        except OSError as exc:
            # Unreadable is not empty, but it has to behave like it: budgets
            # are an input to whether a run may start, and a permissions
            # problem on a journal must not be the thing that stops one.
            logger.warning("usage journal unreadable (%s): %s", self.path, exc)
            raw = ""
        if raw:
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(UsageRow.from_dict(json.loads(line)))
                except Exception:  # noqa: BLE001 - a torn line is not fatal
                    # A partial final line after a crash costs one row of
                    # history. Refusing to start over it would turn a cosmetic
                    # problem into an outage.
                    logger.warning("skipping unreadable usage row in %s", self.path)
        self._rows = rows
        self._loaded = True
        self._stat = self._fingerprint()
        return self._rows

    def rows_since(self, cutoff: float) -> list[UsageRow]:
        return [row for row in self.load() if row.ts >= cutoff]

    def rows_for(
        self,
        *,
        provider: str,
        credential: str = "",
        model: str = "",
        since: float = 0.0,
    ) -> list[UsageRow]:
        """Rows for one meter: a provider, optionally one credential and model.

        The filters are optional and additive because different questions are
        asked at different scopes -- a report is per provider, a ration is per
        credential, and Groq's ration is per credential AND model.
        """
        out = []
        for row in self.load():
            if row.ts < since or row.provider != provider:
                continue
            if credential and row.credential != credential:
                continue
            if model and row.model and row.model != model:
                continue
            out.append(row)
        return out

    # -- writing --------------------------------------------------------

    def record(
        self,
        *,
        provider: str,
        credential: str,
        model: str = "",
        requests: int = 1,
        tokens: int = 0,
        ts: float | None = None,
        kind: str = "reserve",
        rid: str = "",
        attempts: int | None = None,
        until: float = 0.0,
        cached_tokens: int = 0,
    ) -> UsageRow:
        row = UsageRow(
            ts=ts if ts is not None else time.time(),
            provider=provider,
            credential=credential,
            model=model,
            requests=requests,
            tokens=tokens,
            kind=kind,
            rid=rid,
            attempts=requests if attempts is None else attempts,
            until=until,
            cached_tokens=cached_tokens,
        )
        with self._lock:
            self.load().append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(row.to_json() + "\n")
                self.last_write_ok = True
                # Our own row is already in `_rows`; adopt the new fingerprint
                # so this append does not read as someone else's change and
                # force a full reload on the next query.
                self._stat = self._fingerprint()
            except OSError as exc:
                # In-memory accounting continues. Losing durability degrades
                # the month view after a restart; refusing the call would stop
                # the run over a bookkeeping failure.
                self.last_write_ok = False
                logger.warning("usage journal unwritable (%s): %s", self.path, exc)
        return row

    def compact(self, *, keep_s: float = MONTH * 1.1, now: float | None = None) -> int:
        """Drop rows older than any window can ask about. Returns rows dropped."""
        now = now if now is not None else time.time()
        cutoff = now - keep_s
        with self._lock:
            # Re-read immediately before rewriting. Compaction replaces the
            # whole file from this process's view, so a stale view silently
            # DELETES every row another process appended since we loaded --
            # data loss dressed as maintenance, and invisible afterwards.
            self._loaded = False
            rows = self.load()
            keep = [row for row in rows if row.ts >= cutoff]
            dropped = len(rows) - len(keep)
            if not dropped:
                return 0
            self._rows = keep
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                tmp = self.path.with_suffix(".tmp")
                tmp.write_text(
                    "".join(row.to_json() + "\n" for row in keep), encoding="utf-8"
                )
                tmp.replace(self.path)
                self._stat = self._fingerprint()
            except OSError as exc:
                logger.warning("usage journal compaction failed: %s", exc)
            return dropped


# --- the tracker -----------------------------------------------------------


@dataclass
class WindowState:
    window: str
    used_requests: int
    used_tokens: int
    limit_requests: int | None
    limit_tokens: int | None
    resets_in_s: float

    @property
    def remaining_requests(self) -> int | None:
        if self.limit_requests is None:
            return None
        return max(0, self.limit_requests - self.used_requests)

    @property
    def remaining_tokens(self) -> int | None:
        if self.limit_tokens is None:
            return None
        return max(0, self.limit_tokens - self.used_tokens)

    @property
    def exhausted(self) -> bool:
        return self.remaining_requests == 0 or self.remaining_tokens == 0

    @property
    def fraction_used(self) -> float:
        """Worst of the two dimensions. The binding one is what matters."""
        fractions = []
        if self.limit_requests:
            fractions.append(self.used_requests / self.limit_requests)
        if self.limit_tokens:
            fractions.append(self.used_tokens / self.limit_tokens)
        return max(fractions) if fractions else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "window": self.window,
            "used_requests": self.used_requests,
            "used_tokens": self.used_tokens,
            "limit_requests": self.limit_requests,
            "limit_tokens": self.limit_tokens,
            "remaining_requests": self.remaining_requests,
            "remaining_tokens": self.remaining_tokens,
            "fraction_used": round(self.fraction_used, 4),
            "resets_in_s": round(self.resets_in_s, 1),
            "exhausted": self.exhausted,
        }


class BudgetTracker:
    """Answers "what is left" per provider, per window, from the journal."""

    def __init__(
        self,
        journal: UsageJournal | None = None,
        budgets: dict[str, BudgetSpec] | None = None,
    ) -> None:
        self.journal = journal or UsageJournal()
        self.budgets = budgets if budgets is not None else BUDGETS
        self.journal.compact()

    # -- recording ------------------------------------------------------

    def record(self, **kw: Any) -> UsageRow:
        return self.journal.record(**kw)

    # -- reading --------------------------------------------------------

    def window_state(
        self, provider: str, window: str, *, now: float | None = None
    ) -> WindowState:
        now = now if now is not None else time.time()
        spec = self.budgets.get(provider)
        limit = spec.limit_for(window) if spec else None
        duration = WINDOWS[window]

        if spec and window == "day" and spec.reset_kind != "rolling":
            # A wall-clock reset, so the window is "since the last midnight",
            # not "the last 24 hours". Treating it as rolling under-uses the
            # quota all morning and over-commits against it at night.
            reset_at = day_reset_at(spec, now)
            start = reset_at - DAY
            resets_in = reset_at - now
        else:
            start = now - duration
            resets_in = duration

        used_requests = 0
        used_tokens = 0
        for row in self.journal.rows_since(start):
            if row.provider == provider:
                used_requests += row.requests
                used_tokens += row.tokens

        return WindowState(
            window=window,
            used_requests=used_requests,
            used_tokens=used_tokens,
            limit_requests=limit.requests if limit else None,
            limit_tokens=limit.tokens if limit else None,
            resets_in_s=resets_in,
        )

    def grant_state(
        self, provider: str, *, now: float | None = None
    ) -> dict[str, Any] | None:
        """Remaining finite allowance, or None for providers that replenish."""
        spec = self.budgets.get(provider)
        if not spec or spec.kind != "grant" or spec.grant_total is None:
            return None
        now = now if now is not None else time.time()
        # A grant has no window: every request ever made against it counts.
        used = sum(
            row.requests for row in self.journal.load() if row.provider == provider
        )
        remaining = max(0, spec.grant_total - used)
        return {
            "total": spec.grant_total,
            "used": used,
            "remaining": remaining,
            "fraction_used": round(used / spec.grant_total, 4),
            "expires_days": spec.grant_expires_days,
            "exhausted": remaining == 0,
        }

    def observe_day_limit(
        self,
        provider: str,
        *,
        credential: str,
        until: float,
        model: str = "",
        source: str = "",
    ) -> UsageRow:
        """Record that the provider itself said the day is spent.

        ADR-008 in its sharpest form: a 429 whose retry-after is an hour away,
        or whose body names a per-day metric, is the account telling us the
        declared figure was wrong. Written as a journal row rather than held in
        memory so the correction survives a restart and is visible to every
        replica reading the same journal -- a limit learned once and forgotten
        on the next deploy is a limit learned every day.
        """
        logger.warning(
            "%s/%s: provider reports its day exhausted until %.0f (%s)",
            provider, credential, until, source or "429",
        )
        return self.journal.record(
            provider=provider,
            credential=credential,
            model=model,
            requests=0,
            tokens=0,
            kind="exhausted",
            until=until,
        )

    def day_exhausted_until(
        self, provider: str, *, credential: str = "", now: float | None = None
    ) -> float:
        """The latest provider-declared exhaustion still in force, or 0.0."""
        now = now if now is not None else time.time()
        rows = self.journal.rows_for(
            provider=provider, credential=credential, since=now - DAY
        )
        return max(
            (row.until for row in rows if row.kind == "exhausted" and row.until > now),
            default=0.0,
        )

    def observe_headers(
        self,
        headers: RateHeaders,
        *,
        provider: str,
        credential: str,
        model: str = "",
        now: float | None = None,
    ) -> float:
        """Believe a SUCCESSFUL response that says the allowance is gone.

        The cheapest limit to respect is the one the provider volunteers on the
        way past. Every call we make comes back carrying how much is left and
        when it refills, and until now the pool threw all of it away and waited
        for a 429 to learn the same fact -- one rejected request later, with a
        backoff attached.

        Only the EMPTY case is acted on, and only when the refill is far enough
        away to be a long window (`LONG_WINDOW_S`). A near reset is the minute
        bucket, which `quota.py` already paces and which needs no journal row;
        a distant one is a day, and a day that the provider says is spent is
        exactly the fact `blocked` and the ration must not talk past.

        Returns the exhaustion instant it recorded, or 0.0 for the ordinary case
        where there is still headroom.
        """
        now = now if now is not None else time.time()
        worst = 0.0
        for remaining, reset_s in (
            (headers.remaining_requests, headers.reset_requests_s),
            (headers.remaining_tokens, headers.reset_tokens_s),
        ):
            if remaining is None or remaining > 0:
                continue
            if reset_s is None or reset_s < LONG_WINDOW_S:
                continue
            worst = max(worst, now + reset_s)
        if worst:
            self.observe_day_limit(
                provider,
                credential=credential,
                until=worst,
                model=model,
                source="response headers",
            )
        return worst

    def blocked(
        self,
        provider: str,
        *,
        now: float | None = None,
        include_short: bool = False,
    ) -> str:
        """Why this provider cannot take a call right now, or "".

        Returns the FIRST binding window rather than a list: an operator acting
        on this needs the one that is stopping them, and a run of five reasons
        buries it.

        THE PROVIDER'S OWN WORD COMES FIRST. A declared window is a hypothesis;
        an `exhausted` row is the account itself, and it outranks every figure
        in the table (ADR-008). Without this check `observe_day_limit` wrote a
        row nothing read: the ration honoured it, this did not, so the pool's
        budget gate kept offering a provider that had already said no.
        """
        now = now if now is not None else time.time()
        until = self.day_exhausted_until(provider, now=now)
        if until:
            return f"provider reports day spent for {int(until - now)}s"
        grant = self.grant_state(provider, now=now)
        if grant and grant["exhausted"]:
            return "grant exhausted"
        windows = WINDOW_ORDER if include_short else DAY_SCALE_WINDOWS
        for window in windows:
            state = self.window_state(provider, window, now=now)
            if state.exhausted:
                return f"{window} budget exhausted"
        return ""

    def throttled(
        self, provider: str, *, now: float | None = None
    ) -> tuple[str, float]:
        """A self-refilling window that is full right now, and the wait for it.

        Separate from `blocked` because the answers differ: a spent day means
        route elsewhere, a spent minute means wait a moment. Collapsing them
        threw away most of a day's capacity every time one minute filled up.
        """
        now = now if now is not None else time.time()
        for window in SHORT_WINDOWS:
            state = self.window_state(provider, window, now=now)
            if state.exhausted:
                return f"{window} budget full", max(0.0, state.resets_in_s)
        return "", 0.0

    def report(self, provider: str, *, now: float | None = None) -> dict[str, Any]:
        spec = self.budgets.get(provider)
        windows = [
            self.window_state(provider, window, now=now).to_dict()
            for window in WINDOW_ORDER
            if spec and spec.limit_for(window)
        ]
        return {
            "provider": provider,
            "kind": spec.kind if spec else "unknown",
            "source": spec.source if spec else "",
            "checked": spec.checked if spec else "",
            "note": spec.note if spec else "",
            "windows": windows,
            "grant": self.grant_state(provider, now=now),
            "blocked": self.blocked(provider, now=now),
        }

    def report_all(self, *, now: float | None = None) -> list[dict[str, Any]]:
        return [self.report(name, now=now) for name in sorted(self.budgets)]

    # -- planning -------------------------------------------------------

    def observed_tokens_per_request(
        self, provider: str, *, default: int = 1_000, now: float | None = None
    ) -> int:
        """Mean tokens per call for this provider, measured.

        Needed because a request cap and a token cap are different ceilings and
        the smaller one wins. Measured rather than assumed: the figure depends
        on prompt size, which depends on the schema hints and retrieved skills,
        which change.
        """
        now = now if now is not None else time.time()
        # EVERY row of the meter, not only the ones carrying a request count.
        # A call is journalled as several rows that only make sense summed: the
        # ration reserves +1 request and a token ESTIMATE, then settles the
        # difference between the estimate and what the call really cost on a row
        # carrying no request at all. Filtering to `row.requests` kept the
        # estimate and dropped the correction, so this returned the estimate it
        # had itself produced -- a measurement that could never move off its own
        # default no matter what the provider actually charged.
        rows = [
            row
            for row in self.journal.rows_since(now - WEEK)
            if row.provider == provider and row.kind != "exhausted"
        ]
        if not rows:
            return default
        requests = sum(row.requests for row in rows)
        tokens = sum(row.tokens for row in rows)
        if requests <= 0 or tokens <= 0:
            # Only in-flight reservations, or none at all. Nothing has settled
            # into a figure worth believing over the default.
            return default
        return max(1, tokens // requests)

    def daily_capacity(self, provider: str) -> tuple[int, str]:
        """Requests per day this provider can supply, and on what evidence.

        The BASIS matters more than the number, which is why it is returned
        rather than documented:

          "daily_cap"   a published per-day allowance. Plannable.
          "grant"       a finite pool spread over its remaining life. Plannable
                        once, then gone.
          "rate_only"   NO published daily cap, so this is a per-minute rate
                        multiplied out to 24 hours. It is an upper bound that
                        assumes perfect saturation for a full day, and nothing
                        should be planned against it.

        Collapsing these into one integer is how a capacity plan comes to be
        dominated by a number nobody meant as a promise: Mistral's 60/minute
        extrapolates to 86,400/day, which would be 98% of a headline total
        while being the least reliable figure in it.
        """
        spec = self.budgets.get(provider)
        if not spec:
            return 0, "unknown"
        if spec.kind == "grant" and spec.grant_total and spec.grant_expires_days:
            grant = self.grant_state(provider)
            remaining = grant["remaining"] if grant else spec.grant_total
            return int(remaining // max(1, spec.grant_expires_days)), "grant"
        daily = spec.limit_for("day")
        if daily and (daily.requests or daily.tokens):
            # BOTH dimensions, because the smaller ceiling is the real one and
            # it is not always the obvious one. Groq publishes 1,000 requests
            # and 200,000 tokens per model per day; at ~1,035 tokens per call
            # the token figure binds first, at ~193 requests. Reported as
            # 1,000/day it overstated the provider fivefold, and the run that
            # discovered it saw "day budget exhausted" next to "98 / 1,000"
            # because the account-wide reading was wrong in the same place.
            by_requests = daily.requests or 10**9
            if daily.tokens:
                per_call = self.observed_tokens_per_request(provider)
                by_tokens = daily.tokens // max(1, per_call)
            else:
                by_tokens = 10**9
            if by_tokens < by_requests:
                return by_tokens, "daily_cap_tokens"
            return by_requests, "daily_cap"
        minute = spec.limit_for("minute")
        if minute and minute.requests:
            return int(minute.requests * 60 * 24), "rate_only"
        return 0, "none"

    def sustainable_requests_per_day(self, provider: str) -> int | None:
        """Backwards-compatible view of `daily_capacity`."""
        value, basis = self.daily_capacity(provider)
        return value if basis != "unknown" else None

    def remaining_today(self, *, now: float | None = None) -> int:
        """Requests still available across every provider before tomorrow.

        Summed over providers because the pool routes across them: a run is not
        blocked by one provider being spent, only by all of them being spent.
        Grants are included, because a grant is real capacity today -- it is
        the MONTH that cannot be planned on one.
        """
        total = 0
        for name, spec in self.budgets.items():
            if self.blocked(name, now=now):
                continue
            capacity, basis = self.daily_capacity(name)
            if basis == "rate_only":
                # EXCLUDED, for the same reason `capacity_plan` excludes it: a
                # per-minute rate multiplied out to 24 hours assumes perfect
                # saturation for a full day. Counting it here made the
                # preflight report 86,988 requests remaining against a real
                # plannable budget of ~1,146 -- so every run, at any size,
                # answered "fits". A preflight that always says yes is not a
                # preflight.
                continue
            used = self.window_state(name, "day", now=now).used_requests
            if spec.kind == "grant":
                grant = self.grant_state(name, now=now)
                remaining = grant["remaining"] if grant else 0
                total += min(capacity, remaining)
            else:
                total += max(0, capacity - used)
        return total

    def affordable(
        self, estimated_calls: int, *, now: float | None = None
    ) -> dict[str, Any]:
        """Whether a run of this size fits in what is left today.

        Returns the numbers rather than a verdict, because the decision is the
        operator's. A run that does not fit is not forbidden -- it will exhaust
        the budget and stop partway, which is sometimes exactly what someone
        wants at the end of a day. What is not acceptable is finding out
        afterwards.
        """
        remaining = self.remaining_today(now=now)
        return {
            "estimated_calls": estimated_calls,
            "remaining_today": remaining,
            "fits": estimated_calls <= remaining,
            "shortfall": max(0, estimated_calls - remaining),
            "fraction_of_remaining": (
                round(estimated_calls / remaining, 3) if remaining else None
            ),
        }

    def capacity_plan(self, *, now: float | None = None) -> dict[str, Any]:
        """Whether the configured providers can carry a day, a week, a month.

        The headline counts ONLY published daily allowances. Grants are
        reported beside it because they stop; rate extrapolations are reported
        beside it because they assume a day of perfect saturation. Both belong
        in the report and neither belongs in the number an operator plans with.
        """
        per_provider: dict[str, dict[str, Any]] = {}
        for name in sorted(self.budgets):
            value, basis = self.daily_capacity(name)
            per_provider[name] = {"requests_per_day": value, "basis": basis}

        def total(basis: str) -> int:
            return sum(
                entry["requests_per_day"]
                for entry in per_provider.values()
                if entry["basis"] == basis
            )

        planned = total("daily_cap") + total("daily_cap_tokens")
        return {
            "per_provider": per_provider,
            "sustainable_daily_requests": planned,
            "grant_backed_daily_requests": total("grant"),
            "rate_extrapolated_upper_bound": total("rate_only"),
            "week_requests": planned * 7,
            "month_requests": planned * 30,
        }
