"""Session tests: the loop only closes across runs.

The property that matters most is negative: a session must not produce
something that reads as an improvement claim. It produces the data; `swarmd
eval` makes the claim, and only against a paired control.
"""

from __future__ import annotations

import json

import pytest

from swarmd.hitl.approvals import ApprovalManager
from swarmd.hitl.stores import SqliteApprovalStore
from swarmd.swarm.consolidate import Curriculum
from swarmd.swarm.session import SessionReport, SessionRun, SwarmSession
from swarmd.swarm.skills import SkillLibrary


class FakeResult:
    def __init__(
        self, *, solved=True, attempts=1, status="completed", proposed=(),
        skill_used="",
    ):
        self.status = status
        self.duration_s = 0.4
        self.results = [
            type(
                "R", (),
                {
                    "passed": solved,
                    "attempts": attempts,
                    "node": "solve",
                    "skill_used": skill_used,
                },
            )()
        ]
        self.contained = []
        self.proposed_skills = list(proposed)


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "skills.json")


def _factory(solved=True, cost=0.001, proposed=(), attempts=1, skill_used=""):
    async def run_factory(task, index, system_prompt=""):
        return (
            FakeResult(
                solved=solved, attempts=attempts, proposed=proposed,
                skill_used=skill_used,
            ),
            {"cost": {"total_usd": cost}},
        )

    return run_factory


# --- the negative property -------------------------------------------------


async def test_a_session_produces_data_not_a_claim(library):
    """A rising line with nothing to compare against is the artifact this
    project exists to avoid producing."""
    report = await SwarmSession(_factory(), library).run(["t"] * 6)
    payload = report.to_dict()
    assert "claim" in payload
    assert "swarmd eval" in payload["claim"]
    assert "paired control" in payload["claim"]


async def test_the_ablation_state_travels_with_the_numbers(library):
    control = await SwarmSession(
        _factory(), library, skills_enabled=False
    ).run(["t"] * 3)
    assert control.to_dict()["skills_enabled"] is False
    assert "control" in control.render()


async def test_simulated_runs_are_marked_in_the_session_report(library):
    async def run_factory(task, index, system_prompt=""):
        return FakeResult(), {"cost": {"total_usd": 0.0, "simulated": True}}

    report = await SwarmSession(run_factory, library).run(["t"] * 3)
    assert report.to_dict()["simulated"] is True
    assert "not evidence" in report.render()


# --- windows ---------------------------------------------------------------


def test_windows_compare_thirds_rather_than_smearing_a_running_average():
    """The question is whether the last runs differ from the first."""
    report = SessionReport()
    report.runs = [
        SessionRun(
            index=i, task="t", status="completed", solved=i >= 6, cost_usd=0.001,
            duration_s=0.1, first_pass=True, skills_retrieved=0,
            skills_proposed=0, contained=0, difficulty=0.5,
        )
        for i in range(9)
    ]
    data = report.to_dict()
    assert data["first_third"]["success_rate"] == 0.0
    assert data["last_third"]["success_rate"] == 1.0


def test_a_window_with_no_solves_reports_no_cost_per_solved():
    """Dividing by zero solves would report infinite efficiency or crash."""
    report = SessionReport()
    report.runs = [
        SessionRun(
            index=0, task="t", status="error", solved=False, cost_usd=0.01,
            duration_s=0.1, first_pass=False, skills_retrieved=0,
            skills_proposed=0, contained=0, difficulty=0.5,
        )
    ]
    assert report.window(0, 1)["cost_per_solved"] is None


def test_an_empty_session_renders_without_crashing():
    assert "0 runs" in SessionReport().render()


async def test_the_report_is_json_serialisable(library):
    report = await SwarmSession(_factory(), library).run(["t"] * 3)
    json.dumps(report.to_dict())


# --- between-run learning --------------------------------------------------


async def test_consolidation_runs_between_runs_not_during_one(library):
    """Mid-run consolidation would grade two agents in the same run against
    different starting conditions."""
    session = SwarmSession(_factory(), library, consolidate_every=2)
    report = await session.run(["t"] * 6)
    assert [c["after_run"] for c in report.consolidations] == [1, 3, 5]


async def test_the_curriculum_observes_between_consolidations(library):
    session = SwarmSession(
        _factory(solved=True), library, consolidate_every=3,
        curriculum=Curriculum(min_samples=2, difficulty=0.5),
    )
    report = await session.run(["t"] * 6)
    assert report.frontier
    # Everything solved: too easy, so difficulty should have risen.
    assert session.curriculum.difficulty > 0.5


async def test_skill_retrieval_is_counted_only_in_the_treatment_arm(library):
    treatment = await SwarmSession(
        _factory(skill_used="abc123"), library
    ).run(["parse csv files"] * 2)
    control = await SwarmSession(
        _factory(skill_used="abc123"), library, skills_enabled=False
    ).run(["parse csv files"] * 2)

    assert treatment.runs[0].skills_retrieved == 1
    assert control.runs[0].skills_retrieved == 0


# --- the human gate --------------------------------------------------------


async def test_distilled_skills_stay_unusable_without_approval(library, tmp_path):
    """The default: a session cannot approve its own skills."""
    approvals = ApprovalManager(SqliteApprovalStore(tmp_path / "a.db"))
    session = SwarmSession(_factory(), library, approvals=approvals)

    library.propose(name="n", task_pattern="p", instruction="use json.loads on the reply")
    await session.run(["t"] * 2)
    assert library.approved() == []


async def test_auto_approve_is_off_by_default(library):
    assert SwarmSession(_factory(), library).auto_approve is False


async def test_auto_approve_records_the_bypass_in_the_audit_trail(
    library, tmp_path
):
    """A bypassed gate that looks like review is worse than an obvious one."""
    approvals = ApprovalManager(SqliteApprovalStore(tmp_path / "a.db"))
    from swarmd.hitl.skill_gate import SkillGate

    gate = SkillGate(approvals, library)
    skill, _ = await gate.submit(
        name="n", task_pattern="p", instruction="use json.loads on the reply", run_id="r", evidence=2
    )

    session = SwarmSession(
        _factory(), library, approvals=approvals, auto_approve=True
    )
    await session.run(["t"])

    assert library.get(skill.skill_id).usable
    assert library.get(skill.skill_id).approved_by == "auto-approve"
    audit = await approvals.audit()
    assert any(e.actor == "auto-approve" for e in audit)


async def test_auto_approve_works_without_an_approval_store(library):
    """Development convenience must not require infrastructure."""
    library.propose(name="n", task_pattern="p", instruction="use json.loads on the reply")
    session = SwarmSession(_factory(), library, auto_approve=True)
    await session.run(["t"])
    assert len(library.approved()) == 1


async def test_auto_approve_marks_a_thin_candidate_it_pushes_through(library):
    """With no human in the loop, MIN_DISTINCT_TASKS can only be met or
    bypassed -- there is no reviewer to wait on a second task shape for.
    `--auto-approve` bypassing is already documented and visible via the
    `auto-approve` actor; this is the SAME bypass now also visible on the
    skill's own record when the candidate was genuinely short of evidence,
    which the actor alone does not say."""
    skill = library.propose(
        name="n", task_pattern="p", instruction="use json.loads on the reply",
        evidence_task="only-shape",
    )
    assert not skill.promotable

    session = SwarmSession(_factory(), library, auto_approve=True)
    await session.run(["t"])

    approved = library.get(skill.skill_id)
    assert approved.usable
    assert "bypassed" in approved.approval_note


# --- outcomes --------------------------------------------------------------


async def test_a_run_is_solved_only_when_every_node_passed(library):
    report = await SwarmSession(_factory(solved=False), library).run(["t"] * 3)
    assert all(not r.solved for r in report.runs)


async def test_repair_rounds_show_in_the_first_pass_rate(library):
    report = await SwarmSession(_factory(attempts=3), library).run(["t"] * 3)
    assert report.to_dict()["first_third"]["first_pass_rate"] == 0.0


async def test_skills_used_counts_what_workers_actually_retrieved(library):
    """Not a separate pre-run query.

    An earlier version queried the library with the task text while workers
    retrieve per node instruction, so the session reported 0 while skills were
    in use. A reported figure that does not match the run is worse than none.
    """
    async def run_factory(task, index, system_prompt=""):
        result = FakeResult()
        result.results = [
            type("R", (), {"passed": True, "attempts": 1, "node": "solve",
                           "skill_used": "abc123"})(),
            type("R", (), {"passed": True, "attempts": 1, "node": "solve",
                           "skill_used": "abc123"})(),
            type("R", (), {"passed": True, "attempts": 1, "node": "check",
                           "skill_used": "def456"})(),
        ]
        return result, {"cost": {"total_usd": 0.0}}

    report = await SwarmSession(run_factory, library).run(["t"])
    # Two distinct skills across three workers.
    assert report.runs[0].skills_retrieved == 2


async def test_the_control_arm_reports_no_skill_use_even_if_workers_had_some(library):
    async def run_factory(task, index, system_prompt=""):
        result = FakeResult()
        result.results = [
            type("R", (), {"passed": True, "attempts": 1, "node": "solve",
                           "skill_used": "abc123"})()
        ]
        return result, {"cost": {"total_usd": 0.0}}

    report = await SwarmSession(
        run_factory, library, skills_enabled=False
    ).run(["t"])
    assert report.runs[0].skills_retrieved == 0
