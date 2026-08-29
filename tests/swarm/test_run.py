"""End-to-end run tests: an unknown task in, a verified result out.

The properties under test are the ones the whole system claims:
  - a criterion is frozen BEFORE any solving happens
  - the generated plan runs on the real DAG executor
  - chaos does not change the result (integrity hash equality)
  - contained work never reaches the output
  - the control arm is a real ablation, not a flag that does nothing
"""

from __future__ import annotations

import json

import pytest

from swarmd.router.providers import LLMResponse
from swarmd.swarm.run import PROFILES, SwarmRun
from swarmd.swarm.skills import SkillLibrary

# Deliberately strong enough to survive the adversarial pass. An earlier
# version used only min_distinct_words + output_nonempty, which the `echo_task`
# attack passes trivially -- the synthesizer correctly refused to freeze it,
# which is the system working rather than a test problem.
STRONG_CRITERION = {
    "description": "the step emits a structured summary artifact",
    "checks": [
        {"kind": "json_parses", "params": {"required_keys": ["summary", "count"]}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
    ],
}

PLAN = {
    "rationale": "read then verify",
    "nodes": [
        {"name": "gather", "instruction": "produce notes.json", "depends_on": []},
        {"name": "verify", "instruction": "produce report.json",
         "depends_on": ["gather"]},
    ],
}

GOOD_OUTPUT = json.dumps(
    {
        "summary": "loaded the source records, normalised fields, computed "
                   "statistics, and wrote them for downstream verification",
        "count": 128,
        "fields": ["identifier", "timestamp", "value"],
    }
)


class ScriptedProvider:
    """Answers by prompt shape. No network, fully deterministic."""

    name = "scripted"

    def __init__(self, *, worker_output: str = GOOD_OUTPUT, fail_after: int | None = None):
        self.worker_output = worker_output
        self.calls = 0
        self.fail_after = fail_after
        self.prompts: list[str] = []

    async def complete(self, request):
        self.calls += 1
        self.prompts.append(request.prompt)
        if self.fail_after is not None and self.calls > self.fail_after:
            raise RuntimeError("provider exhausted")

        if "matching this schema" in request.prompt and "checks" in request.prompt:
            text = json.dumps(STRONG_CRITERION)
        elif "matching this schema" in request.prompt:
            text = json.dumps(PLAN)
        else:
            text = self.worker_output
        return LLMResponse(
            text=text, provider=self.name, model="scripted-v1",
            latency_s=0.001, tokens_in=10, tokens_out=20,
        )


class NoChaos:
    def should_kill(self):
        return False


class AlwaysChaos:
    """Kills the first agent for every node."""

    def __init__(self):
        self.kills = 0

    def should_kill(self):
        self.kills += 1
        return self.kills <= 2


def _run(**kw):
    return SwarmRun(ScriptedProvider(), profile="smoke", **kw)


# --- the happy path --------------------------------------------------------


async def test_a_run_completes_and_reports_everything():
    run = _run()
    result = await run.run("summarise the source records")

    assert result.status == "completed"
    assert result.criterion is not None
    assert result.plan is not None
    # A POOL per node, not one agent. Population search, market selection and
    # distillation evidence all require several agents attempting the same work.
    assert {r.node for r in result.results} == {"gather", "verify"}
    assert len(result.results) > 2, "each node must be attempted by a pool"
    assert all(r.passed for r in result.results)


async def test_the_criterion_is_frozen_before_any_solving():
    """The ordering IS the claim. A criterion authored after seeing candidate
    output is a rationalisation, not a test."""
    provider = ScriptedProvider()
    run = SwarmRun(provider, profile="smoke")
    await run.run("do the thing")

    schema_prompts = [
        i for i, p in enumerate(provider.prompts) if "checks" in p and "schema" in p
    ]
    worker_prompts = [i for i, p in enumerate(provider.prompts) if "STEP:" in p]
    assert schema_prompts and worker_prompts
    assert max(schema_prompts) < min(worker_prompts)


async def test_the_frozen_criterion_hash_is_a_run_output():
    result = await _run().run("t")
    assert result.criterion is not None
    assert len(result.criterion.hash) == 16
    assert result.to_dict()["criterion"]["hash"] == result.criterion.hash


async def test_the_generated_plan_is_a_run_output():
    result = await _run().run("t")
    assert result.plan is not None
    assert result.to_dict()["plan"]["nodes"]


async def test_the_report_is_json_serialisable():
    """It feeds the dashboard and the ledger; anything unserialisable is a bug."""
    run = _run()
    result = await run.run("t")
    json.dumps(run.report(result))


# --- chaos and integrity ---------------------------------------------------


async def test_chaos_does_not_change_the_result():
    """SLO-2: no error budget. This is the guarantee everything rests on."""
    clean = await SwarmRun(ScriptedProvider(), profile="smoke", chaos=NoChaos()).run(
        "reproduce the reported figure"
    )
    chaotic = await SwarmRun(
        ScriptedProvider(), profile="smoke", chaos=AlwaysChaos()
    ).run("reproduce the reported figure")

    assert clean.status == chaotic.status == "completed"
    assert clean.integrity_hash() == chaotic.integrity_hash()


async def test_killed_agents_are_requeued_rather_than_losing_work():
    chaos = AlwaysChaos()
    result = await SwarmRun(ScriptedProvider(), profile="smoke", chaos=chaos).run("t")
    assert chaos.kills > 0
    assert all(r.passed for r in result.results)


async def test_the_integrity_hash_ignores_completion_order():
    """Chaos changes the order work finishes in; that must not change the hash."""
    first = await _run().run("stable task")
    second = await _run().run("stable task")
    assert first.integrity_hash() == second.integrity_hash()


async def test_moving_prompt_bytes_between_roles_does_not_change_the_result(
    monkeypatch,
):
    """Prefix caching is a PLACEMENT change, and this is the proof.

    The run-stable bytes -- the task and the frozen criterion -- moved out of
    the user turn and into the system message so that a provider's automatic
    prefix cache can hold them. Nothing was added, dropped or reworded, so
    against a fixed scripted provider the two layouts must produce the same
    candidates, the same grading and therefore the same integrity hash.

    Asserted against `SWARMD_PREFIX_ORDER=legacy`, which rebuilds the
    pre-change single user message byte for byte: if the hashes ever diverge,
    the reordering changed what the run PRODUCED, and the rollback switch is
    the thing to reach for.
    """
    monkeypatch.setenv("SWARMD_PREFIX_ORDER", "legacy")
    before = await _run().run("reproduce the reported figure")

    monkeypatch.setenv("SWARMD_PREFIX_ORDER", "hoisted")
    after = await _run().run("reproduce the reported figure")

    assert before.status == after.status == "completed"
    assert before.criterion is not None and after.criterion is not None
    assert before.criterion.hash == after.criterion.hash
    assert before.integrity_hash() == after.integrity_hash()
    assert [r.candidate.output for r in before.results] == [
        r.candidate.output for r in after.results
    ]


async def test_the_two_prompt_layouts_are_genuinely_different_prompts():
    """Guards the test above from being a tautology.

    Equal hashes only mean something if the two arms really did send
    different bytes. A `SWARMD_PREFIX_ORDER` that silently did nothing would
    make the parity test pass forever while proving nothing at all.
    """
    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SWARMD_PREFIX_ORDER", "legacy")
        legacy = ScriptedProvider()
        await SwarmRun(legacy, profile="smoke").run("reproduce the reported figure")

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv("SWARMD_PREFIX_ORDER", "hoisted")
        hoisted = ScriptedProvider()
        await SwarmRun(hoisted, profile="smoke").run("reproduce the reported figure")

    legacy_worker = [p for p in legacy.prompts if "STEP:" in p]
    hoisted_worker = [p for p in hoisted.prompts if "STEP:" in p]
    assert legacy_worker and hoisted_worker
    assert all("TASK:" in p for p in legacy_worker)
    assert not any("TASK:" in p for p in hoisted_worker)


# --- honest failure --------------------------------------------------------


async def test_a_weak_criterion_fails_the_task_rather_than_grading_against_it():
    weak = {"description": "any output", "checks": [
        {"kind": "output_nonempty", "params": {"min_chars": 1}}]}

    class WeakProvider(ScriptedProvider):
        async def complete(self, request):
            if "checks" in request.prompt and "schema" in request.prompt:
                return LLMResponse(
                    text=json.dumps(weak), provider="w", model="m",
                    latency_s=0.0, tokens_in=1, tokens_out=1,
                )
            return await super().complete(request)

    result = await SwarmRun(WeakProvider(), profile="smoke").run("t")
    assert result.status == "failed_criterion"
    assert "weak" in result.error


async def test_a_provider_outage_is_reported_not_raised():
    """A dead provider must not surface as a crash in the orchestrator."""
    result = await SwarmRun(
        ScriptedProvider(fail_after=0), profile="smoke"
    ).run("t")
    assert result.status in {"failed_criterion", "error"}
    assert result.error


async def test_a_run_that_fails_still_produces_a_report():
    run = SwarmRun(ScriptedProvider(fail_after=0), profile="smoke")
    result = await run.run("t")
    json.dumps(run.report(result))


# --- containment -----------------------------------------------------------


async def test_contained_work_never_reaches_the_integrity_hash():
    """PRD acceptance criterion 4, at the run level."""
    padded = " ".join(["result"] * 200)
    result = await SwarmRun(
        ScriptedProvider(worker_output=padded), profile="smoke"
    ).run("t")

    # The padded output is caught by criterion_gaming if it passes at all.
    for outcome in result.results:
        if outcome.contained:
            assert not outcome.passed


async def test_the_redteam_audit_is_part_of_the_report():
    run = _run()
    result = await run.run("t")
    report = run.report(result)
    assert "redteam_audit" in report
    assert report["redteam"]["llm_calls_used"] == 0


# --- the ablation ----------------------------------------------------------


async def test_the_control_arm_genuinely_disables_skills(tmp_path):
    """A flag that changes nothing would make every improvement claim vacuous."""
    library = SkillLibrary(tmp_path / "skills.json")

    treatment = SwarmRun(
        ScriptedProvider(), profile="smoke", skills=library, use_skills=True
    )
    assert treatment.skills is library

    control = SwarmRun(
        ScriptedProvider(), profile="smoke", skills=library, use_skills=False
    )
    assert control.skills is None

    result = await control.run("t")
    assert result.proposed_skills == []


async def test_the_ablation_state_is_recorded_in_the_report(tmp_path):
    run = SwarmRun(ScriptedProvider(), profile="smoke", use_skills=False)
    report = run.report(await run.run("t"))
    assert report["ablation"]["skills_enabled"] is False


async def test_verified_successes_propose_skills_pending_a_human(tmp_path):
    library = SkillLibrary(tmp_path / "skills.json")
    run = SwarmRun(ScriptedProvider(), profile="smoke", skills=library)
    result = await run.run("summarise the source records")

    assert result.proposed_skills
    assert library.pending()          # awaiting review
    assert library.approved() == []   # nothing self-approved


# --- cost and profiles -----------------------------------------------------


async def test_every_run_writes_a_cost_ledger():
    run = _run()
    await run.run("t")
    assert run.account.ledger.rows()
    assert run.account.report()["rows"] > 0


async def test_the_criterion_and_plan_decisions_are_ledger_rows():
    """Anything in a report must trace to a row (ADR-007)."""
    run = _run()
    await run.run("t")
    kinds = {r.kind for r in run.account.ledger.rows()}
    assert "criterion_frozen" in kinds
    assert "plan_selected" in kinds


async def test_a_ledger_written_to_disk_survives_the_run(tmp_path):
    path = tmp_path / "ledger.jsonl"
    run = SwarmRun(ScriptedProvider(), profile="smoke", ledger_path=str(path))
    await run.run("t")
    assert path.exists()
    assert run.account.verify()["match"] is True


def test_profiles_are_derived_from_the_capacity_plan():
    assert set(PROFILES) == {"smoke", "standard", "deep", "eval"}
    assert PROFILES["smoke"].target_calls < PROFILES["standard"].target_calls
    assert PROFILES["standard"].target_calls < PROFILES["deep"].target_calls


def test_an_unknown_profile_is_refused():
    with pytest.raises(ValueError, match="unknown profile"):
        SwarmRun(ScriptedProvider(), profile="enormous")


# --- observability ---------------------------------------------------------


async def test_events_are_emitted_for_the_dashboard():
    events = []
    run = SwarmRun(ScriptedProvider(), profile="smoke", on_event=events.append)
    await run.run("t")

    kinds = {e["kind"] for e in events}
    assert {"run_started", "criterion_frozen", "plan_selected", "node_finished",
            "run_finished"} <= kinds
    assert all(e["run_id"] == run.run_id for e in events)


async def test_reasoning_thoughts_reach_the_event_stream():
    """The dashboard shows thought, action, observation -- it needs the thoughts."""
    events = []
    run = SwarmRun(ScriptedProvider(), profile="smoke", on_event=events.append)
    await run.run("t")
    thoughts = [e for e in events if e["kind"] == "thought"]
    assert thoughts
    assert all("decision" in t and "tick" in t for t in thoughts)


async def test_a_broken_event_sink_never_stalls_the_run():
    """An observability path that can stall a run is a liability."""
    def explode(_event):
        raise RuntimeError("dashboard is on fire")

    result = await SwarmRun(
        ScriptedProvider(), profile="smoke", on_event=explode
    ).run("t")
    assert result.status == "completed"


# --- the pool --------------------------------------------------------------


async def test_each_node_is_attempted_by_a_pool_not_one_agent():
    """This was a real defect: one agent per node meant --agents was unused,
    there was no population to select over, and distillation -- which needs two
    verified successes on a node -- could never fire."""
    result = await _run().run("summarise the source records")
    by_node: dict[str, int] = {}
    for outcome in result.results:
        by_node[outcome.node] = by_node.get(outcome.node, 0) + 1
    assert all(count >= 2 for count in by_node.values()), by_node


async def test_every_agent_in_the_pool_holds_its_own_budget():
    """Otherwise the market has nothing to select over."""
    run = _run()
    await run.run("t")
    accounts = run.economy.all()
    assert len(accounts) >= 4
    assert all(a.attempts or a.spent for a in accounts if a.alive)


async def test_pool_size_is_floored_so_distillation_always_has_evidence():
    from swarmd.swarm.planner import PlanNode, validate

    run = _run()
    # The widest plan the validator permits, so the per-node budget is at
    # its smallest.
    plan = validate([PlanNode(name=f"n{i}", instruction="produce x.json")
                     for i in range(12)])
    assert run._pool_size(plan) >= 2


async def test_a_profile_pool_stays_within_the_advisory_cap():
    """Batching removed the per-agent generation call; repairs are still one
    call each, so a PROFILE stays bounded even though an operator is not."""
    from swarmd.swarm.planner import PlanNode, validate
    from swarmd.swarm.run import ADVISORY_POOL

    run = SwarmRun(ScriptedProvider(), profile="standard")
    plan = validate([PlanNode(name="only", instruction="produce x.json")])
    assert run._pool_size(plan) <= ADVISORY_POOL


async def test_an_explicit_agent_count_overrides_the_advisory_cap():
    """A cap the operator cannot override is a lie about who is in control.

    There is no upper clamp any more: an explicit count is honoured exactly,
    and concurrency is bounded separately by MAX_IN_FLIGHT. The daily budget
    and the cost ceiling are the real protections, and the preflight says what
    a run will cost before it starts.
    """
    from swarmd.swarm.planner import PlanNode, validate
    from swarmd.swarm.run import ADVISORY_POOL

    plan = validate([PlanNode(name="only", instruction="produce x.json")])
    run = SwarmRun(ScriptedProvider(), profile="standard", agents=200)
    assert run._pool_size(plan) > ADVISORY_POOL
    assert run._pool_size(plan) == 200


async def test_exceeding_the_advisory_cap_emits_a_warning_not_a_silent_grant():
    """An expensive run should be a decision, not a surprise."""
    from swarmd.swarm.planner import PlanNode, validate

    seen = []
    run = SwarmRun(
        ScriptedProvider(), profile="standard", agents=200,
        on_event=lambda e: seen.append(e),
    )
    run._pool_size(validate([PlanNode(name="only", instruction="produce x.json")]))
    assert any(e["kind"] == "pool_above_advisory" for e in seen)


async def test_a_small_explicit_count_still_floors_at_two():
    """One agent per node makes distillation structurally impossible: it needs
    two independent verified successes on the same node."""
    from swarmd.swarm.planner import PlanNode, validate

    run = SwarmRun(ScriptedProvider(), profile="smoke", agents=1)
    plan = validate([PlanNode(name="only", instruction="produce x.json")])
    assert run._pool_size(plan) == 2


async def test_a_zero_agent_run_is_refused():
    with pytest.raises(ValueError, match="at least 1"):
        SwarmRun(ScriptedProvider(), profile="smoke", agents=0)


async def test_a_pool_produces_the_evidence_distillation_requires(tmp_path):
    from swarmd.swarm.skills import SkillLibrary

    library = SkillLibrary(tmp_path / "skills.json")
    run = SwarmRun(ScriptedProvider(), profile="smoke", skills=library)
    result = await run.run("summarise the source records")

    assert result.proposed_skills, "a pool should yield repeatable-approach evidence"
    assert library.pending()


# --- checkpoint recovery (PRD G7) -------------------------------------------


class CountingProvider(ScriptedProvider):
    """Counts worker generations so a redo is visible as a number."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.worker_calls = 0

    async def complete(self, request):
        if "STEP:" in request.prompt:
            self.worker_calls += 1
        return await super().complete(request)


class KillOnce:
    """Kills the first agent of the run, then never again."""

    def __init__(self):
        self.kills = 0

    def should_kill(self):
        if self.kills == 0:
            self.kills += 1
            return True
        return False


async def test_a_killed_node_resumes_rather_than_repeating_the_model_call():
    """PRD G7: completed work is never redone.

    The worker checkpoints after generating. A replacement receiving that
    checkpoint must reuse the text rather than pay for it again -- which is the
    difference between resuming and merely being deterministic enough that a
    redo produces the same answer.
    """
    from swarmd.swarm.criteria import Criterion
    from swarmd.swarm.economy import Economy
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import GenericWorker, WorkerContext

    provider = CountingProvider()
    criterion = Criterion.from_dict(STRONG_CRITERION)
    economy = Economy()
    node = PlanNode(name="solve", instruction="produce out.json")

    first = economy.spawn()
    context = WorkerContext(
        provider=provider, criterion=criterion, economy=economy, max_repairs=0
    )
    outcome = await GenericWorker(first.agent_id, context).execute("t", node)
    assert provider.worker_calls == 1
    assert outcome.checkpoint is not None
    assert "generate:1" in outcome.checkpoint.completed_steps

    # A replacement resuming from that checkpoint must NOT call the model.
    replacement = economy.spawn()
    resumed = await GenericWorker(replacement.agent_id, context).execute(
        "t", node, checkpoint=outcome.checkpoint
    )
    assert provider.worker_calls == 1, "the replacement redid the model call"
    assert resumed.passed
    assert resumed.credits_spent == 0.0, "the replacement paid for skipped work"


async def test_a_resumed_agent_says_so_in_its_reasoning():
    """An operator watching the dashboard must see recovery, not a silent retry."""
    from swarmd.swarm.criteria import Criterion
    from swarmd.swarm.economy import Economy
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import GenericWorker, WorkerContext

    economy = Economy()
    context = WorkerContext(
        provider=CountingProvider(),
        criterion=Criterion.from_dict(STRONG_CRITERION),
        economy=economy,
        max_repairs=0,
    )
    node = PlanNode(name="solve", instruction="produce out.json")
    first = await GenericWorker(economy.spawn().agent_id, context).execute("t", node)
    resumed = await GenericWorker(economy.spawn().agent_id, context).execute(
        "t", node, checkpoint=first.checkpoint
    )
    assert "resumed" in [t["decision"] for t in resumed.thoughts]


async def test_a_kill_between_generating_and_grading_reuses_the_generation():
    """The interesting recovery case.

    A checkpoint holding a PASSED grade short-circuits the whole attempt, which
    is a stronger skip. This covers the partial one: generated but not yet
    graded, so the replacement must reuse the text and grade it rather than
    calling the model again.
    """
    from swarmd.swarm.criteria import Criterion
    from swarmd.swarm.economy import Economy
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import GenericWorker, WorkerContext
    from swarmd.task import Checkpoint

    provider = CountingProvider()
    economy = Economy()
    context = WorkerContext(
        provider=provider,
        criterion=Criterion.from_dict(STRONG_CRITERION),
        economy=economy,
        max_repairs=0,
    )
    node = PlanNode(name="solve", instruction="produce out.json")

    first = await GenericWorker(economy.spawn().agent_id, context).execute("t", node)
    assert provider.worker_calls == 1
    assert first.checkpoint is not None

    # Truncate to the moment just after generating: killed before grading.
    partial = Checkpoint(
        task_id=node.name,
        agent_id="dead",
        completed_steps=["generate:1"],
        data={"generate:1": first.checkpoint.data["generate:1"]},
    )
    resumed = await GenericWorker(economy.spawn().agent_id, context).execute(
        "t", node, checkpoint=partial
    )

    assert provider.worker_calls == 1, "the replacement redid the model call"
    assert "skipped_generate" in [t["decision"] for t in resumed.thoughts]
    assert resumed.passed


async def test_the_run_carries_a_checkpoint_across_a_chaos_kill():
    """End to end: chaos kills, the replacement resumes, the node still passes."""
    provider = CountingProvider()
    run = SwarmRun(provider, profile="smoke", chaos=KillOnce())
    result = await run.run("summarise the source records")

    assert result.status == "completed"
    assert all(r.passed for r in result.results)


async def test_recovery_is_bounded_so_relentless_chaos_terminates():
    """A run where chaos always wins must end rather than spin forever."""
    class AlwaysKill:
        def should_kill(self):
            return True

    run = SwarmRun(ScriptedProvider(), profile="smoke", chaos=AlwaysKill())
    result = await run.run("summarise the source records")

    assert result.status == "completed"          # the run itself finished
    assert not any(r.passed for r in result.results)
    assert any("recovery bound" in f for r in result.results for f in r.failures)


async def test_checkpoints_are_json_serialisable():
    """A checkpoint that cannot round-trip through a store only works in memory,
    which is the one case where it is not needed."""
    import json

    from swarmd.swarm.criteria import Criterion
    from swarmd.swarm.economy import Economy
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import GenericWorker, WorkerContext

    economy = Economy()
    context = WorkerContext(
        provider=ScriptedProvider(),
        criterion=Criterion.from_dict(STRONG_CRITERION),
        economy=economy,
        max_repairs=0,
    )
    outcome = await GenericWorker(economy.spawn().agent_id, context).execute(
        "t", PlanNode(name="solve", instruction="produce out.json")
    )
    assert outcome.checkpoint is not None
    json.dumps(outcome.checkpoint.to_dict())


def test_no_profile_degenerates_consensus_into_a_union():
    """Two proposers is not a quorum, it is a union.

    The merge keeps a check when ceil(valid * min_agreement) proposers asked
    for it. At three that is 2 of 3. At two it is 1 of 2 -- so every check
    either proposer thought of survives, and the criterion becomes the union of
    two opinions rather than what they agreed on. Smoke ran that way and was
    graded against 13 checks where standard produced 5.
    """
    import math

    from swarmd.swarm.run import PROFILES

    for name, profile in PROFILES.items():
        threshold = max(1, math.ceil(profile.proposers * 0.5))
        assert threshold >= 2, (
            f"profile {name!r} has {profile.proposers} proposers, so consensus "
            f"needs only {threshold} vote and degenerates into a union"
        )


# --- choosing how many agents to run ---------------------------------------


def test_an_explicit_agent_count_is_honoured_exactly():
    """Asking for 1000 used to give 192.

    HARD_POOL clamped each node's pool to 64 and nothing said so, which made
    the control a suggestion. The concern behind the clamp was concurrency, and
    concurrency is now bounded separately -- the population is what was asked
    for, MAX_IN_FLIGHT is how fast it moves.
    """
    from swarmd.swarm.planner import PlanNode, validate
    from swarmd.swarm.run import MAX_IN_FLIGHT

    plan = validate([PlanNode(name="only", instruction="produce x.json")])
    run = SwarmRun(ScriptedProvider(), profile="standard", agents=1000)
    assert run._pool_size(plan) == 1000
    assert run._pool_size(plan) > MAX_IN_FLIGHT


def test_a_profile_alone_stays_within_the_advisory_cap():
    """A profile must not silently become enormous; only an operator can."""
    from swarmd.swarm.planner import PlanNode, validate
    from swarmd.swarm.run import ADVISORY_POOL

    plan = validate([PlanNode(name="only", instruction="produce x.json")])
    run = SwarmRun(ScriptedProvider(), profile="deep")
    assert run._pool_size(plan) <= ADVISORY_POOL


def test_the_cost_estimate_scales_with_population_not_concurrency():
    """500 and 1000 agents quoting the same price is the opposite of
    informative, and that is what estimating against the in-flight bound did."""
    small = SwarmRun(ScriptedProvider(), profile="standard", agents=500)
    large = SwarmRun(ScriptedProvider(), profile="standard", agents=1000)
    assert large.estimated_calls() > small.estimated_calls() * 1.5


def test_profiles_fit_the_measured_daily_budget():
    """The mismatch that made `standard` unrunnable.

    It promised 500 agents and ~600 calls against a measured plannable budget
    of ~1,146 requests/day -- half a day of total capacity for one run, and the
    number appeared nowhere anyone could act on.
    """

    # A day's plannable budget, from docs/CAPACITY.md section 7.
    daily = 1_146
    for name in ("smoke", "standard", "eval"):
        run = SwarmRun(ScriptedProvider(), profile=name)
        assert run.estimated_calls() < daily * 0.2, (
            f"{name} costs more than a fifth of a day; it cannot be routine"
        )


async def test_preflight_reports_a_run_that_will_not_fit():
    """An operator asking for 1000 agents should learn the cost BEFORE paying
    it, not from a run that stops halfway."""
    from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal

    class Pool(ScriptedProvider):
        pass

    provider = Pool()
    provider.budget = BudgetTracker(
        journal=UsageJournal("nonexistent-for-this-test.jsonl"),
        budgets={"p": BudgetSpec(provider="p", kind="quota",
                                 limits=(Limit("day", requests=100),))},
    )
    run = SwarmRun(provider, profile="standard", agents=1000)
    report = run.preflight()

    assert report["fits"] is False
    assert report["shortfall"] > 0
    assert report["estimated_calls"] > report["remaining_today"]


async def test_preflight_reports_a_run_that_fits():
    from swarmd.router.budget import BudgetSpec, BudgetTracker, Limit, UsageJournal

    provider = ScriptedProvider()
    provider.budget = BudgetTracker(
        journal=UsageJournal("nonexistent-for-this-test.jsonl"),
        budgets={"p": BudgetSpec(provider="p", kind="quota",
                                 limits=(Limit("day", requests=10_000),))},
    )
    run = SwarmRun(provider, profile="smoke")
    assert run.preflight()["fits"] is True


def test_a_distilled_skill_records_an_approach_not_an_answer():
    """The library-poisoning bug the system was generating on its own.

    Distillation used to store the longest successful OUTPUT as the skill's
    instruction. Live, that captured `{"accuracy": 94.3, "baseline": 82.1}` and
    offered it to every future run as advice -- the specific answer to one task
    presented as a general method. A later run on different numbers would be
    handed the wrong ones and told they worked.
    """
    from swarmd.swarm.criteria import Candidate
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import WorkerResult

    run = SwarmRun(ScriptedProvider(), profile="smoke")
    node = PlanNode(name="extract", instruction="pull the numbers out")
    outcomes = [
        WorkerResult(
            agent_id=f"a{i}", node="extract", passed=True,
            candidate=Candidate(
                output='{"accuracy": 94.3}',
                artifacts={"accuracy": 94.3, "baseline": 82.1},
            ),
        )
        for i in range(2)
    ]

    instruction = run._distil_instruction("extract", node, outcomes)

    assert "94.3" not in instruction, "the answer leaked into the skill"
    assert "82.1" not in instruction
    assert "accuracy (float)" in instruction
    assert "pull the numbers out" in instruction
    # No node name: plan node names are regenerated every run, so advice
    # anchored to one describes a step the reader does not have.
    assert "extract" not in instruction.split("Produce")[0].replace(
        "pull the numbers out", ""
    )


def test_distillation_without_artifacts_describes_the_step_only():
    """Nothing structured to generalise from, so no method is invented."""
    from swarmd.swarm.criteria import Candidate
    from swarmd.swarm.planner import PlanNode
    from swarmd.swarm.worker import WorkerResult

    run = SwarmRun(ScriptedProvider(), profile="smoke")
    node = PlanNode(name="summarise", instruction="write one paragraph")
    outcomes = [
        WorkerResult(
            agent_id="a1", node="summarise", passed=True,
            candidate=Candidate(output="some prose"),
        )
    ]
    instruction = run._distil_instruction("summarise", node, outcomes)
    assert "write one paragraph" in instruction
    assert "JSON object" not in instruction
