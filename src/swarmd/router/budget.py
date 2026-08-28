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

    ANATOMY: grant_total / grant_expires_days
      For finite pools. `grant_total` is the whole allowance, not a per-window
      figure, and it never refills.
    """

    provider: str
    kind: str = "rate"
    limits: tuple[Limit, ...] = ()
    resets_at_pacific_midnight: bool = False
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


# Declared budgets, researched 2026-08-28 and marked with where each came from.
#
# Cerebras is deliberately absent: its key returns 402 "Payment required to
# access this resource", so there is no free budget to track. Recording a
# generous free tier for a provider that refuses every call would be the most
# misleading entry in this table.
BUDGETS: dict[str, BudgetSpec] = {
    "groq": BudgetSpec(
        provider="groq",
        kind="quota",
        limits=(
            Limit("minute", requests=30, tokens=12_000),
            Limit("day", requests=1_000, tokens=100_000),
        ),
        source="https://console.groq.com/docs/rate-limits",
        checked="2026-08-28",
        note=(
            "Per-model. Measured fastest of every provider here: 384 tok/s on "
            "openai/gpt-oss-20b, 0.81s to a complete structured answer."
        ),
    ),
    "google-aistudio": BudgetSpec(
        provider="google-aistudio",
        kind="quota",
        limits=(
            Limit("minute", requests=15, tokens=250_000),
            Limit("day", requests=1_000),
        ),
        resets_at_pacific_midnight=True,
        source="https://ai.google.dev/gemini-api/docs/rate-limits",
        checked="2026-08-28",
        note=(
            "Google no longer publishes per-model numbers in its docs -- it "
            "directs you to AI Studio, which is itself the argument for "
            "discovering limits from 429s (ADR-008). These are conservative."
        ),
    ),
    "openrouter": BudgetSpec(
        provider="openrouter",
        kind="quota",
        limits=(
            Limit("minute", requests=20),
            Limit("day", requests=50),
        ),
        source="https://openrouter.ai/docs/api-reference/limits",
        checked="2026-08-28",
        note=(
            "50/day applies to unfunded accounts; funding $10 raises it to "
            "1,000/day and the 20/minute cap stays. At 50/day this is a "
            "tie-breaker, not a workhorse."
        ),
    ),
    "mistral-free": BudgetSpec(
        provider="mistral-free",
        kind="rate",
        limits=(
            Limit("minute", requests=60, tokens=500_000),
            Limit("month", tokens=1_000_000_000),
        ),
        source="https://help.mistral.ai/en/articles/225174",
        checked="2026-08-28",
        note=(
            "1 req/s sustained. The billion-token month is effectively "
            "unreachable at this scale, so the per-second rate is the real "
            "constraint. Requires SWARMD_ALLOW_DATA_TRAINING."
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
            "burst capacity disappears in week one."
        ),
    ),
}


# --- the journal -----------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UsageRow:
    ts: float
    provider: str
    credential: str
    model: str
    requests: int
    tokens: int

    def to_json(self) -> str:
        return json.dumps(
            {
                "ts": round(self.ts, 3),
                "provider": self.provider,
                "credential": self.credential,
                "model": self.model,
                "requests": self.requests,
                "tokens": self.tokens,
            },
            separators=(",", ":"),
        )

    @staticmethod
    def from_dict(data: dict[str, Any]) -> UsageRow:
        return UsageRow(
            ts=float(data["ts"]),
            provider=str(data.get("provider", "")),
            credential=str(data.get("credential", "")),
            model=str(data.get("model", "")),
            requests=int(data.get("requests", 0)),
            tokens=int(data.get("tokens", 0)),
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

    # -- reading --------------------------------------------------------

    def load(self) -> list[UsageRow]:
        if self._loaded:
            return self._rows
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
        return self._rows

    def rows_since(self, cutoff: float) -> list[UsageRow]:
        return [row for row in self.load() if row.ts >= cutoff]

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
    ) -> UsageRow:
        row = UsageRow(
            ts=ts if ts is not None else time.time(),
            provider=provider,
            credential=credential,
            model=model,
            requests=requests,
            tokens=tokens,
        )
        with self._lock:
            self.load().append(row)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.path.open("a", encoding="utf-8") as handle:
                    handle.write(row.to_json() + "\n")
            except OSError as exc:
                # In-memory accounting continues. Losing durability degrades
                # the month view after a restart; refusing the call would stop
                # the run over a bookkeeping failure.
                logger.warning("usage journal unwritable (%s): %s", self.path, exc)
        return row

    def compact(self, *, keep_s: float = MONTH * 1.1, now: float | None = None) -> int:
        """Drop rows older than any window can ask about. Returns rows dropped."""
        now = now if now is not None else time.time()
        cutoff = now - keep_s
        with self._lock:
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

        if spec and spec.resets_at_pacific_midnight and window == "day":
            # A wall-clock reset, so the window is "since the last midnight",
            # not "the last 24 hours". Treating it as rolling under-uses the
            # quota all morning and over-commits against it at night.
            reset_at = next_pacific_midnight(now)
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

    def blocked(self, provider: str, *, now: float | None = None) -> str:
        """Why this provider cannot take a call right now, or "".

        Returns the FIRST binding window rather than a list: an operator acting
        on this needs the one that is stopping them, and a run of five reasons
        buries it.
        """
        grant = self.grant_state(provider, now=now)
        if grant and grant["exhausted"]:
            return "grant exhausted"
        for window in WINDOW_ORDER:
            state = self.window_state(provider, window, now=now)
            if state.exhausted:
                return f"{window} budget exhausted"
        return ""

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
        if daily and daily.requests:
            return daily.requests, "daily_cap"
        minute = spec.limit_for("minute")
        if minute and minute.requests:
            return int(minute.requests * 60 * 24), "rate_only"
        return 0, "none"

    def sustainable_requests_per_day(self, provider: str) -> int | None:
        """Backwards-compatible view of `daily_capacity`."""
        value, basis = self.daily_capacity(provider)
        return value if basis != "unknown" else None

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

        planned = total("daily_cap")
        return {
            "per_provider": per_provider,
            "sustainable_daily_requests": planned,
            "grant_backed_daily_requests": total("grant"),
            "rate_extrapolated_upper_bound": total("rate_only"),
            "week_requests": planned * 7,
            "month_requests": planned * 30,
        }
