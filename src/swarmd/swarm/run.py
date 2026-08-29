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
from swarmd.swarm.batch import Batch, generate_batch
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
from swarmd.swarm.runstore import RunState, RunStore
from swarmd.swarm.skills import SkillLibrary
from swarmd.swarm.synthesis import (
    PROPOSAL_SCHEMA_HINT,
    PROPOSER_SYSTEM,
    CriterionSynthesizer,
    FrozenCriterion,
    SynthesisFailed,
)
from swarmd.swarm.worker import (
    WORKER_SYSTEM,
    GenericWorker,
    WorkerContext,
    WorkerResult,
)
from swarmd.task import Checkpoint

logger = logging.getLogger(__name__)


# Run profiles, sized against the budget this deployment MEASURED rather than
# the one its capacity plan hoped for.
#
# The old table promised 500 agents on `standard`, targeting 600 calls. The
# measured plannable budget is ~1,146 requests/day (docs/CAPACITY.md section 7,
# after discovering that Groq's binding limit is 100,000 tokens/day, not 1,000
# requests). One `standard` run was therefore HALF A DAY of total capacity, and
# the number 500 appeared nowhere except in a table nobody could act on.
#
# Sized so the ordinary profile can be run repeatedly in a working day, which
# is what makes a profile useful. `--agents` overrides any of it: an operator
# who wants 500, or 1000, gets 500 or 1000, and gets told what it will cost
# before it starts (see `SwarmRun.preflight`).
@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    agents: int
    proposers: int
    max_repairs: int
    target_calls: int
    description: str


PROFILES = {
    # THREE proposers everywhere, and the reason is arithmetic rather than
    # thoroughness. The merge keeps a check when `ceil(valid * 0.5)` proposers
    # asked for it: at three that is 2 of 3, a majority; at TWO it is 1 of 2,
    # which is a union. Every check either proposer thought of survived, so
    # smoke runs were graded against the strictest criterion the system can
    # produce -- 13 checks where standard produced 5 -- and the profile meant
    # to be the easy one was the hardest.
    #
    # max_repairs is 2, not 1. The repair round is the mechanism that fixes the
    # single most common live failure -- a model writing "achieving 94.3%
    # accuracy" where a numeric_range check wants 94.3 -- and giving it one
    # attempt meant the mechanism barely ran. Repairs are the cheapest quality
    # lever available: one extra call against a candidate that already exists.
    # 15, not 8: plans typically come back with three nodes, and 8 over three
    # nodes is two agents a node -- below MIN_POOL, so the profile's headline
    # count described a population the run never actually kept in flight.
    "smoke": Profile(
        "smoke", 15, 3, 2, 30,
        "CI and quick checks: ~30 calls, under a minute",
    ),
    # The everyday run. ~90 calls means roughly a dozen a day inside the
    # measured budget, with room left for an eval sweep.
    "standard": Profile(
        "standard", 24, 3, 2, 90,
        "the ordinary run: ~90 calls, 2-4 min, a dozen a day fit the budget",
    ),
    # For a task worth spending on. Roughly a quarter of a day's capacity, so
    # it is a decision rather than a habit.
    "deep": Profile(
        "deep", 64, 3, 3, 280,
        "wide population and more repairs: ~280 calls, a quarter of a day",
    ),
    # One task inside a sweep. Deliberately the smallest: an eval multiplies
    # this by tasks x arms x repeats, so a profile that is merely "small" here
    # becomes a day's budget there.
    "eval": Profile(
        "eval", 15, 3, 2, 30,
        "one task within a sweep: kept small because the sweep multiplies it",
    ),
}


# ADVISORY_POOL caps the pool a PROFILE implies, so a profile cannot silently
# become enormous. An explicit `--agents` is honoured in full instead.
ADVISORY_POOL = 32

# MIN_POOL is the operating floor: how many agents a node keeps in flight at
# once unless the operator explicitly asks for fewer.
#
# Five, not two, and the reason is operational rather than statistical. Two is
# the floor DISTILLATION needs -- it will not propose a skill without two
# independent verified successes on the same node -- so a pool of two makes the
# learning loop technically alive and practically useless: every candidate has
# to succeed for anything to be learned. Five leaves room for the population to
# actually differ, which is the entire premise of running a population rather
# than one agent with retries.
#
# It is also what the deployment is sized for before scaling up: the profiles
# below are set so their stated agent count divides into at least this per
# node, rather than quietly running two.
MIN_POOL = 5

# MAX_IN_FLIGHT bounds how many agents run at once, whatever the population.
# This is a concurrency bound, not a population bound, and the distinction is
# the point: 1000 agents is a legitimate request, 1000 simultaneous provider
# connections is a thundering herd that turns a rate limit into an outage.
# 64 because the pooled free tier is ~45 requests/minute -- more in flight than
# that cannot be served, it can only queue.
MAX_IN_FLIGHT = 64


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
        cache: Any = None,
        system_prompt: str = "",
        no_wait: bool = False,
        store: RunStore | None = None,
        state: RunState | None = None,
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
        # Semantic cache, shared across runs by whoever constructs them --
        # a session, or the control plane. Within ONE run almost nothing
        # repeats (every node's prompt differs, and a repair prompt carries
        # that candidate's own failures), so a per-run cache would be a
        # structural zero dressed up as a feature.
        #
        # REFUSED FOR EVAL, and enforced here rather than written in a doc: an
        # eval measures variance across repeats, and serving repeat 2 from
        # repeat 1 does not bias the interval, it collapses it. A bootstrap CI
        # over identical samples has zero width and reads as a strong result.
        if cache is not None and profile == "eval":
            raise ValueError(
                "an eval run cannot use the semantic cache: identical cached "
                "responses across repeats collapse the bootstrap interval and "
                "turn a measurement into an artefact of the first sample"
            )
        # Empty means the stock prompt. A session hands in the supervisor's
        # current version, which is how a patch reaches the fleet.
        self.system_prompt = system_prompt
        self.cache = cache
        if cache is not None:
            from swarmd.router.cached import CachedProvider

            provider = CachedProvider(provider, cache)
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
        # The pool owns the budget tracker; the run borrows it to answer
        # "does this fit" before spending anything.
        self.budget = getattr(provider, "budget", None)
        # Durable working set. A ration pause lasts hours, which is long
        # enough to cross a laptop sleep or a deploy, so the criterion, plan
        # and batch drafts have to outlive this process or the run buys them
        # twice.
        self.store = store if store is not None else RunStore()
        self.state = state if state is not None else RunState(
            run_id=self.run_id,
            task="",
            profile=self.profile.name,
            agents=self.agents,
        )
        # A pacer pause is where persistence matters most, so the run tells the
        # pacer to checkpoint on the way in and to clear the marker on the way
        # back out.
        pacer = getattr(provider, "pacer", None)
        if pacer is not None:
            pacer.run_id = self.run_id
            pacer.emit = self._emit_pace
            pacer.on_pause = self._on_pause
            pacer.on_resume = self._on_resume
            pacer.checkpoint_path = str(self.store.path_for(self.run_id))
            pacer.no_wait = no_wait
        self.no_wait = no_wait
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

    # -- resume -------------------------------------------------------------

    @classmethod
    def resume(
        cls, run_id: str, provider: Any, *, store: RunStore | None = None, **kw: Any
    ) -> SwarmRun:
        """Rebuild a run from disk. Raises if there is nothing to rebuild.

        The point is what this does NOT do: it does not re-synthesize the
        criterion, does not re-plan, and does not re-generate the batches. Those
        are the provider calls a restart would otherwise buy a second time, and
        the reason the store exists.

        The stored criterion is reused verbatim rather than re-derived. A resume
        that synthesized a fresh one would grade the second half of a run
        against a target the first half never saw, and the run's integrity hash
        would describe two different experiments averaged together.
        """
        store = store or RunStore()
        state = store.load(run_id)
        if state is None:
            raise FileNotFoundError(
                f"no stored run {run_id!r} under {store.root}. "
                f"`swarmd runs list` shows what can be resumed."
            )
        run = cls(
            provider,
            profile=state.profile,
            run_id=state.run_id,
            agents=state.agents or None,
            store=store,
            state=state,
            **kw,
        )
        # Balances first: the economy decides who may still spend, and a
        # population handed fresh allowances on resume would get a second
        # budget for work it has already been paid for.
        for agent_id, balance in state.balances.items():
            run.economy.restore(agent_id, balance)
        for agent_id in state.contained:
            run.redteam.contained_agents.add(agent_id)
        return run

    def restored_criterion(self) -> FrozenCriterion | None:
        """The frozen criterion from disk, or None for a fresh run."""
        if not self.state.criterion:
            return None
        from swarmd.swarm.criteria import Criterion
        from swarmd.swarm.synthesis import AttackReport

        criterion = Criterion.from_dict(self.state.criterion)
        if criterion.content_hash() != self.state.criterion_hash:
            # The document and the criterion it claims to be disagree. Refusing
            # is the only safe move: grading against a criterion whose hash is
            # not the one the run reported makes every number in the report
            # describe something else.
            raise ValueError(
                f"stored criterion hash {self.state.criterion_hash} does not "
                f"match its contents ({criterion.content_hash()}); refusing to "
                f"resume against a criterion that has changed underneath the run"
            )
        return FrozenCriterion(
            criterion=criterion,
            hash=self.state.criterion_hash,
            attempts=0,
            agreement=1.0,
            attack_report=AttackReport(True, ()),
        )

    def restored_plan(self) -> Plan | None:
        if not self.state.plan:
            return None
        from swarmd.swarm.planner import PlanNode, validate

        nodes = [
            PlanNode(
                name=str(n["name"]),
                instruction=str(n.get("instruction", "")),
                depends_on=tuple(n.get("depends_on") or ()),
            )
            for n in self.state.plan.get("nodes", [])
        ]
        return validate(nodes) if nodes else None

    # -- durability ---------------------------------------------------------

    def persist(self) -> None:
        """Write the working set. Called at every boundary that costs money.

        Cheap by design -- one atomic file replace -- because the alternative
        is persisting only at the end, which is exactly when a paused run has
        already been lost.
        """
        self.state.run_id = self.run_id
        self.state.agents = self.agents
        self.state.balances = {
            agent_id: account.balance
            for agent_id, account in self.economy._accounts.items()
        }
        self.state.contained = sorted(self.redteam.contained_agents)
        self.store.save(self.state)

    def _emit_pace(self, event: dict[str, Any]) -> None:
        """Pacer events reach the run's stream, so a pause is visible live."""
        kind = str(event.pop("kind", "pace"))
        self._emit(kind, **event)

    def _on_pause(self, cause: Any) -> None:
        """Checkpoint before waiting. The pause may outlive the process."""
        self.state.status = "paused"
        self.state.paused_reason = getattr(cause, "reason", "")
        self.state.resumes_at = float(getattr(cause, "resumes_at", 0.0))
        self.persist()

    def _on_resume(self, cause: Any) -> None:
        self.state.status = "running"
        self.state.paused_reason = ""
        self.state.resumes_at = 0.0
        self.persist()

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

    async def _ask(
        self, prompt: str, system: str, stage: str, *, cacheable: bool = True
    ) -> str:
        from swarmd.router.providers import LLMRequest

        try:
            response = await self.provider.complete(
                LLMRequest(
                    prompt=prompt,
                    system=system,
                    temperature=0.4,
                    # Room to reason AND answer. Reasoning models spend output
                    # tokens on thinking, so a budget sized for the answer
                    # alone returns an empty completion -- which is what
                    # gpt-oss-20b did to every criterion proposal until the
                    # schema hint grew and pushed it over the edge.
                    max_tokens=2000,
                    metadata={
                        "stage": stage,
                        "agent_id": f"{stage}-agent",
                        **({} if cacheable else {"cache": "bypass"}),
                    },
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
        # Not cacheable: see CachedProvider. Prompts already differ by angle,
        # so this is belt and braces -- but a future edit that unified the
        # angles would otherwise silently turn three opinions into one.
        return await self._ask(prompt, PROPOSER_SYSTEM, "criterion", cacheable=False)

    async def _propose_plan(self, task: str, attempt: int, index: int) -> str:
        # Each proposer decomposes under a different pressure. The earlier
        # version sent one identical prompt to all three and relied on sampling
        # for variety, which made the "competing plans" a single plan drawn
        # three times -- and once a cache sat in front of the provider, drawn
        # once and copied twice.
        angles = [
            "Prefer the fewest steps that still make each one checkable.",
            "Prefer steps that can run in parallel over a long chain.",
            "Prefer isolating the most failure-prone step so it retries alone.",
        ]
        prompt = (
            f"TASK: {task}\n\n"
            f"Decompose into a small dependency graph of steps.\n"
            f"PRIORITY: {angles[index % len(angles)]}\n"
            f"Respond with ONLY a JSON object matching this schema:\n"
            f"{PLAN_SCHEMA_HINT}"
        )
        self._emit("plan_proposal", attempt=attempt, proposer=index)
        return await self._ask(prompt, PLANNER_SYSTEM, "plan", cacheable=False)

    # -- the run ------------------------------------------------------------

    async def run(self, task: str) -> RunResult:
        started = time.monotonic()
        result = RunResult(run_id=self.run_id, task=task)
        self._emit("run_started", task=task, profile=self.profile.name)
        self.preflight()

        try:
            # Restored artefacts short-circuit synthesis. Each of these cost
            # proposer calls the first time; a resume that recomputed them
            # would spend the ration it was waiting for.
            self.state.task = self.state.task or task
            restored_criterion = self.restored_criterion()
            if restored_criterion is not None:
                result.criterion = restored_criterion
                self._emit(
                    "criterion_restored",
                    hash=restored_criterion.hash,
                    checks=len(restored_criterion.criterion.checks),
                )
            else:
                result.criterion = await self._synthesize_criterion(task)

            restored_plan = self.restored_plan()
            if restored_plan is not None:
                result.plan = restored_plan
                self._emit(
                    "plan_restored",
                    hash=restored_plan.content_hash(),
                    nodes=len(restored_plan.nodes),
                )
            else:
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
        # N proposer calls, content-addressed. Persisted immediately: losing it
        # to a restart means buying it again, and resuming under a DIFFERENT
        # criterion would grade the second half of a run against a target the
        # first half never saw.
        self.state.criterion = frozen.criterion.to_dict()
        self.state.criterion_hash = frozen.hash
        self.persist()
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
        self.state.plan = selection.plan.to_dict()
        self.persist()
        return selection.plan

    def estimated_calls(self, nodes: int = 3) -> int:
        """Roughly how many provider requests this run will make.

        Deliberately an estimate and deliberately stated: the exact figure
        depends on a plan that does not exist yet and on how many agents need
        repairs. Wrong by a factor of two is still the difference between "this
        fits" and "this is half your day".

        Composition, per node:
          1 batched generation, whatever the pool width (swarm/batch.py)
          up to max_repairs individual calls per agent that fails

        Plus criterion and plan synthesis, which are per-run, not per-node.
        """
        # The POPULATION, not the concurrency bound. Every agent asked for
        # runs and therefore may cost a repair call; MAX_IN_FLIGHT only decides
        # how many wait. Estimating against the bound made 500 and 1000 agents
        # quote the same price, which is the opposite of informative.
        pool = max(2, self.agents // max(1, nodes))
        if not self.agents_explicit:
            pool = min(ADVISORY_POOL, pool)
        synthesis = self.profile.proposers * 2          # criterion + plan
        generation = nodes                              # one batch per node
        # Assume half the pool needs a repair round. Optimistic assumptions
        # here produce a preflight that says yes and a run that stops early.
        repairs = nodes * (pool // 2) * self.profile.max_repairs
        return synthesis + generation + repairs

    def preflight(self, nodes: int = 3) -> dict[str, Any]:
        """What this run will cost against what is left today.

        Emitted before any work starts. An operator who asks for 1000 agents
        should be told what that means BEFORE spending it, not discover it from
        a run that stops halfway with a budget error.
        """
        estimate = self.estimated_calls(nodes)
        verdict = self.budget.affordable(estimate) if self.budget else {}
        payload: dict[str, Any] = {
            "agents": self.agents,
            "profile": self.profile.name,
            **verdict,
        }

        # The timeline, not just the yes/no. Once a run pauses instead of
        # failing, "does not fit today" stopped being the useful answer:
        # "finishes at 18:40 after one pause" and "spans three days" are both
        # "does not fit", and only the operator can say which is acceptable.
        forecast = getattr(self.provider, "forecast", None)
        if callable(forecast):
            try:
                payload["forecast"] = forecast(estimate)
            except Exception as exc:  # noqa: BLE001 - a projection must never
                # cost the run. Being unable to predict the timeline is not a
                # reason to refuse to start.
                logger.warning("preflight forecast unavailable: %s", exc)

        self._emit("preflight", **payload)

        plan = payload.get("forecast") or {}
        if plan.get("verdict") in {"fits_today_with_pauses", "spans_days"}:
            logger.warning(
                "preflight: ~%d calls needs %d sessions with %d pause(s); "
                "first pause in %.1fh, projected finish in %.1fh. The run will "
                "wait rather than fail -- pass --no-wait to stop instead.",
                estimate,
                plan.get("sessions_needed", 1),
                plan.get("expected_pauses", 0),
                max(0.0, (plan.get("first_pause_at") or 0) - time.time()) / 3600,
                max(0.0, (plan.get("projected_finish") or 0) - time.time()) / 3600,
            )
        elif plan.get("verdict") == "exceeds_horizon":
            logger.warning(
                "preflight: ~%d calls does not finish within the forecast "
                "horizon at the current allowance. Reduce --agents or add a "
                "credential; the run will otherwise pause repeatedly for days.",
                estimate,
            )
        elif verdict and not verdict.get("fits", True):
            logger.warning(
                "preflight: this run needs ~%d calls and %d remain today; it "
                "will exhaust the budget and stop partway",
                estimate, verdict.get("remaining_today", 0),
            )
        return payload

    def _pool_size(self, plan: Plan) -> int:
        """Agents per plan node.

        ANATOMY: the floor of 2
          Distillation needs two independent verified successes on the same
          node before it will propose a skill, so a pool of one makes the
          learning loop structurally dead no matter what else is configured.

        ANATOMY: ADVISORY_POOL (32)
          What the implemented capacity levers support. Generation is now
          batched -- one call per node regardless of pool size -- so the pool no
          longer costs a call per agent. REPAIRS are still one call each,
          because a repair prompt carries one candidate's specific failures, so
          the worst case remains linear in pool size at max_repairs per agent.
          32 is where that worst case still fits the ceiling.

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
        budget = max(MIN_POOL, self.agents // nodes)
        if not self.agents_explicit:
            return min(ADVISORY_POOL, budget)

        # An operator who explicitly asks for fewer than the floor gets fewer.
        # The floor is an operating default, not a veto: a deliberate 2-agent
        # run is a legitimate thing to ask for, and a cap that cannot be
        # overridden is a lie about who is in control.
        if self.agents < MIN_POOL * nodes:
            # Down to two, never to one. Two is not an operating preference,
            # it is what distillation structurally requires: it will not
            # propose a skill without two independent verified successes on the
            # same node, so a pool of one turns the learning loop off entirely.
            budget = max(2, self.agents // nodes)
            self._emit(
                "pool_below_floor",
                requested=self.agents,
                per_node=budget,
                floor=MIN_POOL,
                reason=(
                    f"{self.agents} agents over {nodes} nodes is {budget} per "
                    f"node, below the {MIN_POOL} the run normally keeps in "
                    f"flight. Honoured as asked. Note that distillation needs "
                    f"two independent verified successes on the same node, so "
                    f"a per-node pool of one makes the learning loop "
                    f"structurally dead."
                ),
            )
            return budget

        # AN EXPLICIT COUNT IS HONOURED EXACTLY. Asking for 1000 agents used to
        # give 192 -- HARD_POOL silently clamped each node's pool to 64 -- and
        # nothing said so, which made the control a suggestion.
        #
        # The concern behind that clamp was real: a level of 1000 concurrent
        # workers opens 1000 simultaneous provider connections and turns a rate
        # limit into a thundering herd. But the fix for too much work AT ONCE
        # is to bound concurrency, not to quietly run fewer agents. The
        # population is what the operator asked for; MAX_IN_FLIGHT is how fast
        # it is allowed to move (see `_execute`).
        if budget > ADVISORY_POOL:
            self._emit(
                "pool_above_advisory",
                requested=budget,
                advisory=ADVISORY_POOL,
                granted=budget,
                in_flight_cap=MAX_IN_FLIGHT,
                reason=(
                    "granted in full. Generation is batched, so a wide pool "
                    "costs one call per node; REPAIRS are one call each, so "
                    f"cost grows with population. At most {MAX_IN_FLIGHT} "
                    "agents run concurrently. The daily budget and the cost "
                    "ceiling are what will stop the run, not a cap on how many "
                    "agents you asked for."
                ),
            )
        return budget

    async def _batch_generate(
        self, task: str, node: PlanNode, context: WorkerContext, k: int
    ) -> Batch:
        """One call producing K candidate solutions for this node.

        WHY THE BATCH IS NOT CHARGED TO THE AGENTS. The first version divided
        its cost by K and debited each agent's allowance, on the reasoning that
        work the market does not price is work the market cannot select on.
        That was wrong twice over.

        It changes no ordering: every agent in the pool receives the same
        subsidy, so a uniform debit shifts all balances equally and the
        selection is identical.

        And it broke a detector. `BudgetSiphon` fires when an agent burns most
        of its allowance with nothing verified; pre-debiting a share left too
        little allowance for that threshold to be reachable, so a seeded siphon
        went bankrupt uncaught -- the exact failure this detector had already
        been fixed for once. A subsidy that eats the headroom a safety
        threshold depends on is not a neutral accounting choice.

        The dollars are not lost: the batch call goes through the provider
        pool, so it lands in the ledger like every other call. What the economy
        prices is what agents choose to do -- repairs -- which is where they
        actually differ.
        """
        from swarmd.swarm.worker import GenericWorker

        # Skills retrieved ONCE for the batch rather than per agent. Every
        # agent in a pool queries the same library with the same node text, so
        # per-agent retrieval returned identical results and differed only in
        # how many times it ran.
        skills = (
            context.skills.retrieve(f"{task} {node.instruction}")
            if context.skills
            else []
        )
        prompt = GenericWorker("batch", context).build_prompt(task, node, skills, ())

        batch = await generate_batch(
            provider=self.provider,
            prompt=prompt,
            k=k,
            max_tokens=context.max_tokens,
            temperature=context.temperature,
            system=context.system,
            stage=node.name,
        )
        if batch.variants:
            metrics.record_batch(stage=node.name, saved=batch.saved_calls)
            self._emit(
                "batch_generated",
                node=node.name,
                requested=k,
                variants=len(batch.variants),
                calls=batch.calls,
                saved_calls=batch.saved_calls,
            )
        else:
            # No variants: the pool falls back to generating individually. The
            # saving is lost, the node is not.
            self._emit("batch_failed", node=node.name, requested=k)
        return batch

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
            system=self.system_prompt or WORKER_SYSTEM,
        )

        # Nodes an earlier process finished are seeded back in, not just
        # skipped. Skipping alone drops them from the report, so a resumed run
        # would announce "completed" while returning only the half of the work
        # done after the restart -- and its integrity hash would cover only
        # that half.
        results: list[WorkerResult] = [
            WorkerResult.from_state(row) for row in self.state.results
        ]
        if results:
            self._emit("results_restored", nodes=len(results))
        for level in plan.levels():
            self._emit("level_started", nodes=level)
            metrics.set_queue_depth(stage="plan", depth=len(level))

            async def run_agent(name: str, draft: str = "") -> WorkerResult:
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
                # A pre-generated variant arrives as a COMPLETED checkpoint
                # step. The worker's resume path then skips generating and
                # charges nothing for it -- the same mechanism that stops a
                # killed agent redoing its work, used here to stop K agents
                # duplicating one call.
                carried: Checkpoint | None = None
                if draft:
                    carried = Checkpoint(
                        task_id=name,
                        agent_id=account.agent_id,
                        completed_steps=["generate:1"],
                        data={"generate:1": draft},
                    )
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

            # Nodes already finished in an earlier process are not re-run.
            # Their results were persisted the moment they passed, so redoing
            # them would spend the ration twice for an answer already held.
            done = self.state.finished_nodes
            level = [name for name in level if name not in done]
            if not level:
                continue

            # One batched call per node, then the pool grades its variants.
            batches: dict[str, Batch] = {}
            for name in level:
                stored = self.state.drafts.get(name)
                if stored:
                    # Bought before the restart. Reused rather than re-bought:
                    # this is the single most expensive artefact a crash can
                    # lose, one provider call per node.
                    batches[name] = Batch(
                        variants=tuple(stored),
                        requested=pool,
                        calls=0,
                        cost_credits=0.0,
                    )
                    self._emit(
                        "batch_restored", node=name, variants=len(stored)
                    )
                    continue
                batches[name] = await self._batch_generate(
                    task, plan.node(name), context, pool
                )
                # One provider call bought K variants. Persisting them here
                # means a restart mid-level reuses them instead of paying for
                # the same batch twice -- the single most expensive thing a
                # crash can lose.
                self.state.drafts[name] = list(batches[name].variants)
            self.persist()

            # Concurrency bound. Every agent the operator asked for runs;
            # at most MAX_IN_FLIGHT of them are in the air at once, so a wide
            # population queues instead of stampeding the provider pool.
            gate = asyncio.Semaphore(MAX_IN_FLIGHT)

            # `gate` bound as a default argument: it is recreated per level,
            # and a closure over the loop variable would let a later level's
            # semaphore govern an earlier level's tasks.
            async def bounded(
                name: str, draft: str, gate: asyncio.Semaphore = gate
            ) -> WorkerResult:
                async with gate:
                    return await run_agent(name, draft)

            level_results = await asyncio.gather(
                *(
                    bounded(name, batches[name].for_agent(index))
                    for name in level
                    for index in range(pool)
                ),
                return_exceptions=True,
            )
            for item in level_results:
                if isinstance(item, WorkerResult):
                    results.append(item)
                    # Node finished. Recorded so a resume skips it rather than
                    # re-running work that already passed its criterion.
                    self.state.results.append(item.to_state())
                    self.state.remember(item.checkpoint)
                elif isinstance(item, BaseException):
                    # One node failing must not abort the level. The gate will
                    # record it as a failure, which is the honest outcome.
                    logger.warning("node raised: %s", item)
                    self._emit("node_error", detail=str(item)[:200])

            self.persist()
            self.economy.reap()
            self.economy.reproduce()

        return results

    def _distil_instruction(
        self, node: str, plan_node: PlanNode | None, outcomes: list[Any]
    ) -> str:
        """Turn repeated successes into an APPROACH, not an answer.

        This used to be `max(outputs, key=len)` -- the longest successful
        output, stored verbatim as the skill's instruction. Live, that
        distilled `{"accuracy": 94.3, "baseline": 82.1}` and offered it to
        every future run as advice.

        That is worse than useless. It is the specific answer to one task
        presented as a general method, so a later run on different numbers is
        handed the wrong ones and told they worked. It is also exactly what the
        red-team's `library_poisoning` detector exists to catch, which means
        the system was reliably generating the thing it was built to reject.

        A skill has to describe HOW. What generalises from a set of successes
        is the SHAPE they share -- which keys, of which types -- and the step
        that produced them. The values are what must not carry over.
        """

        shapes: dict[str, str] = {}
        for outcome in outcomes:
            for key, value in (outcome.candidate.artifacts or {}).items():
                if key.startswith("_"):
                    continue
                shapes.setdefault(key, type(value).__name__)

        what = plan_node.instruction if plan_node else node
        # NO NODE NAME. Plan node names are generated fresh for every run, so
        # advice that opens "for steps like 'extract_dates'" describes a step
        # that does not exist in the plan reading it. Measured: a library of
        # such skills made the treatment arm WORSE than control -- 0/5 against
        # 2/5, node pass rate 56.7% against 65.6% -- because retrieval was
        # injecting confident instructions about steps nobody had.
        #
        # What can recur is the KIND of work and the shape it produced.
        if not shapes:
            # Nothing structured to generalise from. Describe the work and stop
            # rather than inventing a method nobody demonstrated.
            return f"When a step calls for this: {what}"

        fields = ", ".join(f"{k} ({v})" for k, v in sorted(shapes.items()))
        return (
            f"When a step calls for this: {what} "
            f"Produce a JSON object with these fields: {fields}. "
            f"Values must come from the task at hand -- this records the shape "
            f"that satisfied the criterion {len(outcomes)} times, not the "
            f"answer, which will differ."
        )

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
        plan = result.plan
        for node, outcomes in by_node.items():
            plan_node = None
            if plan is not None:
                try:
                    plan_node = plan.node(node)
                except Exception:  # noqa: BLE001 - a missing node is not fatal
                    plan_node = None
            if len(outcomes) < self.min_evidence:
                self._emit(
                    "skill_skipped",
                    node=node,
                    reason=f"{len(outcomes)} success(es), need {self.min_evidence}",
                )
                continue

            instruction = self._distil_instruction(node, plan_node, outcomes)
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
            "cache": (
                self.provider.report()
                if self.cache is not None and hasattr(self.provider, "hit_rate")
                else None
            ),
            "ablation": {"skills_enabled": self.use_skills},
        }


def make_node(name: str, instruction: str, deps: tuple[str, ...] = ()) -> PlanNode:
    """Convenience for tests and fixed pipelines."""
    return PlanNode(name=name, instruction=instruction, depends_on=deps)


def empty_candidate() -> Candidate:
    return Candidate()
