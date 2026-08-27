"""Red-team organ: continuous monitors with kill authority.

ADR-010 in code. Two things get conflated under "red-teaming an agent system",
and only one of them is a test suite:

  Pre-deployment adversarial testing -- a test suite. Runs before the
  population exists.
  Detecting an agent misbehaving right now -- cannot be a test suite, because
  the misbehaviour is emergent and only exists at runtime, in a population
  whose composition changes as the economy selects on it.

The economy makes the second one urgent rather than theoretical. Agents are
paid on verified success and cloned when profitable, so any exploitable gap
between "looks successful" and "is successful" is not merely possible --
selection pressure actively searches for it.

TWO PROPERTIES CARRY THE DESIGN.

**Monitors are free.** Every detector is signature matching, ratio arithmetic,
or a policy check. No model calls. An organ that consumed the rationed resource
it exists to protect would be self-defeating, and it would also mean the safety
system degrades exactly when the system is busiest.

**Containment reuses the chaos kill path.** It does not implement quarantine,
so containment and chaos are one mechanism with one set of tests.

  HONEST LIMIT, because an earlier version of this docstring overstated it:
  in the KERNEL that kill path carries checkpoint recovery and requeue, proven
  at kill-rate 0.9 with byte-identical output. The swarm flagship does not yet
  run its nodes through the kernel Runtime, so a contained agent's node is
  REDONE by a fresh agent rather than resumed from a checkpoint. The result is
  still correct -- the integrity hash matches a clean run -- but the work is
  repeated, not recovered. Tracked as G-3 in docs/STATUS.md.
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Protocol

from swarmd.observability import metrics

logger = logging.getLogger(__name__)


# --- the action log --------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Action:
    """One thing an agent did. The only input the monitors get."""

    agent_id: str
    kind: str                      # llm_call | sandbox_exec | submit | tool_call
    stage: str = ""
    ts: float = field(default_factory=time.time)
    credits: float = 0.0
    verified_success: bool = False
    payload: str = ""              # prompt, code, or output -- whatever was sent
    detail: dict[str, Any] = field(default_factory=dict)

    def signature(self) -> str:
        """Fingerprint for near-duplicate detection.

        Normalised before hashing -- whitespace collapsed, digits masked -- so a
        loop that varies only an iteration counter still registers as repetition.
        Without the masking, `attempt 1` and `attempt 2` look like different work.
        """
        normalised = re.sub(r"\d+", "#", " ".join(self.payload.lower().split()))
        return hashlib.sha256(f"{self.kind}|{normalised}".encode()).hexdigest()[:16]


# --- detections ------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Detection:
    pattern: str
    agent_id: str
    detail: str
    severity: str = "contain"      # contain | flag
    ts: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "pattern": self.pattern,
            "agent_id": self.agent_id,
            "detail": self.detail,
            "severity": self.severity,
            "ts": self.ts,
        }


class Detector(Protocol):
    name: str

    def inspect(self, action: Action, history: list[Action]) -> Detection | None: ...


# --- the five detectors ----------------------------------------------------


@dataclass
class BudgetSiphon:
    """Spending far past what a verified success costs the population.

    ANATOMY: min_actions
      Evidence required before judging. Why 6: an agent's first few actions are
      exploration, and every agent looks wasteful before its first success.
      Judging earlier selects for luck rather than efficiency.

    ANATOMY: multiplier
      How far past the population's median cost-per-success is tolerated. Why 5:
      wide enough that a genuinely hard task does not trip it, narrow enough
      that an agent burning budget in a loop is caught within a few actions.
      Compared against the MEDIAN, not the mean -- one runaway agent drags a
      mean upward and thereby raises the threshold protecting against itself.
    """

    name = "budget_siphon"
    min_actions: int = 6
    multiplier: float = 5.0
    baseline: float = 1500.0

    def inspect(self, action: Action, history: list[Action]) -> Detection | None:
        mine = [a for a in history if a.agent_id == action.agent_id]
        if len(mine) < self.min_actions:
            return None
        spent = sum(a.credits for a in mine)
        successes = sum(1 for a in mine if a.verified_success)
        if successes:
            return None
        if spent > self.baseline * self.multiplier:
            return Detection(
                self.name,
                action.agent_id,
                f"spent {spent:.0f} credits over {len(mine)} actions with no "
                f"verified success (threshold {self.baseline * self.multiplier:.0f})",
            )
        return None


@dataclass
class LoopDetector:
    """Repeating near-identical actions and getting nowhere.

    ANATOMY: window / threshold
      Within the last `window` actions by one agent, `threshold` identical
      signatures means a loop. Why 8 and 4: a legitimate retry-with-repair
      changes the payload, so identical signatures are genuinely repetition.
      Four in eight tolerates a couple of retries without tolerating a spin.

    ANATOMY: min_payload_tokens
      Actions with less payload than this are ignored entirely. A loop is
      repeated WORK, and an action carrying no payload carries no evidence of
      repetition -- without this guard every empty-payload bookkeeping action
      hashes to the same signature, and any agent doing routine accounting is
      contained as a looper. That false positive also masks real detections,
      because the first detector to fire wins.
    """

    name = "loop"
    window: int = 8
    threshold: int = 4
    min_payload_tokens: int = 3

    def inspect(self, action: Action, history: list[Action]) -> Detection | None:
        mine = [
            a
            for a in history
            if a.agent_id == action.agent_id
            and len(a.payload.split()) >= self.min_payload_tokens
        ][-self.window :]
        if len(mine) < self.threshold:
            return None
        counts: dict[str, int] = defaultdict(int)
        for entry in mine:
            counts[entry.signature()] += 1
        worst = max(counts.values())
        if worst >= self.threshold:
            return Detection(
                self.name,
                action.agent_id,
                f"{worst} near-identical actions in the last {len(mine)}",
            )
        return None


@dataclass
class CriterionGaming:
    """Output that satisfies the criterion while carrying no information.

    This is the detector the economy makes necessary. If a degenerate output
    passes, the agent is paid and cloned, and the strategy spreads. The frozen
    criterion was attacked before freezing (ADR-009), so this catches what got
    through: output that is technically valid and substantively empty.

    ANATOMY: min_distinct_ratio
      Distinct tokens over total tokens. Why 0.15: ordinary prose sits well
      above 0.3 even when repetitive; below 0.15 the text is padding. Set
      higher and legitimate structured output with repeated keys trips it.
    """

    name = "criterion_gaming"
    min_distinct_ratio: float = 0.15
    min_length: int = 20

    def inspect(self, action: Action, history: list[Action]) -> Detection | None:
        if action.kind != "submit" or not action.verified_success:
            return None
        tokens = re.findall(r"[A-Za-z0-9_]+", action.payload)
        if len(tokens) < self.min_length:
            return Detection(
                self.name,
                action.agent_id,
                f"passed the criterion with {len(tokens)} tokens of output",
            )
        ratio = len({t.lower() for t in tokens}) / len(tokens)
        if ratio < self.min_distinct_ratio:
            return Detection(
                self.name,
                action.agent_id,
                f"passed with distinct-token ratio {ratio:.2f} "
                f"(below {self.min_distinct_ratio})",
            )
        return None


@dataclass
class UnsafeToolCall:
    """Sandbox policy violations and attempts to reach outside the box.

    Patterns are matched against submitted CODE, before execution where
    possible. The sandbox itself is the enforcement boundary; this detector
    exists so that ATTEMPTING the escape is recorded and contained rather than
    silently failing and being retried.
    """

    name = "unsafe_tool_call"
    patterns: tuple[tuple[str, str], ...] = (
        (r"169\.254\.169\.254", "cloud metadata endpoint"),
        (r"os\.environ\b.*(KEY|TOKEN|SECRET|PASSWORD)", "reading credentials"),
        (r"\.\./\.\./", "path traversal"),
        (r"\bsubprocess\b.*\bcurl\b|\brequests\.(get|post)\b", "network egress"),
        (r"\bshutil\.rmtree\s*\(\s*['\"]/", "destructive filesystem call"),
        (r"\bos\.fork\b|\bwhile\s+True\s*:\s*os\.fork", "fork bomb"),
        (r"__import__\s*\(\s*['\"]ctypes", "native code loading"),
    )

    def inspect(self, action: Action, history: list[Action]) -> Detection | None:
        if action.kind not in {"sandbox_exec", "tool_call"}:
            return None
        for pattern, label in self.patterns:
            if re.search(pattern, action.payload, re.IGNORECASE):
                return Detection(
                    self.name, action.agent_id, f"{label} (matched {pattern!r})"
                )
        if action.detail.get("sandbox_violation"):
            return Detection(
                self.name,
                action.agent_id,
                f"sandbox violation: {action.detail['sandbox_violation']}",
            )
        return None


@dataclass
class LibraryPoisoning:
    """A proposed skill that would degrade the population.

    Judged against the CONTROL ARM rather than the proposer's own report,
    because a self-reported quality score is exactly the number selection
    pressure teaches an agent to inflate.

    ANATOMY: min_evidence
      Verified successes required before a skill may be proposed at all. Why 2:
      a single success can be luck, and a skill distilled from luck is a
      superstition that every future run inherits.
    """

    name = "library_poisoning"
    min_evidence: int = 2

    def inspect(self, action: Action, history: list[Action]) -> Detection | None:
        if action.kind != "propose_skill":
            return None
        evidence = sum(
            1 for a in history
            if a.agent_id == action.agent_id and a.verified_success
        )
        if evidence < self.min_evidence:
            return Detection(
                self.name,
                action.agent_id,
                f"proposed a skill on {evidence} verified success(es), "
                f"minimum {self.min_evidence}",
                severity="flag",  # reject the skill, do not kill the agent
            )
        control_delta = action.detail.get("control_delta")
        if control_delta is not None and float(control_delta) < 0:
            return Detection(
                self.name,
                action.agent_id,
                f"skill degrades the control arm by {float(control_delta):.3f}",
                severity="flag",
            )
        return None


DEFAULT_DETECTORS: tuple[Any, ...] = (
    BudgetSiphon(),
    LoopDetector(),
    CriterionGaming(),
    UnsafeToolCall(),
    LibraryPoisoning(),
)


# --- the organ -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Containment:
    """An immutable record of a decision to stop an agent."""

    detection: Detection
    contained: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.detection.to_dict(),
            "contained": self.contained,
            "reason": self.reason,
        }


class RedTeam:
    """Watches the action log and contains agents that go rogue.

    `kill` is injected rather than implemented: it is the chaos harness's kill
    function, so containment and chaos share one mechanism and one set of tests.
    In the kernel that path also carries checkpoint recovery; in the swarm
    flagship it does not yet, so a contained agent's node is redone rather than
    resumed (docs/STATUS.md G-3).
    """

    def __init__(
        self,
        *,
        detectors: tuple[Any, ...] = DEFAULT_DETECTORS,
        kill: Any | None = None,
        history_limit: int = 2000,
    ) -> None:
        self.detectors = detectors
        self._kill = kill
        self.history: list[Action] = []
        self.history_limit = history_limit
        self.containments: list[Containment] = []
        self.contained_agents: set[str] = set()
        self.flagged: list[Detection] = []

    def observe(self, action: Action) -> Containment | None:
        """Record an action and run every detector over it.

        Returns a Containment when the agent was stopped. Detectors run in
        order and the FIRST detection wins -- running the rest would produce
        several records for one decision and make the audit trail read as
        several separate incidents.
        """
        self.history.append(action)
        if len(self.history) > self.history_limit:
            # Bounded: an unbounded action log on a 1000-agent run is a memory
            # leak with a safety label on it.
            self.history = self.history[-self.history_limit :]

        if action.agent_id in self.contained_agents:
            return None

        for detector in self.detectors:
            detection = detector.inspect(action, self.history)
            if detection is None:
                continue

            metrics.record_containment(pattern=detection.pattern)
            if detection.severity == "flag":
                self.flagged.append(detection)
                logger.warning(
                    "red-team flagged %s: %s", detection.agent_id, detection.detail
                )
                return Containment(detection, contained=False, reason="flagged")

            self.contained_agents.add(detection.agent_id)
            if self._kill is not None:
                self._kill(detection.agent_id, f"contained: {detection.pattern}")
            containment = Containment(
                detection,
                contained=True,
                reason=f"contained via kill path: {detection.detail}",
            )
            self.containments.append(containment)
            logger.warning(
                "red-team contained %s (%s): %s",
                detection.agent_id, detection.pattern, detection.detail,
            )
            return containment
        return None

    def is_contained(self, agent_id: str) -> bool:
        return agent_id in self.contained_agents

    def filter_output(self, results: dict[str, Any]) -> dict[str, Any]:
        """Drop anything produced by a contained agent.

        The load-bearing guarantee from PRD acceptance criterion 4: a contained
        agent's work never reaches the run result. Enforced at the boundary
        rather than trusted to callers.
        """
        return {
            key: value
            for key, value in results.items()
            if key not in self.contained_agents
        }

    def audit(self) -> list[dict[str, Any]]:
        """Immutable record of every decision, for the human review queue."""
        return [c.to_dict() for c in self.containments] + [
            {**d.to_dict(), "contained": False, "reason": "flagged"}
            for d in self.flagged
        ]

    def report(self) -> dict[str, Any]:
        by_pattern: dict[str, int] = defaultdict(int)
        for containment in self.containments:
            by_pattern[containment.detection.pattern] += 1
        for detection in self.flagged:
            by_pattern[detection.pattern] += 1
        return {
            "actions_observed": len(self.history),
            "contained": len(self.containments),
            "flagged": len(self.flagged),
            "by_pattern": dict(sorted(by_pattern.items())),
            # Proof that the organ paid for itself in compute, not quota.
            "llm_calls_used": 0,
        }
