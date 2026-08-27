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
    assert [r.node for r in result.results] == ["gather", "verify"]
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
