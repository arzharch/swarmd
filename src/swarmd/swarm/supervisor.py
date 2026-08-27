"""The supervisor: fleet self-correction from observed failures.

PRD section 7 lists a supervisor under the flagship. One existed only in the
LeadOps example, where the stages were fixed and known in advance, so it could
be handed a prompt per stage at startup. The generalist swarm has no such list
-- its stages are named by a plan the system generated minutes ago -- and that
difference is why porting it needed more than an import.

WHAT IT ADDS OVER THE CONSOLIDATOR. The consolidator versions prompts and
refuses a change that hurts the control arm. It has no opinion about what the
change should be. The supervisor supplies that: it reads the failure taxonomy
the frozen criterion produced, and when failures CLUSTER on one check kind it
writes a constraint addressing that specific kind.

  The division is deliberate. Proposing and gating a change are different
  jobs, and letting the proposer also decide whether its own change was good
  is the self-assessment failure the whole criterion-first architecture exists
  to avoid. The supervisor proposes; the consolidator, which measures the
  control arm, decides.

WHY IT INTERVENES ON CLUSTERS AND NOT ON FAILURES. A single failure is a task
being hard. Three of the same kind across a window is the prompt failing to
say something the criterion demands. Patching on the first failure would encode
one task's idiosyncrasy into every future run -- the same reasoning that makes
distillation require two verified successes.

WHY PATCHES ARE MEASURED AND ROLLED BACK. A patch is a hypothesis. The
supervisor records the stage's pass rate before it, checks the rate after, and
reverts a patch that did not help. Without that, prompts accumulate constraints
forever, each one plausible, and nobody can say which of them are load-bearing.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# The stage every generalist worker shares. Plan node names change per run, so
# there is no stable per-stage prompt to patch -- what persists across runs is
# the worker system prompt, and that is what the supervisor amends.
WORKER_STAGE = "worker"


@dataclass
class Patch:
    """One hypothesis about why work is failing, and what to say about it."""

    patch_id: str
    stage: str
    kinds: tuple[str, ...]
    text: str
    rationale: str
    pass_rate_before: float
    pass_rate_after: float | None = None
    effective: bool | None = None
    applied_ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "patch_id": self.patch_id,
            "stage": self.stage,
            "kinds": list(self.kinds),
            "rationale": self.rationale,
            "pass_rate_before": round(self.pass_rate_before, 4),
            "pass_rate_after": (
                round(self.pass_rate_after, 4)
                if self.pass_rate_after is not None
                else None
            ),
            "effective": self.effective,
        }


# Constraints keyed by the criterion check kind that failed. Written as
# instructions to the worker, not as descriptions of the check: a worker told
# "json_parses failed" learns nothing it can act on.
GUIDANCE: dict[str, str] = {
    "json_parses": (
        "Emit ONLY the JSON object. No prose before it, no explanation after "
        "it, no markdown fence around it."
    ),
    "min_distinct_words": (
        "Say something substantive. Repeated phrasing and filler are rejected "
        "even when the structure is correct."
    ),
    "required_keys": (
        "Include every key the step asks for, even when a value is empty or "
        "unknown -- an absent key fails, an explicit null does not."
    ),
    "numeric_range": (
        "Check that every number you report falls inside the range the step "
        "specifies before you emit it."
    ),
    "regex_match": (
        "Match the exact format the step asks for, including case and "
        "separators."
    ),
    "file_exists": (
        "Write the artifact to the path the step names. Printing its contents "
        "is not the same as writing the file."
    ),
    "exit_code": (
        "The code must run to completion and exit zero. Handle the error cases "
        "rather than letting an exception terminate it."
    ),
    "stdout_contains": (
        "Print the marker the step asks for, exactly as written."
    ),
    "artifact_key": (
        "Write every requested key into artifacts.json, not only into stdout."
    ),
}


class Supervisor:
    """Watches failures across runs, patches the worker prompt, measures.

    ANATOMY: cluster_threshold
      Failures of one kind, within one window, before a patch is proposed.
      Why 3: two can be one task's quirk seen twice; three is a pattern. The
      same number the LeadOps supervisor used, kept because the reasoning --
      noise is not signal -- did not change with the domain.

    ANATOMY: window
      Runs the taxonomy accumulates over before it resets. Why 5: matched to
      the consolidation cadence, so a patch is proposed against the same slice
      of history the library is pruned against, and the two cannot disagree
      about what recently happened.
    """

    def __init__(
        self,
        *,
        cluster_threshold: int = 3,
        window: int = 5,
        base_prompt: str = "",
    ) -> None:
        self.cluster_threshold = cluster_threshold
        self.window = window
        self.base_prompt = base_prompt
        self.patches: list[Patch] = []
        self.interventions = 0
        self._taxonomy: dict[str, int] = defaultdict(int)
        self._samples: dict[str, list[str]] = defaultdict(list)
        self._runs_observed = 0
        self._passed = 0
        self._attempted = 0

    # -- observation --------------------------------------------------------

    def observe(self, results: list[Any]) -> None:
        """Take one run's worker results into the current window.

        Failures arrive as `"<kind>: <detail>"` because that is the shape the
        criterion produces; the kind is what clusters, the detail is what makes
        the rationale readable by a human reviewing the patch.
        """
        self._runs_observed += 1
        for outcome in results:
            self._attempted += 1
            if outcome.passed:
                self._passed += 1
            for failure in outcome.failures:
                kind = failure.split(":", 1)[0].strip()
                self._taxonomy[kind] += 1
                if len(self._samples[kind]) < 3:
                    self._samples[kind].append(failure)

    @property
    def pass_rate(self) -> float:
        return self._passed / self._attempted if self._attempted else 0.0

    def taxonomy(self) -> dict[str, int]:
        return dict(self._taxonomy)

    def should_intervene(self) -> bool:
        """Only when failures cluster. Noise is not signal."""
        return any(
            count >= self.cluster_threshold for count in self._taxonomy.values()
        )

    # -- proposal -----------------------------------------------------------

    def propose(self) -> Patch | None:
        """Draft a patch for the clustered failure kinds, or nothing.

        Returns None rather than an empty patch when no kind has usable
        guidance: a patch that appends "avoid failing" teaches nothing and
        still consumes a prompt-version slot and a measurement cycle.
        """
        clustered = sorted(
            (
                kind
                for kind, count in self._taxonomy.items()
                if count >= self.cluster_threshold
            ),
            key=lambda k: (-self._taxonomy[k], k),
        )
        instructions = [GUIDANCE[k] for k in clustered if k in GUIDANCE]
        if not instructions:
            return None

        version = len(self.patches) + 1
        body = "\n".join(f"- {line}" for line in instructions)
        text = (
            f"{self.base_prompt}\n\nRECURRING FAILURES (supervisor patch "
            f"v{version}), observed {self._runs_observed} run(s) ago:\n{body}"
        ).strip()

        samples = [s for k in clustered for s in self._samples.get(k, [])][:3]
        return Patch(
            patch_id=uuid.uuid4().hex[:8],
            stage=WORKER_STAGE,
            kinds=tuple(clustered),
            text=text,
            rationale=(
                f"{sum(self._taxonomy[k] for k in clustered)} clustered "
                f"failures across {', '.join(clustered)}: {samples}"
            ),
            pass_rate_before=self.pass_rate,
        )

    def record(self, patch: Patch) -> None:
        """Register an ACCEPTED patch and start a fresh window.

        Called only after the consolidator's control-arm check has kept it. A
        rejected patch leaves no record here on purpose: the consolidator
        already logged the revert, and counting it as an intervention would
        overstate what the supervisor achieved.
        """
        self.patches.append(patch)
        self.interventions += 1
        self.base_prompt = patch.text
        self._reset_window()

    def _reset_window(self) -> None:
        self._taxonomy = defaultdict(int)
        self._samples = defaultdict(list)
        self._runs_observed = 0
        self._passed = 0
        self._attempted = 0

    # -- measurement --------------------------------------------------------

    def measure(self, patch_id: str) -> bool | None:
        """Compare the pass rate since the patch against the rate before it.

        A patch is a hypothesis and this is the test. Returns None when the
        window holds no attempts yet -- an unmeasured patch must not be
        recorded as effective, which is the direction the error would go if
        an empty window counted as improvement.
        """
        for patch in self.patches:
            if patch.patch_id != patch_id:
                continue
            if not self._attempted:
                return None
            patch.pass_rate_after = self.pass_rate
            patch.effective = self.pass_rate > patch.pass_rate_before
            if not patch.effective:
                logger.warning(
                    "supervisor patch %s did not help: %.3f -> %.3f",
                    patch_id, patch.pass_rate_before, self.pass_rate,
                )
            return patch.effective
        return None

    def rollback(self, patch_id: str) -> Patch | None:
        """Undo a patch, restoring the prompt as it was before it.

        Later patches to the same stage are marked ineffective too: they were
        written against a prompt that no longer exists, so their measurements
        no longer describe anything.
        """
        for index, patch in enumerate(self.patches):
            if patch.patch_id != patch_id:
                continue
            previous = self.patches[index - 1].text if index else ""
            self.base_prompt = previous
            patch.effective = False
            for later in self.patches[index + 1 :]:
                later.effective = False
            return patch
        return None

    def report(self) -> dict[str, Any]:
        return {
            "interventions": self.interventions,
            "taxonomy": self.taxonomy(),
            "patches": [p.to_dict() for p in self.patches],
            "prompt_chars": len(self.base_prompt),
        }
