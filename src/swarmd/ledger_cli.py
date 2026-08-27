"""`swarmd ledger` and `swarmd run inspect` — reading the audit record back.

These exist because docs/RUNBOOK.md tells an on-call engineer to run them. A
runbook that names a command which does not exist is worse than a runbook with
a gap: the gap is visible, and the missing command is discovered at 3am by
someone who now has to improvise. Docs drift is a bug (SPEC cross-cutting
rule 7), and this closes it.

Everything here reads a ledger FILE rather than a live process, because the
situation these commands are for is one where the process is gone.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from swarmd.ledger import CostAccount, JsonlLedger, LedgerRow


class LedgerNotFound(FileNotFoundError):
    pass


def load(path: str | Path) -> list[LedgerRow]:
    target = Path(path)
    if not target.exists():
        raise LedgerNotFound(
            f"no ledger at {target}. Runs write one only when --ledger PATH is "
            f"passed; without it the record lives in memory and dies with the "
            f"process."
        )
    return JsonlLedger("read-only", target).read_durable()


def _account_for(rows: list[LedgerRow], ceiling: float) -> CostAccount:
    """Rebuild an accountant over rows read from disk.

    Aggregation lives in CostAccount, so reporting from a file and reporting
    from a live run go through exactly the same code. Two implementations of
    "what did this cost" is how the two answers start disagreeing.
    """
    from swarmd.ledger import InMemoryLedger

    run_id = rows[0].run_id if rows else "unknown"
    ledger = InMemoryLedger(run_id)
    for row in rows:
        ledger.append(row)
    return CostAccount(ledger, run_id, ceiling_usd=ceiling)


def report(path: str | Path, *, ceiling: float = 0.05) -> dict[str, Any]:
    rows = load(path)
    return _account_for(rows, ceiling).report()


def verify(path: str | Path) -> dict[str, Any]:
    """Check a ledger file for damage.

    A torn final line is the expected result of a hard kill mid-write, and is
    reported as such rather than treated as corruption -- the distinction
    matters when deciding whether a chaos run's numbers are usable.
    """
    target = Path(path)
    if not target.exists():
        raise LedgerNotFound(f"no ledger at {target}")

    total_lines = 0
    parsed = 0
    torn: list[int] = []
    with target.open(encoding="utf-8") as handle:
        for number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            total_lines += 1
            try:
                json.loads(line)
                parsed += 1
            except json.JSONDecodeError:
                torn.append(number)

    rows = load(path)
    seqs = [r.seq for r in rows]
    # Gaps mean rows are missing from the middle, which is a different and
    # much worse failure than a torn tail: the file was not merely truncated.
    gaps = [
        s for s in range(min(seqs), max(seqs) + 1) if s not in set(seqs)
    ] if seqs else []

    return {
        "path": str(target),
        "lines": total_lines,
        "parsed": parsed,
        "torn_lines": torn,
        "rows": len(rows),
        "seq_gaps": gaps[:20],
        "gap_count": len(gaps),
        # Intact means every line parsed AND the sequence is unbroken. A torn
        # tail alone still leaves the surviving rows trustworthy.
        "intact": not torn and not gaps,
        "tail_truncated": bool(torn) and not gaps,
    }


def inspect(path: str | Path) -> dict[str, Any]:
    """Everything a post-incident reader needs from one run's ledger."""
    rows = load(path)
    by_kind: dict[str, int] = defaultdict(int)
    by_agent: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"calls": 0, "cost": 0.0, "successes": 0, "contained": False}
    )
    criterion: dict[str, Any] = {}
    plan: dict[str, Any] = {}
    containments: list[dict[str, Any]] = []

    for row in rows:
        by_kind[row.kind] += 1
        if row.agent_id:
            entry = by_agent[row.agent_id]
            if row.kind == "llm_call":
                entry["calls"] += 1
                entry["cost"] += row.cost_usd
            if row.kind == "success":
                entry["successes"] += 1
            if row.kind == "containment":
                entry["contained"] = True
        if row.kind == "criterion_frozen":
            criterion = dict(row.detail)
        if row.kind == "plan_selected":
            plan = dict(row.detail)
        if row.kind == "containment":
            containments.append({"agent_id": row.agent_id, **row.detail})

    simulated = sum(1 for r in rows if r.simulated)
    return {
        "run_id": rows[0].run_id if rows else "unknown",
        "rows": len(rows),
        "simulated_rows": simulated,
        "simulated": simulated > 0,
        "criterion": criterion,
        "plan": plan,
        "by_kind": dict(sorted(by_kind.items())),
        "agents": len(by_agent),
        "containments": containments,
        "top_spenders": sorted(
            (
                {"agent_id": agent, **stats}
                for agent, stats in by_agent.items()
            ),
            key=lambda a: -float(a["cost"]),
        )[:10],
    }


# --- rendering -------------------------------------------------------------


def render_report(data: dict[str, Any]) -> str:
    lines = [
        f"run        {data['run_id']}",
        f"rows       {data['rows']}",
        f"cost       ${data['total_usd']:.6f} of ${data['ceiling_usd']} ceiling",
        (f"calls      {data['llm_calls']}  cache hits {data['cache_hits']} "
         f"({data['cache_hit_rate']:.0%})"),
        f"tokens     in {data['tokens_in']}  out {data['tokens_out']}",
        f"saved      ${data['cache_savings_usd']:.6f} by cache",
    ]
    if data.get("simulated"):
        lines.append(
            f"SIMULATED  {data['simulated_rows']} of {data['rows']} rows are "
             f"synthetic -- these figures are not evidence"
        )
    if data["by_provider"]:
        lines.append("")
        lines.append("by provider")
        for name, value in data["by_provider"].items():
            lines.append(f"  {name:<24} ${value:.6f}")
    if data["by_stage"]:
        lines.append("")
        lines.append("by stage")
        for name, value in data["by_stage"].items():
            lines.append(f"  {name:<24} ${value:.6f}")
    return "\n".join(lines)


def render_verify(data: dict[str, Any]) -> str:
    lines = [
        f"path       {data['path']}",
        f"lines      {data['lines']}  parsed {data['parsed']}",
        f"rows       {data['rows']}",
    ]
    if data["intact"]:
        lines.append("status     INTACT -- every line parsed, sequence unbroken")
    elif data["tail_truncated"]:
        lines.append(
            f"status     TAIL TRUNCATED at line(s) {data['torn_lines']}"
        )
        lines.append(
            "           Expected after a hard kill mid-write. Surviving rows "
            "are trustworthy."
        )
    else:
        lines.append(f"status     DAMAGED -- {data['gap_count']} sequence gap(s)")
        lines.append(
            "           Rows are missing from the middle, not just the tail. "
            "Aggregates from this file understate the run."
        )
        if data["seq_gaps"]:
            lines.append(f"           missing seq: {data['seq_gaps']}")
    return "\n".join(lines)


def render_inspect(data: dict[str, Any]) -> str:
    lines = [
        f"run        {data['run_id']}",
        f"rows       {data['rows']}  agents {data['agents']}",
    ]
    if data["simulated"]:
        lines.append(
            f"SIMULATED  {data['simulated_rows']} synthetic rows -- not evidence"
        )
    if data["criterion"]:
        lines.append(
            f"criterion  {data['criterion'].get('hash', '?')} "
             f"(attempts {data['criterion'].get('attempts', '?')})"
        )
    if data["plan"]:
        lines.append(
            f"plan       {data['plan'].get('hash', '?')} "
             f"({data['plan'].get('nodes', '?')} nodes, "
             f"width {data['plan'].get('width', '?')})"
        )
    lines.append("")
    lines.append("row kinds")
    for kind, count in data["by_kind"].items():
        lines.append(f"  {kind:<20} {count}")

    if data["containments"]:
        lines.append("")
        lines.append("containments")
        for entry in data["containments"]:
            lines.append(f"  {entry.get('agent_id', '?'):<10} {entry}")

    if data["top_spenders"]:
        lines.append("")
        lines.append("top spenders")
        for agent in data["top_spenders"]:
            flag = " [contained]" if agent["contained"] else ""
            lines.append(
                f"  {agent['agent_id']:<10} ${agent['cost']:.6f}  "
                 f"calls {agent['calls']}  successes {agent['successes']}{flag}"
            )
    return "\n".join(lines)
