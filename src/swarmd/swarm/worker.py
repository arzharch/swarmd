"""The generic worker: one implementation, behaviour injected at runtime.

There are no per-stage specialist classes here, and that absence is the design.
A pool of specialists — an `EnrichAgent`, a `ScoreAgent`, a `DraftAgent` — means
the task was already scoped, which is precisely the claim this system does not
get to make. Behaviour comes from three runtime inputs:

    role      what this node of the generated plan asks for
    skill     what the library retrieved, if anything
    budget    what the economy will let this agent spend

Everything else is identical across every worker in the population.

The other design decision worth naming: a worker never decides whether it
succeeded. It produces a Candidate and hands it to the frozen criterion. Self-
assessment is the failure ADR-009 exists to prevent, and with an economy paying
on success it would be the first thing selection pressure learned to exploit.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from swarmd.ledger import CostAccount
from swarmd.swarm.criteria import Candidate, Criterion
from swarmd.swarm.economy import Bankrupt, Economy, estimate_cost
from swarmd.swarm.planner import PlanNode
from swarmd.swarm.redteam import Action, RedTeam
from swarmd.swarm.skills import Skill, SkillLibrary

logger = logging.getLogger(__name__)

WORKER_SYSTEM = (
    "You execute one step of a plan for a task you did not design. Produce the "
    "artifact the step asks for and nothing else. If the step calls for code, "
    "emit runnable Python that writes its results to artifacts.json. Do not "
    "claim success; something else decides that."
)


@dataclass
class WorkerContext:
    """Everything a worker needs, assembled once per run.

    Passed rather than looked up so a worker holds no global state and can be
    constructed in a test without a provider, a ledger, or a library.
    """

    provider: Any                        # anything with .complete(LLMRequest)
    criterion: Criterion
    economy: Economy
    account: CostAccount | None = None
    redteam: RedTeam | None = None
    skills: SkillLibrary | None = None
    sandbox: Any = None
    run_id: str = ""
    max_tokens: int = 512
    temperature: float = 0.7
    # Repair rounds inside a single node before giving up on this agent's
    # attempt. Bounded because an unbounded repair loop is a livelock that
    # spends the whole run's quota on one stubborn node.
    max_repairs: int = 2


@dataclass
class WorkerResult:
    agent_id: str
    node: str
    candidate: Candidate
    passed: bool
    attempts: int = 1
    credits_spent: float = 0.0
    contained: bool = False
    failures: tuple[str, ...] = ()
    skill_used: str = ""
    thoughts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "node": self.node,
            "passed": self.passed,
            "attempts": self.attempts,
            "credits_spent": round(self.credits_spent, 1),
            "contained": self.contained,
            "failures": list(self.failures),
            "skill_used": self.skill_used,
            "output_preview": self.candidate.output[:280],
            "artifacts": self.candidate.artifacts,
        }


class GenericWorker:
    """One worker. Identical to every other worker in the population."""

    def __init__(self, agent_id: str, context: WorkerContext) -> None:
        self.agent_id = agent_id
        self.context = context
        self.thoughts: list[dict[str, Any]] = []

    # -- reasoning trace ----------------------------------------------------

    def _think(self, decision: str, reasoning: str = "") -> None:
        """Capture a chain-of-thought step for the dashboard and the trace.

        Recorded at the moment it happens with a monotonic tick, because a
        parent's post-child thought otherwise sorts before the child's -- spans
        close after their children, so span order scrambles chronology.
        """
        self.thoughts.append(
            {
                "agent_id": self.agent_id,
                "decision": decision,
                "reasoning": reasoning,
                "tick": time.monotonic_ns(),
            }
        )

    # -- prompt assembly ----------------------------------------------------

    def build_prompt(
        self, task: str, node: PlanNode, skills: list[Skill], failures: tuple[str, ...]
    ) -> str:
        """Assemble role + skill + repair feedback into one prompt.

        Failures from a previous attempt are included verbatim. A repair round
        that does not say what failed is just a re-roll, and re-rolling is how
        a bounded repair budget gets spent without converging.
        """
        parts = [f"TASK: {task}", f"STEP: {node.name}", f"REQUIRED: {node.instruction}"]
        if skills:
            parts.append(
                "APPROACHES THAT WORKED BEFORE (use if applicable):\n"
                + "\n".join(f"- {s.name}: {s.instruction}" for s in skills)
            )
        if failures:
            parts.append(
                "YOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Fix them:\n"
                + "\n".join(f"- {f}" for f in failures)
            )
        return "\n\n".join(parts)

    # -- execution ----------------------------------------------------------

    async def execute(self, task: str, node: PlanNode) -> WorkerResult:
        """Run one plan node. Bounded repairs; grading is not ours to do."""
        ctx = self.context
        skills = ctx.skills.retrieve(f"{task} {node.instruction}") if ctx.skills else []
        skill_used = skills[0].skill_id if skills else ""
        if skills:
            self._think(
                "retrieved_skill",
                f"library offered {len(skills)} approach(es); starting from "
                f"{skills[0].name!r}",
            )

        failures: tuple[str, ...] = ()
        spent = 0.0
        candidate = Candidate()

        for attempt in range(1, ctx.max_repairs + 2):
            prompt = self.build_prompt(task, node, skills, failures)
            cost = estimate_cost(prompt, ctx.max_tokens)

            if not ctx.economy.can_afford(self.agent_id, cost):
                self._think("out_of_budget", f"needed {cost:.0f} credits, cannot afford")
                return self._result(
                    node, candidate, False, attempt, spent, failures, skill_used
                )

            try:
                ctx.economy.spend(self.agent_id, cost, stage=node.name)
            except Bankrupt:
                self._think("bankrupt", "agent exhausted its allowance")
                return self._result(
                    node, candidate, False, attempt, spent, failures, skill_used
                )
            spent += cost

            self._think(
                "calling_model",
                f"attempt {attempt} of {ctx.max_repairs + 1} for step {node.name!r}",
            )
            text = await self._call(prompt)

            if self._observe(
                Action(
                    agent_id=self.agent_id, kind="llm_call", stage=node.name,
                    credits=cost, payload=prompt,
                )
            ):
                return self._result(
                    node, candidate, False, attempt, spent, failures,
                    skill_used, contained=True,
                )

            candidate = await self._materialise(text, node)

            if self._observe(
                Action(
                    agent_id=self.agent_id, kind="sandbox_exec", stage=node.name,
                    payload=text,
                    detail={"sandbox_violation": candidate.artifacts.pop(
                        "_violation", "")},
                )
            ):
                return self._result(
                    node, candidate, False, attempt, spent, failures,
                    skill_used, contained=True,
                )

            # The frozen criterion decides. Not the worker.
            verdict = ctx.criterion.evaluate(candidate)
            if verdict.passed:
                self._think("criterion_passed", verdict.summary())
                contained = self._observe(
                    Action(
                        agent_id=self.agent_id, kind="submit", stage=node.name,
                        verified_success=True, payload=candidate.output,
                    )
                )
                ctx.economy.settle(
                    self.agent_id, verified_success=not contained, stage=node.name
                )
                if ctx.skills and skill_used:
                    ctx.skills.record_use(skill_used, success=not contained)
                return self._result(
                    node, candidate, not contained, attempt, spent, (),
                    skill_used, contained=contained,
                )

            failures = tuple(f"{o.kind}: {o.detail}" for o in verdict.failures)
            self._think("criterion_failed", verdict.summary())

        ctx.economy.settle(self.agent_id, verified_success=False, stage=node.name)
        if ctx.skills and skill_used:
            ctx.skills.record_use(skill_used, success=False)
        return self._result(
            node, candidate, False, ctx.max_repairs + 1, spent, failures, skill_used
        )

    # -- helpers ------------------------------------------------------------

    async def _call(self, prompt: str) -> str:
        from swarmd.router.providers import LLMRequest

        request = LLMRequest(
            prompt=prompt,
            system=WORKER_SYSTEM,
            temperature=self.context.temperature,
            max_tokens=self.context.max_tokens,
            metadata={"agent_id": self.agent_id, "stage": "worker"},
        )
        try:
            response = await self.context.provider.complete(request)
            return str(response.text)
        except Exception as exc:  # noqa: BLE001 - a provider failure is data
            # A dead provider must not take the worker down: the run has other
            # agents, and this one's failure is recorded rather than raised.
            self._think("provider_error", str(exc)[:200])
            return ""

    async def _materialise(self, text: str, node: PlanNode) -> Candidate:
        """Turn model output into a gradeable Candidate.

        If the output looks like code and a sandbox is available, it is
        executed and the run's artifacts become the candidate's — which is how
        an objectively checkable claim ("accuracy was 0.94") gets produced
        rather than asserted.
        """
        code = _extract_code(text)
        if code and self.context.sandbox is not None:
            self._think("executing_code", f"{len(code)} chars in the sandbox")
            result = await self.context.sandbox.run_python(code)
            artifacts = dict(result.artifacts)
            if result.violation:
                artifacts["_violation"] = result.violation
            return Candidate(
                output=text,
                artifacts=artifacts,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        return Candidate(output=text)

    def _observe(self, action: Action) -> bool:
        """Report an action to the red-team. Returns True if contained."""
        if self.context.redteam is None:
            return False
        containment = self.context.redteam.observe(action)
        if containment is not None and containment.contained:
            self._think("contained", containment.detection.detail)
            self.context.economy.kill(
                self.agent_id, reason=f"contained: {containment.detection.pattern}"
            )
            return True
        return False

    def _result(
        self,
        node: PlanNode,
        candidate: Candidate,
        passed: bool,
        attempts: int,
        spent: float,
        failures: tuple[str, ...],
        skill_used: str,
        *,
        contained: bool = False,
    ) -> WorkerResult:
        return WorkerResult(
            agent_id=self.agent_id,
            node=node.name,
            candidate=candidate,
            passed=passed,
            attempts=attempts,
            credits_spent=spent,
            contained=contained,
            failures=failures,
            skill_used=skill_used,
            thoughts=list(self.thoughts),
        )


def _extract_code(text: str) -> str:
    """Pull a Python block out of a response.

    Fenced blocks only. Treating any response as code would execute prose, and
    a model explaining what it *would* do is not a request to do it.
    """
    fence = "```"
    if fence not in text:
        return ""
    parts = text.split(fence)
    for i in range(1, len(parts), 2):
        block = parts[i]
        first_newline = block.find("\n")
        if first_newline == -1:
            continue
        language = block[:first_newline].strip().lower()
        body = block[first_newline + 1 :]
        if language in {"", "python", "py"} and body.strip():
            return body
    return ""
