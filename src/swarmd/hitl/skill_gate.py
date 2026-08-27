"""The gate on what the system is allowed to learn.

A skill entering the library is inherited by every future run, so it is the
most consequential human decision in the system and the least obvious one.
Rejecting an outreach draft affects one email; approving a bad skill affects
everything that comes after.

WHAT THIS FIXES. Distillation used to write straight to the library's pending
list — a JSON file. That meant three things were quietly untrue:

  1. The decision was not durable in the same place as every other human
     decision, so `swarmd list` did not show it and a restart lost the queue.
  2. There was no audit entry, so "who approved this skill and when" had no
     answer — and the provenance chain a poisoned skill has to be traced
     through was broken at its most important link.
  3. Two review queues existed. A reviewer had to know that skills lived
     somewhere else, which is how one of them stops being read.

Now a candidate skill goes through the SAME durable approval store, with the
same append-only audit trail, as everything else awaiting a human. The library
is where an APPROVED skill lands; it is no longer where the decision is made.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from swarmd.hitl.approvals import ApprovalManager, ApprovalRequest, ApprovalState
from swarmd.observability import metrics
from swarmd.swarm.skills import Skill, SkillLibrary, SkillLibraryError

logger = logging.getLogger(__name__)

STAGE = "skill"


@dataclass(frozen=True, slots=True)
class SkillDecision:
    request: ApprovalRequest
    skill: Skill | None
    applied: bool
    detail: str = ""


class SkillGate:
    """Couples the durable approval queue to the skill library.

    Deliberately a separate object rather than a method on either one. The
    library must not depend on the HITL layer (it is used in tests without a
    store), and the approval manager must not know what a skill is — it queues
    opaque items by design, which is what lets it hold outreach drafts and
    containment escalations too.
    """

    def __init__(self, approvals: ApprovalManager, library: SkillLibrary) -> None:
        self.approvals = approvals
        self.library = library

    async def submit(
        self,
        *,
        name: str,
        task_pattern: str,
        instruction: str,
        run_id: str = "",
        criterion_hash: str = "",
        evidence: int = 0,
    ) -> tuple[Skill, ApprovalRequest]:
        """Record a candidate and queue it for a human.

        The skill is written to the library as PENDING and the approval request
        is what actually gates it. Both exist because the library is the store
        and the queue is the decision — collapsing them is what produced the
        second, unread queue in the first place.
        """
        skill = self.library.propose(
            name=name,
            task_pattern=task_pattern,
            instruction=instruction,
            run_id=run_id,
            criterion_hash=criterion_hash,
        )
        if skill.usable:
            # Already approved in an earlier run; content-addressing means the
            # same instruction is the same skill. Re-queueing it would ask a
            # human to approve something they already approved.
            raise SkillAlreadyApproved(skill)

        request = await self.approvals.submit(
            {
                "kind": "skill",
                "skill_id": skill.skill_id,
                "name": skill.name,
                "task_pattern": skill.task_pattern,
                # The reviewer needs to see what they are approving, and a
                # reference they have to go look up is a reference nobody looks
                # up. Truncated because the queue is meant to be skimmable.
                "instruction": skill.instruction[:600],
                "provenance_run": run_id,
                "provenance_criterion": criterion_hash,
                "verified_successes": evidence,
            },
            stage=STAGE,
        )
        metrics.set_approvals_pending(len(await self.approvals.pending()))
        logger.info(
            "skill %s queued for review as %s", skill.skill_id, request.request_id
        )
        return skill, request

    async def decide(
        self, request_id: str, action: str, *, actor: str
    ) -> SkillDecision:
        """Record the decision, then apply it to the library.

        Order matters. The approval store is written FIRST so the audit entry
        exists even if applying to the library fails; a decision that happened
        but left no record is worse than one recorded and not yet applied,
        because the second is recoverable by re-running and the first is not.
        """
        request = await self.approvals.decide(request_id, action, actor=actor)
        skill_id = str(request.item.get("skill_id", ""))
        if not skill_id:
            return SkillDecision(request, None, False, "not a skill request")

        try:
            if request.state is ApprovalState.APPROVED:
                skill = self.library.approve(skill_id, actor=actor)
                detail = "approved into the library"
            elif request.state is ApprovalState.REJECTED:
                skill = self.library.reject(skill_id, actor=actor)
                detail = "retired; will not be re-proposed"
            else:
                return SkillDecision(request, None, False, f"state {request.state}")
        except SkillLibraryError as exc:
            # The decision stands; the library is out of sync. Reported rather
            # than swallowed, because a silent divergence between the audit
            # trail and the library is exactly what the trail exists to catch.
            logger.error("skill %s decided but not applied: %s", skill_id, exc)
            return SkillDecision(request, None, False, f"decided, not applied: {exc}")

        metrics.set_approvals_pending(len(await self.approvals.pending()))
        return SkillDecision(request, skill, True, detail)

    async def pending(self) -> list[ApprovalRequest]:
        """Skill requests only. Other stages have their own reviewers."""
        return [r for r in await self.approvals.pending() if r.stage == STAGE]

    async def summary(self) -> dict[str, Any]:
        queued = await self.pending()
        return {
            "awaiting_review": len(queued),
            "library": self.library.stats(),
            "oldest_waiting_s": (
                round(
                    max(
                        (0.0, *[_age(r) for r in queued]),
                    ),
                    1,
                )
                if queued
                else 0.0
            ),
        }


class SkillAlreadyApproved(RuntimeError):
    """The same instruction was approved in an earlier run.

    Content addressing means an identical skill is the identical skill, so
    re-queueing it would ask a reviewer to decide something already decided.
    """

    def __init__(self, skill: Skill) -> None:
        super().__init__(
            f"skill {skill.skill_id} was already approved by "
            f"{skill.approved_by or 'a reviewer'}"
        )
        self.skill = skill


def _age(request: ApprovalRequest) -> float:
    import time

    return time.time() - request.created_ts
