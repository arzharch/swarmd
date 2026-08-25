"""Quality gates: verifier -> bounded repair -> requeue -> dead-letter.

Design notes:

- Every pipeline item passes through a gate before flowing downstream. A failing
  item enters a BOUNDED repair loop (the stage gets `max_repairs` attempts to fix
  it, e.g. via an LLM re-ask); still-failing items go to the dead-letter queue
  with full context — never silently forwarded downstream.
- Why bounded: an unbounded repair loop is a livelock dressed up as diligence.
  The bound converts "bad input" into a visible, countable outcome.
- The gate records a failure taxonomy (schema/range/content/exception) so runs
  can report WHERE quality breaks, not just THAT it broke.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

RepairFn = Callable[[dict[str, Any], str], Awaitable[dict[str, Any]]]
# RepairFn(item, failure_reason) -> repaired item


@dataclass(frozen=True)
class GateOutcome:
    ok: bool
    item: dict[str, Any]
    attempts: int
    reason: str | None = None


@dataclass
class DeadLetter:
    item: dict[str, Any]
    stage: str
    reason: str
    attempts: int


def classify_failure(reason: str) -> str:
    """Map a failure reason to a coarse taxonomy bucket."""
    r = reason.lower()
    if "missing" in r or "empty" in r or "absent" in r or "not numeric" in r:
        return "schema"
    if "outside" in r:
        return "range"
    if "banned" in r:
        return "content"
    return "other"


class QualityGate:
    """Verifier + bounded repair loop for one stage's output items."""

    def __init__(
        self,
        stage_name: str,
        verify_fn: Callable[[dict[str, Any]], Awaitable[Any]],
        *,
        max_repairs: int = 1,
        repair_fn: RepairFn | None = None,
    ) -> None:
        self.stage_name = stage_name
        self.verify_fn = verify_fn
        self.max_repairs = max_repairs
        self.repair_fn = repair_fn
        self.dead_letters: list[DeadLetter] = []
        self.passed = 0
        self.repaired = 0
        self.taxonomy: dict[str, int] = {}

    async def check(
        self, item: dict[str, Any], _verify_result: Any = None
    ) -> GateOutcome:
        """Run verification with optional bounded repair.

        verify_fn may return a VerifyResult (ok/reason) or raise; both paths are
        normalized into a (ok, reason) pair.
        """
        attempts = 0
        current = item
        while True:
            try:
                result = await self.verify_fn(current)
                ok = getattr(result, "ok", bool(result))
                reason = getattr(result, "reason", None) if not ok else None
            except Exception as exc:  # noqa: BLE001 - verifier bugs are failures too
                ok, reason = False, f"verifier exception: {exc}"

            if ok:
                self.passed += 1
                if attempts > 0:
                    self.repaired += 1
                return GateOutcome(ok=True, item=current, attempts=attempts)

            assert reason is not None
            bucket = classify_failure(reason)
            self.taxonomy[bucket] = self.taxonomy.get(bucket, 0) + 1

            if attempts >= self.max_repairs or self.repair_fn is None:
                dl = DeadLetter(
                    item=current, stage=self.stage_name, reason=reason, attempts=attempts
                )
                self.dead_letters.append(dl)
                return GateOutcome(ok=False, item=current, attempts=attempts, reason=reason)

            current = await self.repair_fn(current, reason)
            attempts += 1

    def summary(self) -> dict[str, Any]:
        return {
            "stage": self.stage_name,
            "passed": self.passed,
            "repaired": self.repaired,
            "dead_lettered": len(self.dead_letters),
            "taxonomy": dict(self.taxonomy),
        }
