"""The gate on what the system is allowed to learn.

A skill entering the library is inherited by every future run, so this is the
most consequential human decision in the system. The property under test is
that the decision and its effect cannot diverge: the audit trail must never say
approved while the skill stays unusable, and vice versa.
"""

from __future__ import annotations

import asyncio
import subprocess
import sys

import pytest

from swarmd.hitl.approvals import ApprovalManager, ApprovalState
from swarmd.hitl.skill_gate import STAGE, SkillAlreadyApproved, SkillGate
from swarmd.hitl.stores import SqliteApprovalStore
from swarmd.swarm.skills import SkillLibrary


# The CLI loads `.env` so a fresh checkout finds its provider keys. That file
# also carries DATABASE_URL, which would send these subprocesses to Postgres
# instead of the SQLite store the fixture just populated. An explicit empty
# value wins over the file -- the loader never overrides what the environment
# already says -- which is the same escape hatch a developer gets.
def _cli_env():
    import os
    return {**os.environ, "DATABASE_URL": "", "SWARMD_SIMULATED_PROVIDER": "true"}


@pytest.fixture
def gate(tmp_path):
    return SkillGate(
        ApprovalManager(SqliteApprovalStore(tmp_path / "approvals.db")),
        SkillLibrary(tmp_path / "skills.json"),
    )


async def _submit(gate, name="csv parsing", instruction="use csv.DictReader"):
    return await gate.submit(
        name=name,
        task_pattern="parse csv files",
        instruction=instruction,
        run_id="run-1",
        criterion_hash="abc123",
        evidence=3,
    )


# --- one queue, one decision -----------------------------------------------


async def test_a_candidate_skill_lands_in_the_shared_approval_queue(gate):
    """Two review queues means one of them stops being read."""
    skill, request = await _submit(gate)
    assert request.stage == STAGE

    pending = await gate.approvals.pending()
    assert [r.request_id for r in pending] == [request.request_id]
    assert not skill.usable


async def test_the_reviewer_can_see_what_they_are_approving(gate):
    """A reference they have to go look up is a reference nobody looks up."""
    _, request = await _submit(gate)
    assert request.item["instruction"].startswith("use csv")
    assert request.item["name"] == "csv parsing"
    assert request.item["verified_successes"] == 3


async def test_provenance_travels_with_the_request(gate):
    """A poisoned skill has to be traceable back to the run that produced it."""
    _, request = await _submit(gate)
    assert request.item["provenance_run"] == "run-1"
    assert request.item["provenance_criterion"] == "abc123"


async def test_approving_makes_the_skill_usable(gate):
    skill, request = await _submit(gate)
    decision = await gate.decide(request.request_id, "approve", actor="reviewer")

    assert decision.applied
    assert decision.request.state is ApprovalState.APPROVED
    assert gate.library.get(skill.skill_id).usable
    assert gate.library.get(skill.skill_id).approved_by == "reviewer"


async def test_rejecting_retires_the_skill(gate):
    skill, request = await _submit(gate)
    decision = await gate.decide(request.request_id, "reject", actor="reviewer")

    assert decision.applied
    assert not gate.library.get(skill.skill_id).usable
    assert gate.library.get(skill.skill_id).retired


async def test_a_decided_skill_leaves_the_queue(gate):
    _, request = await _submit(gate)
    await gate.decide(request.request_id, "approve", actor="r")
    assert await gate.pending() == []


async def test_the_decision_is_recorded_before_it_is_applied(gate):
    """A decision that happened but left no record is unrecoverable; one
    recorded and not yet applied can be re-run."""
    import inspect

    source = inspect.getsource(SkillGate.decide)
    record_at = source.index("self.approvals.decide")
    apply_at = source.index("self.library.approve")
    assert record_at < apply_at


async def test_the_audit_trail_records_who_decided(gate):
    _, request = await _submit(gate)
    await gate.decide(request.request_id, "approve", actor="reviewer-7")

    audit = await gate.approvals.audit()
    assert [(e.action, e.actor) for e in audit] == [
        ("submit", "system"),
        ("approve", "reviewer-7"),
    ]


async def test_decisions_are_immutable(gate):
    _, request = await _submit(gate)
    await gate.decide(request.request_id, "approve", actor="a")
    with pytest.raises(ValueError, match="already decided"):
        await gate.decide(request.request_id, "reject", actor="b")


async def test_an_already_approved_skill_is_not_requeued(gate):
    """Content addressing means an identical instruction is the same skill;
    re-queueing asks a reviewer to decide something already decided."""
    _, request = await _submit(gate)
    await gate.decide(request.request_id, "approve", actor="r")

    with pytest.raises(SkillAlreadyApproved):
        await _submit(gate)


# --- deduping the queue -----------------------------------------------------


async def test_a_second_pending_submission_reuses_the_first_request(gate):
    """Distillation re-proposes the same skill from every task that produces
    it. Without this, a third distinct task shape submitted a second pending
    request for a skill the second shape had already queued -- two requests
    for one skill_id, only one of which a reviewer's decision can apply to."""
    _, first = await _submit(gate)
    _, second = await _submit(gate)

    assert second.request_id == first.request_id
    pending = await gate.approvals.pending()
    assert [r.request_id for r in pending] == [first.request_id]


async def test_deciding_a_stale_duplicate_is_a_no_op(gate):
    """THE BUG THIS FIXES. Two pending requests for one skill_id -- queued
    before the dedupe above shipped, or written to the approval store
    directly -- used to let a decision on the SECOND un-approve and RETIRE a
    skill the FIRST had already approved. The audit trail must still record
    that the second request was decided; the library must not move twice."""
    skill, first = await _submit(gate)
    # Simulate a duplicate that predates dedupe-on-submit: a second pending
    # request for the same skill_id, written straight to the approval store.
    second = await gate.approvals.submit(dict(first.item), stage=STAGE)

    approved = await gate.decide(first.request_id, "approve", actor="reviewer")
    assert approved.applied
    assert gate.library.get(skill.skill_id).usable

    stale = await gate.decide(second.request_id, "reject", actor="late-reviewer")
    assert not stale.applied
    assert "stale duplicate" in stale.detail

    # The skill is exactly as the first decision left it: approved, usable,
    # not retired -- the second decision left no mark on the library at all.
    still = gate.library.get(skill.skill_id)
    assert still.usable
    assert not still.retired

    # The audit trail still shows both decisions were made.
    audit = await gate.approvals.audit()
    assert [e.action for e in audit] == ["submit", "submit", "approve", "reject"]


async def test_deciding_approve_forwards_force_to_the_library(gate):
    """`decide` is the human-approval path's only route to `library.approve`;
    an operator's explicit override has to reach it or the CLI flag that sets
    it is a no-op. Two skills because a decided request is immutable -- a
    refused decision cannot be retried with `force` on the same request."""
    short, refused_request = await _submit(gate, name="short a", instruction="do the short thing a")
    gate.library.record_evidence(short.skill_id, "only-shape")
    assert not gate.library.get(short.skill_id).promotable

    refused = await gate.decide(refused_request.request_id, "approve", actor="r")
    assert not refused.applied
    assert not gate.library.get(short.skill_id).usable

    thin, forced_request = await _submit(gate, name="short b", instruction="do the short thing b")
    gate.library.record_evidence(thin.skill_id, "only-shape")
    assert not gate.library.get(thin.skill_id).promotable

    forced = await gate.decide(
        forced_request.request_id, "approve", actor="reviewer", force=True
    )
    assert forced.applied
    assert gate.library.get(thin.skill_id).usable
    assert "bypassed" in gate.library.get(thin.skill_id).approval_note


async def test_the_queue_survives_a_process_boundary(tmp_path):
    """The whole reason for using the durable store rather than a JSON list."""
    approvals = tmp_path / "approvals.db"
    library = tmp_path / "skills.json"

    gate_a = SkillGate(
        ApprovalManager(SqliteApprovalStore(approvals)), SkillLibrary(library)
    )
    skill, request = await _submit(gate_a)
    del gate_a

    gate_b = SkillGate(
        ApprovalManager(SqliteApprovalStore(approvals)), SkillLibrary(library)
    )
    assert [r.request_id for r in await gate_b.pending()] == [request.request_id]

    await gate_b.decide(request.request_id, "approve", actor="later-reviewer")
    assert SkillLibrary(library).get(skill.skill_id).usable


async def test_the_summary_reports_how_long_a_skill_has_waited(gate):
    await _submit(gate)
    summary = await gate.summary()
    assert summary["awaiting_review"] == 1
    assert summary["oldest_waiting_s"] >= 0


async def test_only_skill_requests_are_returned_as_pending(gate):
    """Other stages have their own reviewers."""
    await _submit(gate)
    await gate.approvals.submit({"draft": "an email"}, stage="outreach")
    assert len(await gate.pending()) == 1
    assert len(await gate.approvals.pending()) == 2


# --- the CLI path ----------------------------------------------------------


def test_approving_a_skill_from_the_cli_applies_it(tmp_path, monkeypatch):
    """A decision recorded without reaching the library is a divergence."""
    approvals = tmp_path / "approvals.db"
    library_path = tmp_path / "skills.json"
    monkeypatch.setenv("SWARMD_APPROVALS_DB", str(approvals))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    gate = SkillGate(
        ApprovalManager(SqliteApprovalStore(approvals)), SkillLibrary(library_path)
    )
    skill, request = asyncio.run(_submit(gate))

    result = subprocess.run(
        [
            sys.executable, "-m", "swarmd.cli", "approve", request.request_id,
            "--actor", "cli-reviewer", "--skills", str(library_path),
        ],
        capture_output=True, text=True, check=False, timeout=60,
        env=_cli_env(),
    )
    assert result.returncode == 0, result.stderr
    assert "approved into the library" in result.stdout
    assert SkillLibrary(library_path).get(skill.skill_id).usable


def test_the_cli_force_flag_reaches_the_library(tmp_path, monkeypatch):
    """The escape has to work for the actual operator, not just the library
    call under it -- a `--force` that only the internal API understands is
    not an escape a reviewer can use."""
    approvals = tmp_path / "approvals.db"
    library_path = tmp_path / "skills.json"
    monkeypatch.setenv("SWARMD_APPROVALS_DB", str(approvals))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    library = SkillLibrary(library_path)
    library.propose(
        name="csv parsing", task_pattern="parse csv files",
        instruction="use csv.DictReader", evidence_task="only-shape",
    )
    gate = SkillGate(ApprovalManager(SqliteApprovalStore(approvals)), library)
    skill, request = asyncio.run(_submit(gate))
    assert not skill.promotable

    without_force = subprocess.run(
        [
            sys.executable, "-m", "swarmd.cli", "approve", request.request_id,
            "--actor", "cli-reviewer", "--skills", str(library_path),
        ],
        capture_output=True, text=True, check=False, timeout=60,
        env=_cli_env(),
    )
    assert without_force.returncode == 1
    assert not SkillLibrary(library_path).get(skill.skill_id).usable

    # decisions are immutable, so the refused decision needs a fresh request
    library.propose(
        name="csv parsing 2", task_pattern="parse csv files",
        instruction="use csv.DictReader v2", evidence_task="only-shape",
    )
    skill2, request2 = asyncio.run(
        _submit(gate, name="csv parsing 2", instruction="use csv.DictReader v2")
    )
    with_force = subprocess.run(
        [
            sys.executable, "-m", "swarmd.cli", "approve", request2.request_id,
            "--actor", "cli-reviewer", "--skills", str(library_path), "--force",
        ],
        capture_output=True, text=True, check=False, timeout=60,
        env=_cli_env(),
    )
    assert with_force.returncode == 0, with_force.stderr
    approved = SkillLibrary(library_path).get(skill2.skill_id)
    assert approved.usable
    assert "bypassed" in approved.approval_note


def test_approving_a_skill_without_the_library_warns_about_divergence(
    tmp_path, monkeypatch
):
    """Silence here would leave the trail saying approved and the skill unusable."""
    approvals = tmp_path / "approvals.db"
    monkeypatch.setenv("SWARMD_APPROVALS_DB", str(approvals))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    gate = SkillGate(
        ApprovalManager(SqliteApprovalStore(approvals)),
        SkillLibrary(tmp_path / "real.json"),
    )
    _, request = asyncio.run(_submit(gate))

    result = subprocess.run(
        [sys.executable, "-m", "swarmd.cli", "approve", request.request_id],
        capture_output=True, text=True, check=False, timeout=60,
        env=_cli_env(),
    )
    # No --skills: the gate builds an empty in-memory library, cannot find the
    # skill, and must say so rather than reporting success.
    assert "out of sync" in result.stdout or "not applied" in result.stdout
    assert result.returncode == 1


def test_queued_skills_show_up_in_the_shared_list_command(tmp_path, monkeypatch):
    monkeypatch.setenv("SWARMD_APPROVALS_DB", str(tmp_path / "a.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)

    gate = SkillGate(
        ApprovalManager(SqliteApprovalStore(tmp_path / "a.db")),
        SkillLibrary(tmp_path / "s.json"),
    )
    _, request = asyncio.run(_submit(gate))

    result = subprocess.run(
        [sys.executable, "-m", "swarmd.cli", "list"],
        capture_output=True, text=True, check=False, timeout=60,
        env=_cli_env(),
    )
    assert request.request_id in result.stdout
    assert "skill" in result.stdout
