"""Evaluation: the artifact the project is judged on.

Everything else exists to make these numbers mean something. Two commitments
are enforced in code rather than asserted in prose (ADR-007):

  1. **No claim without a control.** An improvement figure requires a paired
     control run: identical tasks, identical seeds, skills disabled. The
     harness refuses to emit a delta without one.
  2. **Overlapping intervals are a non-result, and say so.** When the
     confidence intervals overlap the report prints "no measured improvement",
     in those words. A non-result that reads as a soft positive is worse than
     no measurement at all, because it is quotable.

Statistics, and why these choices:

  BOOTSTRAP intervals rather than a t-interval. Success rate is a proportion
  over a handful of runs and cost-per-solved-task is heavily skewed by the
  occasional expensive failure; neither is remotely normal at n=5, and a
  t-interval on skewed data at that size understates the spread in exactly the
  direction that flatters a result.

  PAIRED comparison rather than two independent samples. Task difficulty varies
  far more than the skills effect does, so comparing arm means across different
  task draws buries the signal in task noise. Pairing on (task, seed) removes
  it.

  COST PER SOLVED TASK, not cost per task. A run that fails cheaply is not
  efficient. Dividing by solves is the number that survives the obvious
  follow-up question.
"""

from __future__ import annotations

import json
import logging
import random
import statistics
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from swarmd.ledger import refuse_simulated

logger = logging.getLogger(__name__)

# Bootstrap resamples. Why 2000: the interval stabilises to two decimal places
# by ~1000 and costs microseconds, so there is no reason to be stingy; beyond
# ~10000 nothing changes.
BOOTSTRAP_RESAMPLES = 2000
CONFIDENCE = 0.95


@dataclass(frozen=True, slots=True)
class Task:
    """One evaluation task.

    `arm` records where the task came from. The public arm exists to answer
    "is this self-graded?" and the custom arm to answer "does it handle what it
    was not built for", and mixing them in a single figure would let a strong
    result on one hide a weak result on the other.
    """

    task_id: str
    prompt: str
    arm: str = "custom"        # public | custom
    domain: str = "general"
    seed: int = 0


@dataclass
class TaskOutcome:
    task_id: str
    arm: str
    domain: str
    seed: int
    treatment: bool            # True = skills enabled
    solved: bool
    cost_usd: float
    tokens: int
    duration_s: float
    first_pass: bool           # passed without any repair round
    containments: int
    status: str
    simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- statistics ------------------------------------------------------------


def bootstrap_ci(
    values: list[float],
    *,
    resamples: int = BOOTSTRAP_RESAMPLES,
    confidence: float = CONFIDENCE,
    seed: int = 12345,
) -> tuple[float, float]:
    """Percentile bootstrap interval for the mean.

    Seeded so a report is reproducible: an interval that moves between two runs
    of the same analysis is not something anyone can check.
    """
    if not values:
        return (0.0, 0.0)
    if len(values) == 1:
        return (values[0], values[0])
    rng = random.Random(seed)
    n = len(values)
    means = sorted(
        statistics.fmean(rng.choices(values, k=n)) for _ in range(resamples)
    )
    lower = means[int((1 - confidence) / 2 * resamples)]
    upper = means[min(resamples - 1, int((1 + confidence) / 2 * resamples))]
    return (round(lower, 6), round(upper, 6))


def intervals_overlap(
    a: tuple[float, float], b: tuple[float, float]
) -> bool:
    return a[0] <= b[1] and b[0] <= a[1]


@dataclass
class ArmSummary:
    label: str
    runs: int
    solved: int
    success_rate: float
    success_ci: tuple[float, float]
    cost_per_solved: float | None
    cost_ci: tuple[float, float]
    first_pass_rate: float
    mean_tokens: float
    mean_duration_s: float
    containments: int

    def to_dict(self) -> dict[str, Any]:
        return {
            **asdict(self),
            "success_ci": list(self.success_ci),
            "cost_ci": list(self.cost_ci),
        }


def summarise(outcomes: list[TaskOutcome], label: str) -> ArmSummary:
    if not outcomes:
        return ArmSummary(label, 0, 0, 0.0, (0.0, 0.0), None, (0.0, 0.0),
                          0.0, 0.0, 0.0, 0)
    solved = [o for o in outcomes if o.solved]
    successes = [1.0 if o.solved else 0.0 for o in outcomes]
    # Cost per SOLVED task: a run that failed cheaply is not efficient.
    total_cost = sum(o.cost_usd for o in outcomes)
    per_solved = (total_cost / len(solved)) if solved else None
    solved_costs = [o.cost_usd for o in solved]

    return ArmSummary(
        label=label,
        runs=len(outcomes),
        solved=len(solved),
        success_rate=round(statistics.fmean(successes), 4),
        success_ci=bootstrap_ci(successes),
        cost_per_solved=(round(per_solved, 8) if per_solved is not None else None),
        cost_ci=bootstrap_ci(solved_costs) if solved_costs else (0.0, 0.0),
        first_pass_rate=round(
            statistics.fmean([1.0 if o.first_pass else 0.0 for o in outcomes]), 4
        ),
        mean_tokens=round(statistics.fmean([float(o.tokens) for o in outcomes]), 1),
        mean_duration_s=round(
            statistics.fmean([o.duration_s for o in outcomes]), 2
        ),
        containments=sum(o.containments for o in outcomes),
    )


class MissingControlArm(RuntimeError):
    """An improvement figure was requested with no paired control.

    Deliberately fatal. A curve with nothing to compare against is the single
    most common way self-improvement claims get made, and refusing to produce
    one is cheaper than arguing about it later.
    """


def compare(
    treatment: list[TaskOutcome], control: list[TaskOutcome]
) -> dict[str, Any]:
    """Paired comparison. Refuses to report a delta without a control."""
    if not control:
        raise MissingControlArm(
            "no control arm: an improvement claim requires a paired run with "
            "skills disabled, identical tasks and identical seeds (ADR-007)"
        )

    treated = summarise(treatment, "treatment")
    baseline = summarise(control, "control")

    # Pair on (task_id, seed). Task difficulty varies far more than the effect
    # being measured, so an unpaired comparison buries the signal in task noise.
    control_index = {(o.task_id, o.seed): o for o in control}
    paired_deltas: list[float] = []
    for outcome in treatment:
        match = control_index.get((outcome.task_id, outcome.seed))
        if match is not None:
            paired_deltas.append(
                (1.0 if outcome.solved else 0.0) - (1.0 if match.solved else 0.0)
            )

    delta = treated.success_rate - baseline.success_rate
    overlapping = intervals_overlap(treated.success_ci, baseline.success_ci)

    verdict = (
        "no measured improvement"
        if overlapping or not paired_deltas
        else ("improvement" if delta > 0 else "regression")
    )

    return {
        "verdict": verdict,
        "success_rate_delta": round(delta, 4),
        "paired_mean_delta": (
            round(statistics.fmean(paired_deltas), 4) if paired_deltas else None
        ),
        "paired_delta_ci": (
            list(bootstrap_ci(paired_deltas)) if paired_deltas else None
        ),
        "pairs": len(paired_deltas),
        "intervals_overlap": overlapping,
        "cost_per_solved_delta": (
            round(
                (treated.cost_per_solved or 0.0) - (baseline.cost_per_solved or 0.0),
                8,
            )
            if treated.cost_per_solved is not None
            and baseline.cost_per_solved is not None
            else None
        ),
        "note": (
            "Confidence intervals overlap; this is reported as a non-result "
            "rather than a small positive."
            if overlapping
            else "Intervals are disjoint at 95%."
        ),
    }


# --- the harness -----------------------------------------------------------


@dataclass
class EvalReport:
    started_ts: float = field(default_factory=time.time)
    outcomes: list[TaskOutcome] = field(default_factory=list)
    repeats: int = 1
    duration_s: float = 0.0

    def by_arm(self, arm: str | None = None) -> list[TaskOutcome]:
        return [o for o in self.outcomes if arm is None or o.arm == arm]

    def to_dict(self) -> dict[str, Any]:
        arms_present = sorted({o.arm for o in self.outcomes})
        simulated = any(o.simulated for o in self.outcomes)

        report: dict[str, Any] = {
            "started_ts": self.started_ts,
            "duration_s": round(self.duration_s, 2),
            "repeats": self.repeats,
            "total_runs": len(self.outcomes),
            "simulated": simulated,
            "arms": {},
        }
        for arm in arms_present:
            outcomes = self.by_arm(arm)
            treatment = [o for o in outcomes if o.treatment]
            control = [o for o in outcomes if not o.treatment]
            entry: dict[str, Any] = {
                "treatment": summarise(treatment, "treatment").to_dict(),
                "control": summarise(control, "control").to_dict(),
            }
            try:
                entry["comparison"] = compare(treatment, control)
            except MissingControlArm as exc:
                entry["comparison"] = {"verdict": "unavailable", "reason": str(exc)}
            report["arms"][arm] = entry
        return report

    def render(self) -> str:
        """Human-readable summary. What actually gets read."""
        data = self.to_dict()
        lines = [
            "swarmd eval",
            (f"  runs={data['total_runs']} repeats={data['repeats']} "
             f"duration={data['duration_s']}s"),
        ]
        if data["simulated"]:
            lines.append(
                "  *** SIMULATED DATA -- these figures are not evidence ***"
            )
        for arm, entry in data["arms"].items():
            treated = entry["treatment"]
            baseline = entry["control"]
            comparison = entry["comparison"]
            lines += [
                "",
                f"  [{arm}]",
                (f"    treatment  solved {treated['solved']}/{treated['runs']}  "
                 f"success {treated['success_rate']:.1%} "
                 f"CI[{treated['success_ci'][0]:.2f},"
                 f"{treated['success_ci'][1]:.2f}]  "
                 f"$/solved {_fmt(treated['cost_per_solved'])}  "
                 f"first-pass {treated['first_pass_rate']:.1%}"),
                (f"    control    solved {baseline['solved']}/{baseline['runs']}  "
                 f"success {baseline['success_rate']:.1%} "
                 f"CI[{baseline['success_ci'][0]:.2f},"
                 f"{baseline['success_ci'][1]:.2f}]  "
                 f"$/solved {_fmt(baseline['cost_per_solved'])}  "
                 f"first-pass {baseline['first_pass_rate']:.1%}"),
                f"    VERDICT: {comparison.get('verdict')}"
                + (
                    f"  delta={comparison.get('success_rate_delta'):+.3f}"
                    if comparison.get("success_rate_delta") is not None
                    else ""
                ),
            ]
            if comparison.get("note"):
                lines.append(f"    {comparison['note']}")
        return "\n".join(lines)

    def write_benchmarks(self, path: str | Path) -> None:
        """Generate BENCHMARKS.md. Never hand-edited.

        A hand-written benchmarks file drifts from the runs it claims to
        describe, and nobody notices because there is nothing to compare it to.
        """
        data = self.to_dict()
        refuse_simulated(
            {"simulated": data["simulated"], "rows": data["total_runs"],
             "simulated_rows": sum(1 for o in self.outcomes if o.simulated)},
            context="BENCHMARKS.md generation",
        )
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        lines = [
            "# Benchmarks",
            "",
            ("**Generated by `swarmd eval`. Do not edit by hand** -- a "
             "hand-written benchmarks file drifts from the runs it describes."),
            "",
            (f"Runs: {data['total_runs']} · repeats: {data['repeats']}"
             f" · wall clock: {data['duration_s']}s"),
            "",
            ("Every figure is computed from the append-only ledger (ADR-007), "
             "and no improvement is reported without its paired control arm."),
            "",
        ]
        for arm, entry in data["arms"].items():
            lines += [
                f"## {arm} arm",
                "",
                "| | treatment | control |",
                "|---|---|---|",
            ]
            treated, baseline = entry["treatment"], entry["control"]
            rows: list[tuple[str, str, Callable[[Any], str]]] = [
                ("runs", "runs", str),
                ("solved", "solved", str),
                ("success rate", "success_rate", lambda v: f"{v:.1%}"),
                ("cost per solved", "cost_per_solved", _fmt),
                ("first-pass rate", "first_pass_rate", lambda v: f"{v:.1%}"),
                ("mean tokens", "mean_tokens", str),
                ("containments", "containments", str),
            ]
            for label, key, fmt in rows:
                lines.append(
                    f"| {label} | {fmt(treated[key])} | {fmt(baseline[key])} |"
                )
            comparison = entry["comparison"]
            lines += [
                "",
                f"**Verdict: {comparison.get('verdict')}**",
                "",
                comparison.get("note", comparison.get("reason", "")),
                "",
            ]
        target.write_text("\n".join(lines), encoding="utf-8")

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _fmt(value: Any) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"${value:.6f}" if value < 1 else f"${value:.2f}"
    return str(value)


class Evaluator:
    """Runs both arms over a task suite.

    ANATOMY: repeats
      How many times each task is run per arm. Why default 5: below 3 a
      bootstrap interval is meaningless, and each repeat costs a full run
      against a ~45 req/min ceiling (docs/CAPACITY.md) -- 100 tasks x 2 arms x
      5 repeats is already about 75% of a day's free quota.
    """

    def __init__(self, run_factory: Any, *, repeats: int = 5) -> None:
        # run_factory(task, use_skills, seed) -> awaitable RunResult+report, so
        # this module never imports a provider and the harness is testable
        # without a network.
        self.run_factory = run_factory
        self.repeats = repeats

    async def evaluate(self, tasks: list[Task]) -> EvalReport:
        started = time.monotonic()
        report = EvalReport(repeats=self.repeats)

        for task in tasks:
            for repeat in range(self.repeats):
                seed = task.seed + repeat
                # Both arms, same task, same seed. The pairing is the point.
                for use_skills in (True, False):
                    outcome = await self._one(task, use_skills, seed)
                    report.outcomes.append(outcome)

        report.duration_s = time.monotonic() - started
        return report

    async def _one(self, task: Task, use_skills: bool, seed: int) -> TaskOutcome:
        result, run_report = await self.run_factory(task, use_skills, seed)
        cost = run_report.get("cost", {})
        passed = [r for r in result.results if r.passed]
        return TaskOutcome(
            task_id=task.task_id,
            arm=task.arm,
            domain=task.domain,
            seed=seed,
            treatment=use_skills,
            solved=(
                result.status == "completed"
                and bool(result.results)
                and len(passed) == len(result.results)
            ),
            cost_usd=float(cost.get("total_usd", 0.0)),
            tokens=int(cost.get("tokens_in", 0)) + int(cost.get("tokens_out", 0)),
            duration_s=result.duration_s,
            first_pass=all(r.attempts == 1 for r in result.results) if result.results
            else False,
            containments=len(result.contained),
            status=result.status,
            simulated=bool(cost.get("simulated", False)),
        )
