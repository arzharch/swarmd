"""Criterion synthesis: propose, cross-check, attack, freeze.

The pipeline, and why each stage exists:

  PROPOSE     N agents independently author a criterion. Independent because
              one proposer cannot disagree with itself, and disagreement is the
              only signal that the task was ambiguous.
  CROSS-CHECK Compare proposals. Broad agreement means the task was legible.
              Irreconcilable disagreement ESCALATES to a human rather than
              silently picking one -- disagreement about what success means is
              information, not noise.
  ATTACK      A red-team tries to satisfy the merged criterion with degenerate
              output. If garbage passes, the criterion is rejected.
  FREEZE      Content-address it. The hash is a run output, so any result can
              be traced to exactly what it was graded against.

A weak criterion is worse than no criterion: it manufactures confident false
passes, and every downstream number -- gate pass rate, skill quality, the
learning curve -- inherits the lie. That is why the attack stage can fail the
whole task, and why exhausting the retry budget is an honest failure rather
than a licence to proceed.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from swarmd.swarm.criteria import (
    CHECK_PARAMS,
    Candidate,
    Check,
    CheckError,
    Criterion,
)

logger = logging.getLogger(__name__)

# --- adversarial candidates ------------------------------------------------
#
# Every degenerate shape a criterion must reject. These are the attack, and
# they are deliberately fixed rather than model-generated: a model asked to
# produce garbage produces creative garbage, which is a worse test than the
# boring garbage that actually slips through weak checks.


def degenerate_candidates(task: str) -> list[tuple[str, Candidate]]:
    """Named attacks, ordered roughly by how often each one succeeds."""
    return [
        ("empty", Candidate(output="")),
        ("whitespace", Candidate(output="   \n\t  \n ")),
        ("constant", Candidate(output="x" * 200)),
        ("repeated_token", Candidate(output=" ".join(["result"] * 80))),
        # Echoing the prompt is the classic false pass: it is long, non-empty,
        # lexically overlaps the task, and contains no work whatsoever.
        ("echo_task", Candidate(output=task * 3)),
        ("refusal", Candidate(output="I cannot help with that request.")),
        (
            "empty_json",
            Candidate(output="{}", artifacts={}, exit_code=0),
        ),
        (
            "null_valued_json",
            Candidate(
                output='{"result": null, "value": null, "answer": null}',
                artifacts={"result": None},
                exit_code=0,
            ),
        ),
        (
            "zero_artifacts",
            Candidate(
                output="done",
                artifacts={"accuracy": 0.0, "score": 0, "result": 0},
                exit_code=0,
            ),
        ),
    ]


@dataclass(frozen=True, slots=True)
class AttackReport:
    survived: bool
    breaches: tuple[str, ...] = ()

    def summary(self) -> str:
        if self.survived:
            return "criterion rejected all degenerate candidates"
        return f"criterion accepted garbage: {', '.join(self.breaches)}"


def attack(criterion: Criterion, task: str) -> AttackReport:
    """Try to pass the criterion with output that did no work."""
    breaches = [
        name
        for name, candidate in degenerate_candidates(task)
        if criterion.evaluate(candidate).passed
    ]
    # A criterion made only of trivially-satisfiable checks is reported as
    # breached even if the fixed attacks happen to miss it -- the attack list
    # is a sample, not a proof, and the structural signal is stronger.
    if not breaches and criterion.is_weak():
        breaches.append("only_trivial_checks")
    return AttackReport(not breaches, tuple(breaches))


# --- proposals -------------------------------------------------------------

def _schema_hint() -> str:
    """The proposal contract, generated from the checks themselves.

    The previous version showed `"params": {}` as the example and named the
    kinds in a pipe-separated blob. Real models copied the empty object
    faithfully, producing checks with no parameters -- which fail every
    candidate, so the criterion was unsatisfiable and every node failed
    forever. The simulated provider never reproduced it because its proposals
    were hand-written with complete parameters.

    Generated rather than written out so the prompt cannot drift from the code:
    a check whose parameters change updates the instructions with it.
    """
    lines = [
        "{",
        '  "description": "one sentence describing what success means",',
        '  "checks": [ {"kind": "...", "params": {...}} ]',
        "}",
        "",
        (
            "EVERY check needs its params. A check missing them fails every "
            "candidate and makes the whole criterion unsatisfiable."
        ),
        (
            "Values in <angle brackets> are PLACEHOLDERS describing what to "
            "supply. Derive every value from THIS task -- copying the "
            "placeholders, or inventing a file name or marker string the task "
            "never mentioned, produces a criterion nothing can satisfy."
        ),
    ]
    lines.extend(
        f'  {kind:<20} params example: {example}'
        for kind, example in CHECK_PARAMS.items()
    )
    return "\n".join(lines)


PROPOSAL_SCHEMA_HINT = _schema_hint()

PROPOSER_SYSTEM = (
    "You define what SUCCESS means for a task, before anyone attempts it. "
    "You do not solve the task. You produce machine-checkable criteria that "
    "empty, constant, or prompt-echoing output would FAIL. Prefer checks over "
    "concrete artifacts and exit codes to checks over prose.\n"
    "\n"
    "THE ARTIFACT CONTRACT, which your checks are graded against: a worker "
    "writes ONE flat JSON object to artifacts.json. Its top-level keys are the "
    "values being reported, so `artifact_exists` and `numeric_range` take one "
    "of those keys -- `accuracy`, `claims` -- never a file name. The graded "
    "output IS that object, so `json_parses` sees it directly."
)


@dataclass
class Proposal:
    """One agent's answer to 'how would we know this was done?'"""

    criterion: Criterion | None
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.criterion is not None


def parse_proposal(raw: str) -> Proposal:
    """Turn a model response into a Criterion, or record why it could not.

    Parse failures are data, not exceptions: a proposer that emits nonsense
    should reduce consensus rather than abort synthesis, because the other
    proposers may well have produced something usable.
    """
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return Proposal(None, raw, "no JSON object in response")
        payload = json.loads(raw[start : end + 1])
        criterion = Criterion.from_dict(payload)
        # A check missing its required parameters fails EVERY candidate, so a
        # proposal carrying one is not a weak criterion -- it is an
        # unsatisfiable one. Rejected here, at parse time, which is what
        # CheckError's own docstring has always claimed happens.
        broken = criterion.malformed()
        if broken:
            return Proposal(None, raw, f"malformed checks: {'; '.join(broken)}")
        return Proposal(criterion, raw)
    except (json.JSONDecodeError, CheckError, TypeError, ValueError) as exc:
        return Proposal(None, raw, str(exc))


# --- consensus -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Consensus:
    """What the proposers agreed on, and how much."""

    merged: Criterion | None
    agreement: float
    proposal_count: int
    valid_count: int
    escalate: bool
    reason: str = ""


def merge(proposals: Sequence[Proposal], *, min_agreement: float = 0.5) -> Consensus:
    """Union the checks that enough proposers independently asked for.

    ANATOMY: min_agreement
      Fraction of valid proposers that must include a check for it to enter the
      merged criterion. Why 0.5: a check only one of three proposers thought of
      is as likely to be a misreading of the task as an insight, and including
      it makes the criterion stricter on one agent's opinion. Requiring all
      proposers (1.0) collapses to the intersection, which drifts toward the
      weakest common denominator -- usually just `output_nonempty`, which is
      exactly the criterion the attack stage exists to reject.

    UNION of agreed checks, not intersection. Intersection produces the
    weakest criterion everyone could live with; union of the well-supported
    ones produces the strongest one nobody objected to.
    """
    valid = [p for p in proposals if p.ok and p.criterion is not None]
    if not valid:
        return Consensus(
            None, 0.0, len(proposals), 0, escalate=True,
            reason="no proposer produced a parseable criterion",
        )

    tally: dict[str, tuple[Check, int]] = {}
    for proposal in valid:
        assert proposal.criterion is not None
        # De-duplicate within a proposal so one agent listing a check twice
        # cannot manufacture agreement by itself.
        for canonical in {c.canonical(): c for c in proposal.criterion.checks}.items():
            key, check = canonical
            existing = tally.get(key)
            tally[key] = (check, (existing[1] if existing else 0) + 1)

    # ceil, not int: truncation makes 3 proposers at 0.5 require ONE vote,
    # which means every check any single proposer named is 'agreed' and the
    # consensus mechanism agrees on nothing. ceil gives a real majority.
    threshold = max(1, math.ceil(len(valid) * min_agreement))
    agreed = [check for check, count in tally.values() if count >= threshold]

    # Agreement score: how much of the total proposed check-mass was shared.
    total_mentions = sum(count for _, count in tally.values())
    shared_mentions = sum(
        count for _, count in tally.values() if count >= threshold
    )
    agreement = shared_mentions / total_mentions if total_mentions else 0.0

    if not agreed:
        return Consensus(
            None, agreement, len(proposals), len(valid), escalate=True,
            reason="proposers shared no check; the task is ambiguous",
        )

    description = "; ".join(
        dict.fromkeys(
            p.criterion.description for p in valid
            if p.criterion is not None and p.criterion.description
        )
    )[:400]
    return Consensus(
        Criterion(description or "swarm-authored criterion", tuple(agreed)),
        agreement,
        len(proposals),
        len(valid),
        escalate=False,
    )


# --- the frozen result -----------------------------------------------------


@dataclass(frozen=True, slots=True)
class FrozenCriterion:
    """The output of synthesis. Immutable for the rest of the run."""

    criterion: Criterion
    hash: str
    attempts: int
    agreement: float
    attack_report: AttackReport
    history: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.hash,
            "attempts": self.attempts,
            "agreement": round(self.agreement, 3),
            "attack": self.attack_report.summary(),
            "criterion": self.criterion.to_dict(),
            "history": list(self.history),
        }


class SynthesisFailed(RuntimeError):
    """Synthesis could not produce a criterion that survived attack.

    Deliberately fatal for the task. Proceeding with a criterion known to be
    weak would produce a confident, meaningless result -- which is worse than
    an honest failure, because it is indistinguishable from a real one.
    """

    def __init__(self, message: str, history: Sequence[str]) -> None:
        super().__init__(message)
        self.history = list(history)


@dataclass
class CriterionSynthesizer:
    """Runs propose -> cross-check -> attack -> freeze.

    ANATOMY: proposers
      How many agents independently author a criterion. Why 3: the minimum at
      which majority agreement is meaningful. At 2 a disagreement is a tie with
      no way to resolve it; above 5 the extra proposals mostly restate each
      other and spend scarce provider quota (docs/CAPACITY.md).

    ANATOMY: max_attempts
      Rounds of propose-and-attack before failing the task honestly. Why 3:
      enough to recover from one bad sample and one weak consensus; beyond that
      the failures are systematic rather than unlucky, and burning quota to
      re-roll a systematic problem is waste.
    """

    proposers: int = 3
    max_attempts: int = 3
    min_agreement: float = 0.5
    history: list[str] = field(default_factory=list)

    def _log(self, message: str) -> None:
        self.history.append(message)
        logger.info("criterion synthesis: %s", message)

    async def synthesize(
        self,
        task: str,
        propose: Any,
        *,
        on_escalate: Any | None = None,
    ) -> FrozenCriterion:
        """`propose(task, attempt, index) -> str` returns one raw proposal.

        The caller supplies the proposal function so this module never imports a
        provider -- which keeps it testable without a network and reusable with
        any harness.
        """
        for attempt in range(1, self.max_attempts + 1):
            raw = [await propose(task, attempt, i) for i in range(self.proposers)]
            proposals = [parse_proposal(r) for r in raw]
            invalid = [p.error for p in proposals if not p.ok]
            if invalid:
                self._log(f"attempt {attempt}: {len(invalid)} unparseable proposal(s)")

            consensus = merge(proposals, min_agreement=self.min_agreement)
            if consensus.escalate or consensus.merged is None:
                self._log(f"attempt {attempt}: {consensus.reason}")
                if on_escalate is not None:
                    await on_escalate(task, consensus)
                continue

            report = attack(consensus.merged, task)
            if not report.survived:
                # Do NOT silently patch the criterion here. Strengthening it
                # ourselves would mean the criterion was authored by this
                # module rather than by the swarm, and the next weak proposal
                # would be papered over the same way without anyone noticing.
                self._log(f"attempt {attempt}: {report.summary()}")
                continue

            self._log(
                f"attempt {attempt}: frozen with {len(consensus.merged.checks)} "
                f"checks, agreement {consensus.agreement:.2f}"
            )
            return FrozenCriterion(
                criterion=consensus.merged,
                hash=consensus.merged.content_hash(),
                attempts=attempt,
                agreement=consensus.agreement,
                attack_report=report,
                history=tuple(self.history),
            )

        raise SynthesisFailed(
            f"no criterion survived {self.max_attempts} attempts for task: "
            f"{task[:120]!r}. Proceeding would grade results against a "
            f"criterion known to be weak.",
            self.history,
        )
