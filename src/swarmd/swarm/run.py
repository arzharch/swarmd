"""The run: an unknown task in, a verified result and a ledger out.

This is where every piece meets. The order is the argument:

    1. CRITERION   authored, attacked, frozen        (nothing else may start first)
    2. PLAN        proposed, validated, selected
    3. RETRIEVE    skills matched to the task
    4. EXECUTE     generic workers over the generated DAG, under chaos
    5. GATE        the frozen criterion decides
    6. DISTILL     verified successes become candidate skills, pending a human
    7. REPORT      everything traceable to a ledger row

Two things are deliberately NOT here. The run does not decide whether it
succeeded — the frozen criterion does. And the run does not approve its own
skills — a human does, because a skill is inherited by every future run.
"""

from __future__ import annotations

import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from swarmd.ledger import CostAccount, InMemoryLedger, JsonlLedger
from swarmd.observability import metrics
from swarmd.swarm.criteria import Candidate
from swarmd.swarm.economy import Economy
from swarmd.swarm.planner import (
    PLAN_SCHEMA_HINT,
    PLANNER_SYSTEM,
    Plan,
    PlanNode,
    PlanSynthesizer,
)
from swarmd.swarm.redteam import RedTeam
from swarmd.swarm.skills import SkillLibrary
from swarmd.swarm.synthesis import (
    PROPOSAL_SCHEMA_HINT,
    PROPOSER_SYSTEM,
    CriterionSynthesizer,
    FrozenCriterion,
    SynthesisFailed,
)
from swarmd.swarm.worker import GenericWorker, WorkerContext, WorkerResult

logger = logging.getLogger(__name__)


# Run profiles. Derived from docs/CAPACITY.md rather than chosen: the pooled
# free-tier ceiling is ~45 requests/minute, so a profile is really a statement
# about how many model calls fit in a target wall-clock.
@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    agents: int
    proposers: int
    max_repairs: int
    target_calls: int
    description: str


PROFILES = {
    "smoke": Profile("smoke", 8, 2, 1, 60, "CI: proves the loop runs, ~2 min"),
    "demo": Profile("demo", 500, 3, 2, 600, "the watchable run, 12-18 min"),
    "deep": Profile("deep", 500, 3, 3, 1800, "enough curve points, ~40 min"),
    "eval": Profile("eval", 500, 3, 2, 600, "one task within a sweep"),
}


@dataclass
class RunResult:
    run_id: str
    task: str
    status: str = "pending"          # completed | failed_criterion | aborted | error
    criterion: FrozenCriterion | None = None
    plan: Plan | None = None
    results: list[WorkerResult] = field(default_factory=list)
    proposed_skills: list[str] = field(default_factory=list)
    error: str = ""
    started_ts: float = field(default_factory=time.time)
    duration_s: float = 0.0

    @property
    def passed(self) -> list[WorkerResult]:
        return [r for r in self.results if r.passed]

    @property
    def contained(self) -> list[WorkerResult]:
        return [r for r in self.results if r.contained]

    def integrity_hash(self) -> str:
        """Order-independent hash of what the run actually produced.

        Order-independent because chaos changes the order work completes in and
        must not change the result. This is the number the chaos gate compares.
        Contained work is excluded, since it never reaches the output.
        """
        import hashlib

        payload = sorted(
            f"{r.node}|{r.candidate.output}|{sorted(r.candidate.artifacts.items())}"
            for r in self.results
            if r.passed and not r.contained
        )
        return hashlib.sha256("|".join(payload).encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "status": self.status,
            "duration_s": round(self.duration_s, 2),
            "criterion": self.criterion.to_dict() if self.criterion else None,
            "plan": self.plan.to_dict() if self.plan else None,
            "nodes_passed": len(self.passed),
            "nodes_total": len(self.results),
            "contained": len(self.contained),
            "integrity_hash": self.integrity_hash(),
            "proposed_skills": self.proposed_skills,
            "results": [r.to_dict() for r in self.results],
            "error": self.error,
        }


class SwarmRun:
    """Orchestrates one task end to end."""

    def __init__(
        self,
        provider: Any,
        *,
        profile: str = "demo",
        run_id: str | None = None,
        ledger_path: str | None = None,
        ceiling_usd: float = 0.05,
        skills: SkillLibrary | None = None,
        sandbox: Any = None,
        chaos: Any = None,
        use_skills: bool = True,
        on_event: Any = None,
    ) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; known: {sorted(PROFILES)}")
        self.profile = PROFILES[profile]
        self.provider = provider
        self.run_id = run_id or f"run-{uuid.uuid4().hex[:10]}"
        self.ledger = (
            JsonlLedger(self.run_id, ledger_path) if ledger_path
            else InMemoryLedger(self.run_id)
        )
        self.account = CostAccount(self.ledger, self.run_id, ceiling_usd=ceiling_usd)
        self.economy = Economy(account=self.account)
        # `use_skills` is the ablation switch. The control arm runs with it
        # False and everything else identical -- same tasks, same seeds, same
        # chaos schedule (ADR-007). An improvement claim without this is a
        # curve with nothing to compare against.
        self.skills = skills if use_skills else None
        self.use_skills = use_skills
        self.sandbox = sandbox
        self.chaos = chaos
        self.redteam = RedTeam(kill=self._contain)
        self.on_event = on_event

    # -- events -------------------------------------------------------------

    def _emit(self, kind: str, **payload: Any) -> None:
        """Publish to whatever is watching. Never blocks the run.

        The dashboard is an observer: if it is slow, absent, or throwing, the
        run continues. An observability path that can stall a run is a
        liability dressed as a feature.
        """
        if self.on_event is None:
            return
        try:
            self.on_event({"run_id": self.run_id, "kind": kind, **payload})
        except Exception:
            logger.debug("event sink raised; continuing", exc_info=True)

    def _contain(self, agent_id: str, reason: str) -> None:
        self.economy.kill(agent_id, reason=reason)
        self._emit("containment", agent_id=agent_id, reason=reason)

    # -- proposal callbacks -------------------------------------------------

    async def _ask(self, prompt: str, system: str, stage: str) -> str:
        from swarmd.router.providers import LLMRequest

        try:
            response = await self.provider.complete(
                LLMRequest(
                    prompt=prompt,
                    system=system,
                    temperature=0.4,
                    max_tokens=700,
                    metadata={"stage": stage, "agent_id": f"{stage}-agent"},
                )
            )
            return str(response.text)
        except Exception as exc:  # noqa: BLE001 - a provider failure is data
            logger.warning("%s proposal failed: %s", stage, exc)
            return ""

    async def _propose_criterion(self, task: str, attempt: int, index: int) -> str:
        # Temperature is not varied per proposer; independence comes from the
        # framing. Varying temperature would make one proposer systematically
        # sloppier, which is noise rather than an independent opinion.
        angles = [
            "What artifact must exist for this to be done?",
            "What would prove this was NOT done, however plausible the output?",
            "What number or exit status settles this objectively?",
        ]
        prompt = (
            f"TASK: {task}\n\n"
            f"ANGLE: {angles[index % len(angles)]}\n\n"
            f"Respond with ONLY a JSON object matching this schema:\n"
            f"{PROPOSAL_SCHEMA_HINT}"
        )
        self._emit("criterion_proposal", attempt=attempt, proposer=index)
        return await self._ask(prompt, PROPOSER_SYSTEM, "criterion")

    async def _propose_plan(self, task: str, attempt: int, index: int) -> str:
        prompt = (
            f"TASK: {task}\n\n"
            f"Decompose into a small dependency graph of steps.\n"
            f"Respond with ONLY a JSON object matching this schema:\n"
            f"{PLAN_SCHEMA_HINT}"
        )
        self._emit("plan_proposal", attempt=attempt, proposer=index)
        return await self._ask(prompt, PLANNER_SYSTEM, "plan")

    # -- the run ------------------------------------------------------------

    async def run(self, task: str) -> RunResult:
        started = time.monotonic()
        result = RunResult(run_id=self.run_id, task=task)
        self._emit("run_started", task=task, profile=self.profile.name)

        try:
            result.criterion = await self._synthesize_criterion(task)
            result.plan = await self._synthesize_plan(task)
            result.results = await self._execute(task, result.plan, result.criterion)
            result.proposed_skills = self._distill(task, result)
            result.status = "completed"
        except SynthesisFailed as exc:
            # An honest failure: no criterion survived attack, so there is
            # nothing to grade against and proceeding would produce a
            # confident, meaningless number.
            result.status = "failed_criterion"
            result.error = str(exc)
            self._emit("run_failed", reason="criterion", detail=str(exc))
        except Exception as exc:  # noqa: BLE001 - the run boundary
            from swarmd.ledger import CeilingExceeded

            if isinstance(exc, CeilingExceeded):
                metrics.record_ceiling_abort()
                result.status = "aborted"
                result.error = f"cost ceiling: {exc}"
            else:
                result.status = "error"
                result.error = f"{type(exc).__name__}: {exc}"
            self._emit("run_failed", reason=result.status, detail=result.error)

        result.duration_s = time.monotonic() - started
        self._emit("run_finished", **{
            k: v for k, v in result.to_dict().items() if k != "results"
        })
        return result

    async def _synthesize_criterion(self, task: str) -> FrozenCriterion:
        self._emit("stage_started", stage="criterion")
        frozen = await CriterionSynthesizer(
            proposers=self.profile.proposers
        ).synthesize(task, self._propose_criterion)
        self.account.record(
            "criterion_frozen",
            stage="criterion",
            detail={"hash": frozen.hash, "attempts": frozen.attempts},
        )
        self._emit("criterion_frozen", **frozen.to_dict())
        return frozen

    async def _synthesize_plan(self, task: str) -> Plan:
        self._emit("stage_started", stage="plan")
        selection = await PlanSynthesizer(
            proposers=self.profile.proposers
        ).synthesize(task, self._propose_plan)
        self.account.record(
            "plan_selected",
            stage="plan",
            detail={
                "hash": selection.plan.content_hash(),
                "nodes": len(selection.plan.nodes),
                "width": selection.plan.width,
            },
        )
        self._emit("plan_selected", **selection.plan.to_dict())
        return selection.plan

    async def _execute(
        self, task: str, plan: Plan, criterion: FrozenCriterion
    ) -> list[WorkerResult]:
        """Run the generated DAG with a pool of identical workers.

        Executed level by level using the plan's own dependency levels. The
        agent count is sized to the plan's WIDTH, not to the profile: spawning
        500 agents for a three-node plan would create 497 accounts that never
        act, which inflates the population figure without doing any work — the
        precise thing ADR-008 says not to do.
        """
        import asyncio

        context = WorkerContext(
            provider=self.provider,
            criterion=criterion.criterion,
            economy=self.economy,
            account=self.account,
            redteam=self.redteam,
            skills=self.skills,
            sandbox=self.sandbox,
            run_id=self.run_id,
            max_repairs=self.profile.max_repairs,
        )

        results: list[WorkerResult] = []
        for level in plan.levels():
            self._emit("level_started", nodes=level)
            metrics.set_queue_depth(stage="plan", depth=len(level))

            async def run_node(name: str) -> WorkerResult:
                node = plan.node(name)
                account = self.economy.spawn(traits={"node": name})
                metrics.set_agents_alive(
                    state="running", count=len(self.economy.alive())
                )
                self._emit("agent_spawned", agent_id=account.agent_id, node=name)

                if self.chaos is not None and self.chaos.should_kill():
                    self.economy.kill(account.agent_id, reason="chaos")
                    metrics.record_kill(source="chaos")
                    self._emit("agent_killed", agent_id=account.agent_id,
                               source="chaos")
                    # Requeue with a fresh agent: the work is not lost, which
                    # is the whole recovery guarantee.
                    metrics.record_requeue(stage=name)
                    account = self.economy.spawn(traits={"node": name})
                    self._emit("agent_requeued", agent_id=account.agent_id, node=name)

                worker = GenericWorker(account.agent_id, context)
                outcome = await worker.execute(task, node)
                metrics.record_gate(
                    stage=name, outcome="pass" if outcome.passed else "fail"
                )
                self._emit("node_finished", **outcome.to_dict())
                for thought in outcome.thoughts:
                    self._emit("thought", **thought)
                return outcome

            level_results = await asyncio.gather(
                *(run_node(name) for name in level), return_exceptions=True
            )
            for item in level_results:
                if isinstance(item, WorkerResult):
                    results.append(item)
                elif isinstance(item, BaseException):
                    # One node failing must not abort the level. The gate will
                    # record it as a failure, which is the honest outcome.
                    logger.warning("node raised: %s", item)
                    self._emit("node_error", detail=str(item)[:200])

            self.economy.reap()
            self.economy.reproduce()

        return results

    def _distill(self, task: str, result: RunResult) -> list[str]:
        """Turn verified successes into candidate skills, pending a human.

        Nothing here enters the library. `propose` records a candidate; a human
        approves it. That gate is the difference between a library that
        improves and one that compounds its own mistakes.
        """
        if self.skills is None or result.criterion is None:
            return []
        proposed = []
        for outcome in result.passed:
            if outcome.contained:
                continue
            skill = self.skills.propose(
                name=f"{outcome.node} approach",
                task_pattern=task,
                instruction=outcome.candidate.output[:600],
                run_id=self.run_id,
                criterion_hash=result.criterion.hash,
            )
            proposed.append(skill.skill_id)
            self._emit("skill_proposed", skill_id=skill.skill_id, name=skill.name)
        return proposed

    # -- reporting ----------------------------------------------------------

    def report(self, result: RunResult) -> dict[str, Any]:
        """Everything about this run, sourced from the ledger."""
        return {
            "run": result.to_dict(),
            "cost": self.account.report(),
            "economy": self.economy.report(),
            "leaderboard": self.economy.leaderboard(),
            "redteam": self.redteam.report(),
            "redteam_audit": self.redteam.audit(),
            "skills": self.skills.stats() if self.skills else None,
            "profile": {
                "name": self.profile.name,
                "agents": self.profile.agents,
                "target_calls": self.profile.target_calls,
            },
            "ablation": {"skills_enabled": self.use_skills},
        }


def make_node(name: str, instruction: str, deps: tuple[str, ...] = ()) -> PlanNode:
    """Convenience for tests and fixed pipelines."""
    return PlanNode(name=name, instruction=instruction, depends_on=deps)


def empty_candidate() -> Candidate:
    return Candidate()
