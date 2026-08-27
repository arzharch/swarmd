"""Append-only cost ledger — the only source of any number swarmd reports.

Why this exists (ADR-007): agents are selected on reported success and paid on
verified success. Anything an agent can write, selection pressure eventually
teaches it to write dishonestly. So no component keeps a running total. Every
model call, cache hit, gate outcome, containment, and verified success appends a
row; every reported figure is an aggregate over rows.

The distinction that matters: `total_cost()` SUMS the rows. It is not an
incrementing float kept alongside them. A counter can drift from its rows and
nothing notices; a sum cannot. `verify()` re-reads the durable log and asserts
the in-process view matches it, which turns "the ledger is honest" into a test
rather than a promise.

Cache hits write rows too, at zero cost. That makes "what did the cache save" a
query (sum of `would_have_cost` over cache-hit rows) instead of an estimate.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

# --- pricing ---------------------------------------------------------------
#
# Prices are DATA, not code, so a provider price change is an edit to a table
# rather than a patch to logic. USD per million tokens, verified 2026-08-27.
# Anything absent from this table is priced at zero ONLY if the model id or the
# provider is known-free; otherwise pricing is refused loudly, because silently
# pricing an unknown model at zero is how a cost ceiling stops working.


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """USD per million tokens."""

    input_per_m: float
    output_per_m: float

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return (
            tokens_in * self.input_per_m + tokens_out * self.output_per_m
        ) / 1_000_000


FREE = ModelPrice(0.0, 0.0)

# Providers whose entire offering used here is free-tier.
# "simulated" is priced free because it never touches a network. Its rows
# carry simulated=True, which is what stops them being read as a real $0 run.
FREE_PROVIDERS = frozenset(
    {"groq", "cerebras", "google-aistudio", "mistral-free", "simulated"}
)

PRICES: dict[str, ModelPrice] = {
    # Paid overflow tier (PRD section 11). Cheapest capable model with a large
    # context window, which is what an agent carrying retrieved skills needs.
    "z-ai/glm-5.3-flash": ModelPrice(0.075, 0.25),
    # Second-choice overflow if GLM is unavailable.
    "qwen/qwen-3.8-flash": ModelPrice(0.15, 0.47),
}


class UnpricedModel(RuntimeError):
    """Raised when a model cannot be priced.

    Deliberately fatal. A model priced at an assumed zero silently disables the
    cost ceiling, which is the one control standing between a run and an
    unbounded bill.
    """


def price_for(provider: str, model: str) -> ModelPrice:
    """Resolve a price, or refuse."""
    if model in PRICES:
        return PRICES[model]
    if model.endswith(":free") or provider in FREE_PROVIDERS:
        return FREE
    raise UnpricedModel(
        f"no price for provider={provider!r} model={model!r}; add it to "
        f"swarmd.ledger.PRICES or route to a free-tier model"
    )


# --- rows ------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LedgerRow:
    """One immutable fact about a run.

    `seq` is assigned by the ledger on append and is monotonic within a run, so
    rows can be ordered without trusting wall-clock timestamps (which move
    backwards under NTP correction and are useless for ordering fast events).
    """

    run_id: str
    seq: int
    ts: float
    kind: str  # llm_call | cache_hit | gate | containment | success | abort
    agent_id: str = ""
    stage: str = ""
    provider: str = ""
    model: str = ""
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    would_have_cost: float = 0.0  # cache hits: what this call would have cost
    # Taint flag. True when the response came from the simulated provider
    # rather than a real one. Carried on the ROW rather than inferred from
    # configuration, so a report built from these rows cannot present
    # synthetic results as real ones no matter who builds it (ADR-012).
    simulated: bool = False
    detail: dict[str, Any] = field(default_factory=dict)


class Ledger(Protocol):
    """Append-only fact log. No update, no delete — by design, not by omission."""

    def append(self, row: LedgerRow) -> None: ...
    def rows(self) -> list[LedgerRow]: ...
    def next_seq(self) -> int: ...


class InMemoryLedger:
    """Non-durable ledger for tests. Same contract, no file."""

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._rows: list[LedgerRow] = []
        self._lock = threading.Lock()

    def append(self, row: LedgerRow) -> None:
        with self._lock:
            self._rows.append(row)

    def rows(self) -> list[LedgerRow]:
        with self._lock:
            return list(self._rows)

    def next_seq(self) -> int:
        with self._lock:
            return len(self._rows)


class JsonlLedger:
    """Durable append-only ledger: one JSON object per line, flushed per row.

    Why JSONL rather than a table: append-only is the file format's native mode,
    so immutability is a property of the medium instead of a constraint someone
    has to remember to enforce. It survives a hard kill mid-run with at most a
    torn final line, which `verify()` reports rather than hides. Cross-run
    queries move to Postgres in the cloud phase; a single run's ledger is small
    enough that a file is the honest choice.

    Flushed and fsynced on every append. That is slow relative to buffering and
    it is the point: a ledger that loses the last N rows when an agent is killed
    is exactly the ledger that cannot be trusted about a chaos run.
    """

    def __init__(self, run_id: str, path: str | Path) -> None:
        self.run_id = run_id
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._rows: list[LedgerRow] = []
        self._lock = threading.Lock()
        self._fh = self.path.open("a", encoding="utf-8")

    def append(self, row: LedgerRow) -> None:
        with self._lock:
            self._rows.append(row)
            self._fh.write(json.dumps(asdict(row), separators=(",", ":")) + "\n")
            self._fh.flush()
            os.fsync(self._fh.fileno())

    def rows(self) -> list[LedgerRow]:
        with self._lock:
            return list(self._rows)

    def next_seq(self) -> int:
        with self._lock:
            return len(self._rows)

    def close(self) -> None:
        with self._lock:
            if not self._fh.closed:
                self._fh.close()

    def read_durable(self) -> list[LedgerRow]:
        """Re-read rows from disk. The authoritative view after a crash."""
        out: list[LedgerRow] = []
        if not self.path.exists():
            return out
        with self.path.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(LedgerRow(**json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    # A torn final line from a hard kill. Reported by verify(),
                    # not silently dropped -- see verify() docstring.
                    continue
        return out


# --- accounting ------------------------------------------------------------


class SimulatedDataRefused(RuntimeError):
    """Raised when simulated rows reach something that must not accept them.

    Guards the boundary between "developing without keys" and "reporting a
    result". Eval reports, benchmarks, and improvement claims call
    `refuse_simulated` before doing anything with a ledger.
    """


def refuse_simulated(report: dict[str, Any], *, context: str) -> None:
    """Abort if a report carries simulated rows.

    Called by anything that publishes a number. The check is on the DATA rather
    than on configuration, so it holds even when a run was misconfigured, when
    an env var was set three shells ago, or when someone reuses a ledger file
    from a development session by mistake.
    """
    if report.get("simulated"):
        raise SimulatedDataRefused(
            f"{context} refused: {report.get('simulated_rows', '?')} of "
            f"{report.get('rows', '?')} ledger rows came from the simulated "
            f"provider. Simulated runs exist to develop against, not to report "
            f"from. Configure a real provider key and re-run."
        )


class CeilingExceeded(RuntimeError):
    """Run cost hit its hard ceiling.

    Carries the itemised report, because a bare 'budget exceeded' tells you
    nothing about which provider or stage burned it.
    """

    def __init__(self, message: str, report: dict[str, Any]) -> None:
        super().__init__(message)
        self.report = report


class CostAccount:
    """Charges calls to a ledger and enforces a hard USD ceiling.

    ANATOMY: ceiling_usd
      Hard upper bound on what one run may spend. Checked BEFORE a call is
      issued using a conservative estimate, and again after the real usage is
      known. Breach raises CeilingExceeded, which callers turn into a clean
      aborted run with a report -- never a truncated run that still emits
      numbers. Default 0.05 is chosen, not derived: roughly 180 calls at paid
      overflow rates, enough headroom for a run whose bulk rides free tiers, and
      tight enough that the run cannot succeed by giving up and paying. Raising
      it is a config change; the point is that a bound exists at all, because
      without one the caching and routing work never has to actually finish.

    ANATOMY: reserve_usd
      Fraction of the ceiling held back so the abort path itself can afford to
      run (writing the report, escalating to a human). Why 2%: large enough to
      cover a handful of small calls, small enough not to distort the budget.
    """

    def __init__(
        self,
        ledger: Ledger,
        run_id: str,
        *,
        ceiling_usd: float = 0.05,
        reserve_frac: float = 0.02,
    ) -> None:
        self.ledger = ledger
        self.run_id = run_id
        self.ceiling_usd = ceiling_usd
        self.reserve_usd = ceiling_usd * reserve_frac

    # -- computed, never counted --------------------------------------------

    def total_cost(self) -> float:
        """Sum of every row. Not a counter -- see module docstring."""
        return sum(r.cost_usd for r in self.ledger.rows())

    def cache_savings(self) -> float:
        return sum(
            r.would_have_cost for r in self.ledger.rows() if r.kind == "cache_hit"
        )

    def remaining(self) -> float:
        return max(0.0, self.ceiling_usd - self.reserve_usd - self.total_cost())

    # -- charging -----------------------------------------------------------

    def _row(self, kind: str, **kw: Any) -> LedgerRow:
        return LedgerRow(
            run_id=self.run_id,
            seq=self.ledger.next_seq(),
            ts=time.time(),
            kind=kind,
            **kw,
        )

    def precheck(self, provider: str, model: str, est_tokens: int) -> None:
        """Refuse a call that cannot fit in what is left.

        Checked before issuing rather than only after, because discovering the
        breach after the tokens are spent means the ceiling was advisory.
        """
        price = price_for(provider, model)
        # Conservative: assume the whole estimate bills at the output rate.
        estimate = price.cost(0, est_tokens)
        if estimate > self.remaining():
            raise CeilingExceeded(
                f"call would cost ~${estimate:.6f}, ${self.remaining():.6f} left "
                f"of ${self.ceiling_usd:.4f} ceiling",
                self.report(),
            )

    def charge_call(
        self,
        *,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        agent_id: str = "",
        stage: str = "",
        simulated: bool = False,
        detail: dict[str, Any] | None = None,
    ) -> float:
        """Record a model call and enforce the ceiling."""
        price = price_for(provider, model)
        cost = price.cost(tokens_in, tokens_out)
        self.ledger.append(
            self._row(
                "llm_call",
                agent_id=agent_id,
                stage=stage,
                provider=provider,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=cost,
                simulated=simulated,
                detail=detail or {},
            )
        )
        total = self.total_cost()
        if total > self.ceiling_usd - self.reserve_usd:
            raise CeilingExceeded(
                f"run cost ${total:.6f} exceeded ceiling ${self.ceiling_usd:.4f}",
                self.report(),
            )
        return cost

    def charge_cache_hit(
        self,
        *,
        provider: str,
        model: str,
        tokens_in: int,
        tokens_out: int,
        agent_id: str = "",
        stage: str = "",
        simulated: bool = False,
    ) -> None:
        """Record a served-from-cache call at zero cost.

        The row exists so cache savings are a query, not an estimate.

        `simulated` is not optional in spirit. A cache entry created by the
        simulated provider and later served into a run carries the same taint
        the original call did; a hit that recorded simulated=False would
        launder synthetic output into a report that claims to be real, which is
        precisely what ADR-012 exists to prevent.
        """
        price = price_for(provider, model)
        self.ledger.append(
            self._row(
                "cache_hit",
                agent_id=agent_id,
                stage=stage,
                provider=provider,
                model=model,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=0.0,
                would_have_cost=price.cost(tokens_in, tokens_out),
                simulated=simulated,
            )
        )

    def record(self, kind: str, **kw: Any) -> None:
        """Record a non-billable fact: gate outcome, containment, success."""
        self.ledger.append(self._row(kind, **kw))

    # -- reporting ----------------------------------------------------------

    def report(self) -> dict[str, Any]:
        """Itemised breakdown. Every figure here is an aggregate over rows."""
        rows = self.ledger.rows()
        by_provider: dict[str, float] = defaultdict(float)
        by_stage: dict[str, float] = defaultdict(float)
        by_model: dict[str, float] = defaultdict(float)
        calls = cache_hits = tokens_in = tokens_out = 0

        for r in rows:
            if r.kind == "llm_call":
                calls += 1
                by_provider[r.provider] += r.cost_usd
                by_stage[r.stage or "-"] += r.cost_usd
                by_model[r.model] += r.cost_usd
                tokens_in += r.tokens_in
                tokens_out += r.tokens_out
            elif r.kind == "cache_hit":
                cache_hits += 1

        attempts = calls + cache_hits
        simulated_rows = sum(1 for r in rows if r.simulated)
        return {
            "run_id": self.run_id,
            # Taint propagates from rows to report. A report is simulated if
            # ANY row in it is, because a run that mixed real and synthetic
            # calls is not a real run -- it is a run whose numbers mean nothing
            # in particular, and saying so is the only honest option.
            "simulated": simulated_rows > 0,
            "simulated_rows": simulated_rows,
            "ceiling_usd": self.ceiling_usd,
            "total_usd": round(sum(r.cost_usd for r in rows), 8),
            "remaining_usd": round(self.remaining(), 8),
            "cache_savings_usd": round(self.cache_savings(), 8),
            "cache_hit_rate": round(cache_hits / attempts, 4) if attempts else 0.0,
            "llm_calls": calls,
            "cache_hits": cache_hits,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "by_provider": {k: round(v, 8) for k, v in sorted(by_provider.items())},
            "by_model": {k: round(v, 8) for k, v in sorted(by_model.items())},
            "by_stage": {k: round(v, 8) for k, v in sorted(by_stage.items())},
            "rows": len(rows),
        }

    def verify(self) -> dict[str, Any]:
        """Check the in-process view against what actually reached disk.

        Returns a discrepancy report rather than raising: after a hard kill the
        honest outcome is "the last row was torn", stated plainly, not an
        exception that makes the run look like a code failure.
        """
        durable = getattr(self.ledger, "read_durable", None)
        if durable is None:
            return {"durable": False, "reason": "ledger has no durable backing"}
        disk_rows = durable()
        mem_rows = self.ledger.rows()
        return {
            "durable": True,
            "rows_in_memory": len(mem_rows),
            "rows_on_disk": len(disk_rows),
            "cost_in_memory": round(sum(r.cost_usd for r in mem_rows), 8),
            "cost_on_disk": round(sum(r.cost_usd for r in disk_rows), 8),
            "match": len(mem_rows) == len(disk_rows),
        }
