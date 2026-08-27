"""Between runs: consolidate what was learned, and choose what to try next.

Two passes that only make sense between runs, not during one.

CONSOLIDATION prunes the library and rewrites prompts. It runs between runs
because a library that changes mid-run would mean two agents in the same run
were graded against different starting conditions, and the run's own numbers
would stop being comparable to each other.

CURRICULUM picks the next task's difficulty from measured ability. Without it,
tasks must be hand-fed forever, and a system that needs a human to choose every
problem is not being thrown at unknown tasks.

THE GUARD THAT MATTERS: consolidation must never lower the control-arm score.
A prompt rewrite that helps the treatment arm and hurts the baseline is not an
improvement, it is the system learning to look better on its own benchmark.
Every proposed change is checked against the control and reverted if it
regresses.
"""

from __future__ import annotations

import logging
import statistics
from dataclasses import dataclass, field
from typing import Any

from swarmd.swarm.skills import Skill, SkillLibrary

logger = logging.getLogger(__name__)


# --- prompt versioning -----------------------------------------------------


@dataclass
class PromptVersion:
    """One revision of a stage prompt, with the evidence that motivated it."""

    version: int
    text: str
    rationale: str = ""
    control_score: float | None = None
    treatment_score: float | None = None

    @property
    def delta(self) -> float | None:
        if self.control_score is None or self.treatment_score is None:
            return None
        return self.treatment_score - self.control_score


class PromptHistory:
    """Versioned prompts with one-command rollback.

    Prompt changes are artifacts, not chat history: a prompt that drifted
    through six untracked edits cannot be reasoned about, and the question
    "what changed when the pass rate dropped" has no answer.
    """

    def __init__(self, stage: str, initial: str) -> None:
        self.stage = stage
        self.versions: list[PromptVersion] = [
            PromptVersion(0, initial, "initial")
        ]

    @property
    def current(self) -> PromptVersion:
        return self.versions[-1]

    def propose(self, text: str, rationale: str) -> PromptVersion:
        version = PromptVersion(len(self.versions), text, rationale)
        self.versions.append(version)
        return version

    def rollback(self, to: int | None = None) -> PromptVersion:
        """Revert to a prior version. Never deletes: the bad version stays in
        the record, because 'we tried that and it regressed' is information."""
        target = to if to is not None else max(0, len(self.versions) - 2)
        if not 0 <= target < len(self.versions):
            raise IndexError(f"no version {target} for stage {self.stage!r}")
        reverted = self.versions[target]
        self.versions.append(
            PromptVersion(
                len(self.versions),
                reverted.text,
                f"rollback to v{target}: {reverted.rationale}",
            )
        )
        return self.current

    def to_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "current_version": self.current.version,
            "history": [
                {
                    "version": v.version,
                    "rationale": v.rationale,
                    "delta": v.delta,
                    "preview": v.text[:120],
                }
                for v in self.versions
            ],
        }


# --- consolidation ---------------------------------------------------------


@dataclass
class ConsolidationReport:
    pruned: list[str] = field(default_factory=list)
    reverted: list[str] = field(default_factory=list)
    kept: int = 0
    library_before: int = 0
    library_after: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pruned": self.pruned,
            "reverted": self.reverted,
            "kept": self.kept,
            "library_before": self.library_before,
            "library_after": self.library_after,
        }


class Consolidator:
    """Between-run maintenance of the library and the prompts.

    ANATOMY: min_control_delta
      How much a change may lower the control-arm score before it is reverted.
      Why 0.0: any control regression means the change made the system worse at
      the task and better only at its own benchmark. A tolerance above zero
      would let exactly that accumulate one small step at a time.
    """

    def __init__(
        self,
        library: SkillLibrary,
        *,
        min_uses: int = 5,
        min_success_rate: float = 0.3,
        min_control_delta: float = 0.0,
    ) -> None:
        self.library = library
        self.min_uses = min_uses
        self.min_success_rate = min_success_rate
        self.min_control_delta = min_control_delta
        self.prompts: dict[str, PromptHistory] = {}

    def register_prompt(self, stage: str, text: str) -> PromptHistory:
        history = self.prompts.setdefault(stage, PromptHistory(stage, text))
        return history

    def apply_prompt_change(
        self,
        stage: str,
        text: str,
        rationale: str,
        *,
        control_before: float,
        control_after: float,
    ) -> bool:
        """Accept a prompt rewrite only if the control arm did not regress.

        Returns True when kept. The check is against the CONTROL, not the
        treatment: a change that helps only when skills are on has improved the
        system's ability to score on its own benchmark, which is the failure
        this guard exists for.
        """
        history = self.prompts.get(stage)
        if history is None:
            raise KeyError(f"unregistered prompt stage: {stage!r}")

        version = history.propose(text, rationale)
        version.control_score = control_after
        version.treatment_score = control_before

        if control_after < control_before - self.min_control_delta:
            history.rollback()
            logger.warning(
                "consolidation reverted %s: control %.3f -> %.3f",
                stage, control_before, control_after,
            )
            return False
        return True

    def consolidate(self) -> ConsolidationReport:
        """Prune skills that have demonstrated a poor record."""
        report = ConsolidationReport(library_before=len(self.library.approved()))
        retired = self.library.prune(
            min_uses=self.min_uses, min_success_rate=self.min_success_rate
        )
        report.pruned = [s.skill_id for s in retired]
        report.library_after = len(self.library.approved())
        report.kept = report.library_after
        return report

    def report(self) -> dict[str, Any]:
        return {
            "prompts": {
                stage: history.to_dict() for stage, history in self.prompts.items()
            },
            "library": self.library.stats(),
        }


# --- curriculum ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Frontier:
    """The measured edge of current ability."""

    pass_rate: float
    samples: int
    difficulty: float
    verdict: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "pass_rate": round(self.pass_rate, 4),
            "samples": self.samples,
            "difficulty": round(self.difficulty, 3),
            "verdict": self.verdict,
        }


class Curriculum:
    """Proposes the next task's difficulty from measured pass rates.

    ANATOMY: target_band
      The pass-rate window the curriculum steers toward. Why 0.4-0.7: above
      0.7 the tasks are too easy to produce a learning signal -- everything
      passes and nothing distinguishes a good approach from a lucky one. Below
      0.4 almost everything dead-letters, which burns quota generating failures
      that teach nothing. The band is where outcomes actually vary.

    ANATOMY: min_samples
      Runs required before adjusting. Why 5: below that the pass rate is mostly
      noise, and a curriculum that chases noise oscillates between trivial and
      impossible instead of converging.

    ANATOMY: step
      How far difficulty moves per adjustment. Why 0.1: small enough that one
      unlucky batch does not swing the system to the other extreme, large
      enough to cross the band in a handful of sessions.
    """

    def __init__(
        self,
        *,
        target_low: float = 0.4,
        target_high: float = 0.7,
        min_samples: int = 5,
        step: float = 0.1,
        difficulty: float = 0.5,
    ) -> None:
        self.target_low = target_low
        self.target_high = target_high
        self.min_samples = min_samples
        self.step = step
        self.difficulty = difficulty
        self.history: list[Frontier] = []

    def observe(self, outcomes: list[bool]) -> Frontier:
        """Update the frontier from a batch of pass/fail results."""
        samples = len(outcomes)
        pass_rate = (
            statistics.fmean([1.0 if o else 0.0 for o in outcomes])
            if outcomes else 0.0
        )

        if samples < self.min_samples:
            verdict = "insufficient evidence"
        elif pass_rate > self.target_high:
            self.difficulty = min(1.0, self.difficulty + self.step)
            verdict = "too easy: raising difficulty"
        elif pass_rate < self.target_low:
            self.difficulty = max(0.0, self.difficulty - self.step)
            verdict = "too hard: lowering difficulty"
        else:
            verdict = "in band: holding"

        frontier = Frontier(pass_rate, samples, self.difficulty, verdict)
        self.history.append(frontier)
        return frontier

    def select(self, tasks: list[Any], difficulty_of: Any) -> list[Any]:
        """Pick tasks nearest the current frontier.

        Nearest rather than above: the goal is tasks whose outcome is genuinely
        uncertain. Always picking harder ones produces a monotonic slide into
        tasks that all fail, which measures nothing.
        """
        if not tasks:
            return []
        return sorted(tasks, key=lambda t: abs(difficulty_of(t) - self.difficulty))

    def report(self) -> dict[str, Any]:
        return {
            "difficulty": round(self.difficulty, 3),
            "target_band": [self.target_low, self.target_high],
            "adjustments": len(self.history),
            "history": [f.to_dict() for f in self.history[-20:]],
        }


def distil_candidate(
    outputs: list[str], *, node: str, task: str
) -> tuple[str, str] | None:
    """Turn repeated successful outputs into a candidate skill instruction.

    Requires MORE THAN ONE success. A skill distilled from a single win is a
    superstition, and the red-team's library_poisoning detector flags exactly
    that -- checking here as well means the cheap check runs before the
    expensive proposal.
    """
    usable = [o.strip() for o in outputs if o and o.strip()]
    if len(usable) < 2:
        return None
    longest = max(usable, key=len)
    return (f"{node} approach", longest[:600])


def is_valid_skill(skill: Skill) -> bool:
    return bool(skill.instruction.strip()) and skill.usable
