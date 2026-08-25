"""Tests for the Supervisor: patch proposal, versioned apply, rollback, measurement."""

from examples.leadops.supervisor import Supervisor


def test_intervention_only_on_clustered_failures() -> None:
    s = Supervisor()
    assert not s.should_intervene({"schema": 1, "range": 1})
    assert s.should_intervene({"schema": 3})


def test_propose_and_apply_patch_versioned() -> None:
    s = Supervisor()
    s.register_stage("draft", "You write short B2B emails.")

    patch = s.propose_patch("draft", ["banned term 'guarantee'", "banned term 'act now'"])
    assert patch is not None
    old_prompt = s.stage_prompts["draft"]
    s.apply(patch)

    assert len(s.patches) == 1
    assert s.stage_prompts["draft"] != old_prompt
    assert "guarantee" in s.stage_prompts["draft"] or "banned" in s.stage_prompts["draft"]
    assert s.interventions == 1


def test_rollback_restores_previous_prompt() -> None:
    s = Supervisor()
    s.register_stage("draft", "original")
    patch = s.propose_patch("draft", ["banned term x"])
    assert patch is not None
    s.apply(patch)
    patched_prompt = s.stage_prompts["draft"]

    rolled_back = s.rollback(patch.id)
    assert rolled_back is not None
    assert s.stage_prompts["draft"] == "original"
    assert patched_prompt != "original"


def test_rollback_unknown_id_returns_none() -> None:
    s = Supervisor()
    assert s.rollback("ghost") is None


def test_effectiveness_measurement() -> None:
    s = Supervisor()
    s.register_stage("score", "score leads")
    patch = s.propose_patch("score", ["outside range"])
    assert patch is not None
    s.apply(patch)

    assert s.measure(patch.id, pass_rate_before=0.5, pass_rate_after=0.9)
    assert patch.effective is True

    # A second patch that doesn't help gets marked ineffective.
    p2 = s.propose_patch("score", ["other failure"])
    assert p2 is not None
    s.apply(p2)
    assert not s.measure(p2.id, pass_rate_before=0.9, pass_rate_after=0.85)
    assert p2.effective is False


def test_report_shape() -> None:
    s = Supervisor()
    s.register_stage("qa", "check drafts")
    patch = s.propose_patch("qa", ["missing subject"])
    assert patch is not None
    s.apply(patch)
    rep = s.report()
    assert rep["interventions"] == 1
    assert rep["patches"][0]["stage"] == "qa"
