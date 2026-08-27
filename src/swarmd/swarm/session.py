"""A session: many runs, with learning happening between them.

A single run cannot learn. It retrieves whatever the library already holds and
adds nothing usable, because a candidate skill has to clear a human first. The
loop only closes across runs, and this is the object that closes it:

    run task -> distill candidates -> HUMAN APPROVES -> consolidate -> pick the
    next task at the measured frontier -> run again

Three things happen between runs and deliberately not during one:

  CONSOLIDATE  prune skills with a demonstrated poor record, and apply prompt
               changes only if they do not lower the control-arm score. Doing
               this mid-run would mean two agents in the same run were graded
               against different starting conditions, and the run's own numbers
               would stop being comparable to each other.
  CURRICULUM   move difficulty toward the band where outcomes actually vary.
  REPORT       cost per solved task and first-pass rate over the session, which
               is the curve — and it is emitted with the ablation state
               attached, because a curve without one means nothing (ADR-007).

WHAT THIS DOES NOT CLAIM. Running a session does not demonstrate improvement.
It produces the data an improvement claim would be made from, and `swarmd eval`
is what makes the claim, only against a paired control. A rising line here with
skills enabled and nothing to compare it to is exactly the artifact this
project exists to avoid producing.
"""

from __future__ import annotations

import logging
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from swarmd.swarm.consolidate import Consolidator, Curriculum
from swarmd.swarm.skills import SkillLibrary

logger = logging.getLogger(__name__)


@dataclass
class SessionRun:
    """One run's contribution to the session record."""

    index: int
    task: str
    status: str
    solved: bool
    cost_usd: float
    duration_s: float
    first_pass: bool
    skills_retrieved: int
    skills_proposed: int
    contained: int
    difficulty: float
    simulated: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "task": self.task[:120],
            "status": self.status,
            "solved": self.solved,
            "cost_usd": round(self.cost_usd, 8),
            "duration_s": round(self.duration_s, 2),
            "first_pass": self.first_pass,
            "skills_retrieved": self.skills_retrieved,
            "skills_proposed": self.skills_proposed,
            "contained": self.contained,
            "difficulty": round(self.difficulty, 3),
            "simulated": self.simulated,
        }


@dataclass
class SessionReport:
    runs: list[SessionRun] = field(default_factory=list)
    consolidations: list[dict[str, Any]] = field(default_factory=list)
    frontier: list[dict[str, Any]] = field(default_factory=list)
    started_ts: float = field(default_factory=time.time)
    duration_s: float = 0.0
    skills_enabled: bool = True

    def window(self, start: int, end: int) -> dict[str, Any]:
        """Aggregate a slice of the session.

        Windows rather than a running average: the question is whether the last
        ten runs differ from the first ten, and a running average smears the
        transition across the whole series.
        """
        chunk = self.runs[start:end]
        if not chunk:
            return {"runs": 0}
        solved = [r for r in chunk if r.solved]
        return {
            "runs": len(chunk),
            "solved": len(solved),
            "success_rate": round(len(solved) / len(chunk), 4),
            "cost_per_solved": (
                round(sum(r.cost_usd for r in chunk) / len(solved), 8)
                if solved else None
            ),
            "first_pass_rate": round(
                statistics.fmean([1.0 if r.first_pass else 0.0 for r in chunk]), 4
            ),
            "mean_retrieved": round(
                statistics.fmean([float(r.skills_retrieved) for r in chunk]), 2
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        total = len(self.runs)
        third = max(1, total // 3)
        simulated = any(r.simulated for r in self.runs)
        return {
            "runs": total,
            "duration_s": round(self.duration_s, 2),
            "simulated": simulated,
            # The ablation state travels with the numbers. A curve with skills
            # enabled and nothing to compare it against is not evidence.
            "skills_enabled": self.skills_enabled,
            "first_third": self.window(0, third),
            "last_third": self.window(total - third, total),
            "consolidations": self.consolidations,
            "frontier": self.frontier,
            "detail": [r.to_dict() for r in self.runs],
            "claim": (
                "This session produced data, not a claim. Run `swarmd eval` "
                "with a paired control arm to compare."
            ),
        }

    def render(self) -> str:
        data = self.to_dict()
        first, last = data["first_third"], data["last_third"]
        lines = [
            f"session   {data['runs']} runs in {data['duration_s']}s",
            f"arm       {'treatment (skills on)' if data['skills_enabled'] else 'control (skills off)'}",
        ]
        if data["simulated"]:
            lines.append("SIMULATED synthetic provider -- these figures are not evidence")
        lines += [
            "",
            f"{'':<16}{'first third':>14}{'last third':>14}",
            f"{'runs':<16}{first.get('runs', 0):>14}{last.get('runs', 0):>14}",
            _row("success rate", _pct(first.get("success_rate")),
                 _pct(last.get("success_rate"))),
            _row("first-pass rate", _pct(first.get("first_pass_rate")),
                 _pct(last.get("first_pass_rate"))),
            _row("$/solved", _usd(first.get("cost_per_solved")),
                 _usd(last.get("cost_per_solved"))),
            _row("skills used", str(first.get("mean_retrieved", 0)),
                 str(last.get("mean_retrieved", 0))),
            "",
            data["claim"],
        ]
        return "\n".join(lines)


def _row(label: str, first: str, last: str) -> str:
    return f"{label:<16}{first:>14}{last:>14}"


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _usd(value: float | None) -> str:
    return "—" if value is None else f"${value:.6f}"


class SwarmSession:
    """Runs a sequence of tasks with learning between them.

    ANATOMY: consolidate_every
      Runs between consolidation passes. Why 5: pruning needs several uses per
      skill before the record means anything (the library's own floor is 5
      uses), and consolidating after every run would retire skills on one bad
      draw. Larger intervals let a harmful skill persist longer.

    ANATOMY: auto_approve
      Approves distilled skills without a human. DEVELOPMENT ONLY and off by
      default: the human gate is the control that stops the library poisoning
      itself, and a session that approves its own skills has removed the one
      thing standing between a superstition and every future run. Exists so the
      loop can be exercised end to end without a person in it, and every
      auto-approval is recorded in the audit trail with actor `auto-approve`
      so a later reader can see the gate was bypassed.
    """

    def __init__(
        self,
        run_factory: Any,
        library: SkillLibrary,
        *,
        approvals: Any = None,
        consolidate_every: int = 5,
        auto_approve: bool = False,
        curriculum: Curriculum | None = None,
        skills_enabled: bool = True,
    ) -> None:
        self.run_factory = run_factory
        self.library = library
        self.approvals = approvals
        self.consolidate_every = consolidate_every
        self.auto_approve = auto_approve
        self.curriculum = curriculum or Curriculum()
        self.consolidator = Consolidator(library)
        self.skills_enabled = skills_enabled
        if auto_approve:
            logger.warning(
                "auto-approve is ON: distilled skills enter the library with no "
                "human review. Development only."
            )

    async def run(self, tasks: list[str]) -> SessionReport:
        started = time.monotonic()
        report = SessionReport(skills_enabled=self.skills_enabled)
        outcomes: list[bool] = []

        for index, task in enumerate(tasks):
            result, run_report = await self.run_factory(task, index)

            # Counted from what workers ACTUALLY used, not from a separate
            # pre-run query. An earlier version queried the library with the
            # task text while workers retrieve per node instruction, so the
            # two used different queries and the session reported a number
            # that was not what happened -- it read 0 while skills were being
            # used. A reported figure that does not match the run is worse
            # than no figure.
            retrieved = (
                len({r.skill_used for r in result.results if r.skill_used})
                if self.skills_enabled
                else 0
            )

            solved = (
                result.status == "completed"
                and bool(result.results)
                and all(r.passed for r in result.results)
            )
            cost = run_report.get("cost", {})
            report.runs.append(
                SessionRun(
                    index=index,
                    task=task,
                    status=result.status,
                    solved=solved,
                    cost_usd=float(cost.get("total_usd", 0.0)),
                    duration_s=result.duration_s,
                    first_pass=(
                        all(r.attempts == 1 for r in result.results)
                        if result.results else False
                    ),
                    skills_retrieved=retrieved,
                    skills_proposed=len(result.proposed_skills),
                    contained=len(result.contained),
                    difficulty=self.curriculum.difficulty,
                    simulated=bool(cost.get("simulated", False)),
                )
            )
            outcomes.append(solved)

            if self.auto_approve:
                await self._auto_approve()

            if (index + 1) % self.consolidate_every == 0:
                report.consolidations.append(
                    {
                        "after_run": index,
                        **self.consolidator.consolidate().to_dict(),
                    }
                )
                report.frontier.append(
                    {"after_run": index, **self.curriculum.observe(outcomes).to_dict()}
                )
                outcomes = []

        report.duration_s = time.monotonic() - started
        return report

    async def _auto_approve(self) -> None:
        """Approve pending skills without a human. Development only.

        Goes through the same gate and audit trail as a human decision, with
        `auto-approve` as the actor, so the bypass is visible in the record
        rather than indistinguishable from review.
        """
        if self.approvals is None:
            for skill in self.library.pending():
                self.library.approve(skill.skill_id, actor="auto-approve")
            return

        from swarmd.hitl.skill_gate import SkillGate

        gate = SkillGate(self.approvals, self.library)
        for request in await gate.pending():
            await gate.decide(request.request_id, "approve", actor="auto-approve")
