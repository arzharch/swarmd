"""VerifyHarness: schema validators + resample checks for quality gates.

A verifier answers one question: is this item good enough to flow downstream?
Checks are composable predicates; the first failure short-circuits with a
structured reason (which feeds the failure taxonomy in the gates module).
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

Check = Callable[[dict[str, Any]], Awaitable[str | None]]
# Check returns None on pass, or a failure reason string.


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    failed_check: str | None = None
    reason: str | None = None


def schema_check(required_fields: list[str]) -> Check:
    """All required fields present and non-empty."""

    async def check(item: dict[str, Any]) -> str | None:
        missing = [
            f for f in required_fields
            if f not in item or item[f] is None or item[f] == ""
        ]
        if missing:
            return f"missing/empty fields: {missing}"
        return None

    return check


def range_check(field: str, low: float, high: float) -> Check:
    """Numeric field within [low, high]."""

    async def check(item: dict[str, Any]) -> str | None:
        value = item.get(field)
        if value is None:
            return f"field {field} absent for range check"
        if not isinstance(value, (int, float)):
            return f"field {field} not numeric: {value!r}"
        if not low <= value <= high:
            return f"field {field}={value} outside [{low}, {high}]"
        return None

    return check


def forbidden_content_check(banned_terms: list[str]) -> Check:
    """No banned term appears in any string value (shallow scan)."""

    async def check(item: dict[str, Any]) -> str | None:
        for key, value in item.items():
            if isinstance(value, str):
                lowered = value.lower()
                for term in banned_terms:
                    if term.lower() in lowered:
                        return f"banned term {term!r} in field {key}"
        return None

    return check


class VerifyHarness:
    """Runs an ordered list of checks; first failure wins."""

    def __init__(self, name: str, checks: list[Check] | None = None) -> None:
        self.name = name
        self.checks: list[Check] = checks or []

    def add_check(self, check: Check) -> VerifyHarness:
        self.checks.append(check)
        return self

    async def verify(self, item: dict[str, Any]) -> VerifyResult:
        for i, check in enumerate(self.checks):
            reason = await check(item)
            if reason is not None:
                return VerifyResult(ok=False, failed_check=f"#{i}", reason=reason)
        return VerifyResult(ok=True)
