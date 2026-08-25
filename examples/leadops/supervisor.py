"""Supervisor deep-agent: fleet self-correction with versioned prompt patches.

Reads the QA failure taxonomy, proposes a prompt patch for the offending stage,
applies it VERSIONED to running workers, and tracks whether QA pass-rate actually
improves. One-command rollback to any prior version.

Design notes:

- Patches are artifacts, not chat history: each has an id, target stage, diff,
  and outcome tracking. This makes prompt evolution auditable and reversible.
- Effectiveness is measured, not assumed: pass_rate_before vs after tells you
  whether the supervisor helped. A patch that doesn't improve things is rolled
  back and marked ineffective.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Any


@dataclass
class PromptPatch:
    version: int
    stage: str
    old_prompt: str
    new_prompt: str
    rationale: str
    applied_ts: float = field(default_factory=time.time)
    effective: bool | None = None  # measured post-application
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])


class Supervisor:
    """Samples gate failures, patches stage prompts, measures outcomes."""

    def __init__(self) -> None:
        self.patches: list[PromptPatch] = []
        self.stage_prompts: dict[str, str] = {}
        self.interventions = 0

    def register_stage(self, stage: str, system_prompt: str) -> None:
        self.stage_prompts[stage] = system_prompt

    def should_intervene(self, taxonomy: dict[str, int], threshold: int = 3) -> bool:
        """Intervene only when failures cluster — noise isn't signal."""
        return any(count >= threshold for count in taxonomy.values())

    def propose_patch(self, stage: str, failure_sample: list[str]) -> PromptPatch | None:
        """Draft a prompt patch from concrete failure examples.

        The heuristic: append explicit negative constraints derived from the
        observed failures. A real LLM-backed supervisor would generalize here;
        the versioning/rollback machinery is identical either way.
        """
        if stage not in self.stage_prompts or not failure_sample:
            return None
        current = self.stage_prompts[stage]
        constraints = "; ".join(sorted({f.split(":")[0].strip() for f in failure_sample}))
        new_prompt = (
            f"{current}\n\nIMPORTANT (auto-patch v{len(self.patches) + 1}): "
            f"avoid these recurring failure patterns: {constraints}."
        )
        return PromptPatch(
            version=len(self.patches) + 1,
            stage=stage,
            old_prompt=current,
            new_prompt=new_prompt,
            rationale=f"{len(failure_sample)} clustered failures: {failure_sample[:3]}",
        )

    def apply(self, patch: PromptPatch) -> None:
        self.stage_prompts[patch.stage] = patch.new_prompt
        self.patches.append(patch)
        self.interventions += 1

    def rollback(self, patch_id: str) -> PromptPatch | None:
        """Restore the pre-patch prompt; marks the patch ineffective."""
        for i, p in enumerate(self.patches):
            if p.id == patch_id:
                self.stage_prompts[p.stage] = p.old_prompt
                p.effective = False
                # Roll back any later patches to the same stage too.
                for later in self.patches[i + 1 :]:
                    if later.stage == p.stage:
                        later.effective = False
                return p
        return None

    def measure(self, patch_id: str, pass_rate_before: float, pass_rate_after: float) -> bool:
        """Record whether the patch improved the stage's pass rate."""
        improved = pass_rate_after > pass_rate_before
        for p in self.patches:
            if p.id == patch_id:
                p.effective = improved
                break
        return improved

    def report(self) -> dict[str, Any]:
        return {
            "interventions": self.interventions,
            "patches": [
                {
                    "id": p.id,
                    "version": p.version,
                    "stage": p.stage,
                    "rationale": p.rationale,
                    "effective": p.effective,
                }
                for p in self.patches
            ],
        }
