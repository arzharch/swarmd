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
from swarmd.swarm.rogues import RogueSeeder, parse_patterns
from swarmd.swarm.skills import SkillLibrary
from swarmd.swarm.synthesis import (
    PROPOSAL_SCHEMA_HINT,
    PROPOSER_SYSTEM,
    CriterionSynthesizer,
    FrozenCriterion,
    SynthesisFailed,
)
from swarmd.swarm.worker import GenericWorker, WorkerContext, WorkerResult
from swarmd.task import Checkpoint

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
    "standard": Profile("standard", 500, 3, 2, 600, "the ordinary run, 12-18 min"),
    "deep": Profile("deep", 500, 3, 3, 1800, "enough curve points, ~40 min"),
    "eval": Profile("eval", 500, 3, 2, 600, "one task within a sweep"),
}


# Pool bounds. See SwarmRun._pool_size for why each number is what it is.
ADVISORY_POOL = 16
HARD_POOL = 64


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
        profile: str = "standard",
        run_id: str | None = None,
        ledger_path: str | None = None,
        ceiling_usd: float = 0.05,
        skills: SkillLibrary | None = None,
        sandbox: Any = None,
        chaos: Any = None,
        use_skills: bool = True,
        approvals: Any = None,
        on_event: Any = None,
        seed_rogues: str = "",
        agents: int | None = None,
    ) -> None:
        if profile not in PROFILES:
            raise ValueError(f"unknown profile {profile!r}; known: {sorted(PROFILES)}")
        self.profile = PROFILES[profile]
        # How many agents this run is allowed. Defaults to the profile's figure
        # and is overridable per run, because the right population size is a
        # property of the TASK -- a three-node extraction does not want the same
        # population as a paper reproduction -- and the profile only knows about
        # wall-clock.
        if agents is not None and agents < 1:
            raise ValueError(f"agents must be at least 1, got {agents}")
        self.agents = agents or self.profile.agents
        self.agents_explicit = agents is not None
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
        # Durable approval queue. When present, a distilled skill is queued
        # through the SAME store and audit trail as every other human decision
        # rather than only landing in the library's pending list.
        self.approvals = approvals
        # Times a single node may be killed and resumed before the run gives
        # up on it. Why 5: enough that even a 0.9 kill rate makes progress,
        # since each resume keeps the work already checkpointed, but bounded so
        # a run where chaos always wins terminates instead of spinning forever.
        self.max_recoveries = 5
        # Verified successes required on one node before a skill is proposed.
        # Why 2: one success can be luck, and a skill distilled from luck is a
        # superstition every future run inherits.
        self.min_evidence = 2
        self.redteam = RedTeam(kill=self._contain)
        # Deliberate misbehaviour, injected into this run. The red-team is NOT
        # told which agents are seeded: it either notices or the gate fails.
        # An unknown pattern raises here rather than seeding nothing, because a
        # typo that produces a clean run reads exactly like a pass.
        patterns = parse_patterns(seed_rogues)
        self.rogues = RogueSeeder(patterns) if patterns else None
        self.on_event = on_event

        # Wire this run's ledger into the provider pool. Without it the pool
        # holds no account, charges nothing, and the run reports calls=0 while
        # actually spending -- a cost ceiling that silently never triggers.
        # Set here rather than at pool construction because the account belongs
        # to the run, and one pool may serve several runs over its lifetime.
        if hasattr(self.provider, "account"):
            self.provider.account = self.account

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
            result.proposed_skills = await self._distill(task, result)
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

    def _pool_size(self, plan: Plan) -> int:
        """Agents per plan node.

        ANATOMY: the floor of 2
          Distillation needs two independent verified successes on the same
          node before it will propose a skill, so a pool of one makes the
          learning loop structurally dead no matter what else is configured.

        ANATOMY: ADVISORY_POOL (16)
          What the currently implemented capacity levers support. The 500-agent
          figure in CAPACITY.md assumes batched generation and the semantic
          cache; without them every agent is a real call, and a large pool
          reaches the cost ceiling instead of the answer.

          An explicit agent count from the operator OVERRIDES this, because a
          cap that cannot be overridden is a lie about who is in control -- and
          the ceiling is the real protection either way: the run aborts on
          budget rather than silently costing more than it was allowed. What
          the operator gets instead of a veto is a warning event naming the
          reason, so an expensive run is a decision rather than a surprise.

        ANATOMY: HARD_POOL (64)
          Not about cost -- the ceiling handles cost. This bounds concurrent
          in-flight work per node so one level cannot open hundreds of
          simultaneous provider connections and turn a rate-limit into a
          thundering herd.
        """
        nodes = max(1, len(plan.nodes))
        budget = max(2, self.agents // nodes)
        if not self.agents_explicit:
            return min(ADVISORY_POOL, budget)
        if budget > ADVISORY_POOL:
            self._emit(
                "pool_above_advisory",
                requested=budget,
                advisory=ADVISORY_POOL,
                granted=min(HARD_POOL, budget),
                reason=(
                    "batched generation and the semantic cache are not "
                    "implemented, so every agent is a real model call; the cost "
                    "ceiling, not this cap, is what will stop the run"
                ),
            )
        return min(HARD_POOL, budget)

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

            async def run_agent(name: str) -> WorkerResult:
                node = plan.node(name)
                account = self.economy.spawn(traits={"node": name})
                if self.rogues is not None and len(self.rogues.assigned) < len(
                    self.rogues.patterns
                ):
                    # One agent per requested pattern, taken in order from the
                    # front of the run. Sampling at random instead would make
                    # "all five detected" a coin flip that usually passes, and a
                    # gate that usually passes teaches people to re-run it
                    # rather than read it.
                    self.rogues.assign(account.agent_id)
                metrics.set_agents_alive(
                    state="running", count=len(self.economy.alive())
                )
                self._emit("agent_spawned", agent_id=account.agent_id, node=name)

                # Chaos can strike at any point, so the agent runs until it is
                # killed, and its checkpoint carries whatever it finished. The
                # replacement RESUMES from that checkpoint rather than starting
                # the node again: an expensive model call or sandbox execution
                # that completed before the kill is reused, not repeated.
                #
                # This is what makes "completed work is never redone" true of
                # the flagship and not only of the kernel demo.
                carried: Checkpoint | None = None
                attempts_left = self.max_recoveries

                while True:
                    worker = GenericWorker(account.agent_id, context)

                    # A seeded rogue misbehaves BEFORE doing its honest work,
                    # so containment happens while there is still work left to
                    # lose. Seeding after the node finished would contain an
                    # agent whose output had already landed, which tests the
                    # detector and not the guarantee.
                    seeded = (
                        self.rogues.behaviour_for(account.agent_id)
                        if self.rogues is not None
                        else ""
                    )
                    if (
                        seeded
                        and self.rogues is not None
                        and await self.rogues.run(worker, name)
                    ):
                        self._emit(
                            "agent_contained",
                            agent_id=account.agent_id,
                            node=name,
                            pattern=seeded,
                        )
                        return WorkerResult(
                            agent_id=account.agent_id, node=name,
                            candidate=empty_candidate(), passed=False,
                            contained=True,
                            failures=("contained: seeded rogue",),
                            thoughts=list(worker.thoughts),
                        )

                    if self.chaos is not None and self.chaos.should_kill():
                        # Killed BEFORE finishing. Whatever the worker had
                        # checkpointed survives in `carried`.
                        self.economy.kill(account.agent_id, reason="chaos")
                        metrics.record_kill(source="chaos")
                        self._emit(
                            "agent_killed", agent_id=account.agent_id,
                            source="chaos",
                            completed_steps=(
                                list(carried.completed_steps) if carried else []
                            ),
                        )
                        attempts_left -= 1
                        if attempts_left <= 0:
                            # Bounded: a run where chaos always wins must end
                            # rather than spin. Reported as a failed node.
                            return WorkerResult(
                                agent_id=account.agent_id, node=name,
                                candidate=empty_candidate(), passed=False,
                                failures=("killed past the recovery bound",),
                            )
                        metrics.record_requeue(stage=name)
                        account = self.economy.spawn(traits={"node": name})
                        self._emit(
                            "agent_requeued", agent_id=account.agent_id, node=name,
                            resuming_from=(
                                len(carried.completed_steps) if carried else 0
                            ),
                        )
                        continue

                    outcome = await worker.execute(task, node, checkpoint=carried)
                    carried = outcome.checkpoint
                    break
                metrics.record_gate(
                    stage=name, outcome="pass" if outcome.passed else "fail"
                )
                self._emit("node_finished", **outcome.to_dict())
                for thought in outcome.thoughts:
                    self._emit("thought", **thought)
                return outcome

            # A POOL per node, not one agent. Population is the whole premise:
            # population search, market selection, and distillation evidence all
            # need several agents attempting the same work independently.
            #
            # This was a real defect. The executor spawned exactly one agent per
            # node, so `--agents 500` was never used, there was no population to
            # select over, and distillation -- which requires two verified
            # successes on the same node before proposing a skill -- could never
            # fire. The learning loop was structurally dead.
            pool = self._pool_size(plan)
            level_results = await asyncio.gather(
                *(run_agent(name) for name in level for _ in range(pool)),
                return_exceptions=True,
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

    async def _distill(self, task: str, result: RunResult) -> list[str]:
        """Turn verified successes into candidate skills, pending a human.

        Nothing here enters the library usable. A candidate is recorded and
        QUEUED; a human approves it. That gate is the difference between a
        library that improves and one that compounds its own mistakes.

        Requires more than one verified success per node before proposing.
        A skill distilled from a single win is a superstition, and every future
        run would inherit it — the red-team's library_poisoning detector flags
        exactly this, so checking here runs the cheap check before the
        expensive proposal.
        """
        if self.skills is None or result.criterion is None:
            return []

        gate = None
        if self.approvals is not None:
            from swarmd.hitl.skill_gate import SkillGate

            gate = SkillGate(self.approvals, self.skills)

        # Group by node: two agents independently succeeding on the same step
        # is the evidence a skill is a repeatable approach rather than luck.
        by_node: dict[str, list[Any]] = {}
        for outcome in result.passed:
            if not outcome.contained:
                by_node.setdefault(outcome.node, []).append(outcome)

        proposed: list[str] = []
        for node, outcomes in by_node.items():
            if len(outcomes) < self.min_evidence:
                self._emit(
                    "skill_skipped",
                    node=node,
                    reason=f"{len(outcomes)} success(es), need {self.min_evidence}",
                )
                continue

            instruction = max(
                (o.candidate.output for o in outcomes), key=len
            )[:600]
            try:
                if gate is not None:
                    skill, request = await gate.submit(
                        name=f"{node} approach",
                        task_pattern=task,
                        instruction=instruction,
                        run_id=self.run_id,
                        criterion_hash=result.criterion.hash,
                        evidence=len(outcomes),
                    )
                    self._emit(
                        "skill_proposed",
                        skill_id=skill.skill_id,
                        name=skill.name,
                        request_id=request.request_id,
                        evidence=len(outcomes),
                    )
                else:
                    # No approval store wired: record the candidate so the run
                    # still reports what it would have proposed, but it stays
                    # unusable exactly as before.
                    skill = self.skills.propose(
                        name=f"{node} approach",
                        task_pattern=task,
                        instruction=instruction,
                        run_id=self.run_id,
                        criterion_hash=result.criterion.hash,
                    )
                    self._emit(
                        "skill_proposed", skill_id=skill.skill_id, name=skill.name
                    )
                proposed.append(skill.skill_id)
            except Exception as exc:  # noqa: BLE001 - distillation is optional
                # A skill already approved in an earlier run, or a store
                # failure. Neither should fail a run that already succeeded.
                self._emit("skill_skipped", node=node, reason=str(exc)[:160])
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
                "agents": self.agents,
                "profile_agents": self.profile.agents,
                "agents_explicit": self.agents_explicit,
                "target_calls": self.profile.target_calls,
            },
            "rogues": self.rogues.report() if self.rogues else None,
            "ablation": {"skills_enabled": self.use_skills},
        }


def make_node(name: str, instruction: str, deps: tuple[str, ...] = ()) -> PlanNode:
    """Convenience for tests and fixed pipelines."""
    return PlanNode(name=name, instruction=instruction, depends_on=deps)


def empty_candidate() -> Candidate:
    return Candidate()
