"""Consolidation and curriculum tests.

The guard that matters: a prompt change that lowers the CONTROL-arm score is
reverted. A change helping only the treatment arm has taught the system to
score better on its own benchmark, which is the opposite of improvement.
"""

from __future__ import annotations

import pytest

from swarmd.swarm.consolidate import (
    Consolidator,
    Curriculum,
    PromptHistory,
    distil_candidate,
)
from swarmd.swarm.skills import SkillLibrary


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "skills.json")


# --- the control-arm guard -------------------------------------------------


def test_a_prompt_change_that_regresses_the_control_is_reverted(library):
    """Better on its own benchmark, worse at the task, is not an improvement."""
    consolidator = Consolidator(library)
    consolidator.register_prompt("solve", "original prompt")

    kept = consolidator.apply_prompt_change(
        "solve", "clever new prompt", "supervisor suggestion",
        control_before=0.62, control_after=0.48,
    )
    assert kept is False
    assert consolidator.prompts["solve"].current.text == "original prompt"


def test_a_prompt_change_that_holds_the_control_is_kept(library):
    consolidator = Consolidator(library)
    consolidator.register_prompt("solve", "original")

    kept = consolidator.apply_prompt_change(
        "solve", "improved", "clearer instruction",
        control_before=0.60, control_after=0.61,
    )
    assert kept is True
    assert consolidator.prompts["solve"].current.text == "improved"


def test_any_control_regression_is_rejected_not_merely_a_large_one(library):
    """A tolerance above zero lets regressions accumulate one step at a time."""
    consolidator = Consolidator(library)
    consolidator.register_prompt("solve", "original")
    assert consolidator.apply_prompt_change(
        "solve", "x", "tiny change", control_before=0.5000, control_after=0.4999
    ) is False


def test_changing_an_unregistered_stage_raises(library):
    with pytest.raises(KeyError, match="unregistered"):
        Consolidator(library).apply_prompt_change(
            "ghost", "x", "y", control_before=1.0, control_after=1.0
        )


# --- prompt versioning -----------------------------------------------------


def test_prompt_history_starts_at_version_zero():
    history = PromptHistory("solve", "first")
    assert history.current.version == 0
    assert history.current.text == "first"


def test_rollback_appends_rather_than_deleting():
    """'We tried that and it regressed' is information worth keeping."""
    history = PromptHistory("solve", "v0")
    history.propose("v1", "a change")
    history.rollback()

    assert history.current.text == "v0"
    assert len(history.versions) == 3          # v0, v1, rollback
    assert any(v.text == "v1" for v in history.versions)


def test_rollback_can_target_a_specific_version():
    history = PromptHistory("solve", "v0")
    history.propose("v1", "one")
    history.propose("v2", "two")
    history.rollback(to=1)
    assert history.current.text == "v1"


def test_rolling_back_to_a_nonexistent_version_raises():
    with pytest.raises(IndexError):
        PromptHistory("solve", "v0").rollback(to=99)


def test_prompt_history_serialises_for_audit():
    history = PromptHistory("solve", "v0")
    history.propose("v1", "because the gate kept failing on formatting")
    payload = history.to_dict()
    assert payload["current_version"] == 1
    assert payload["history"][1]["rationale"].startswith("because")


# --- library consolidation -------------------------------------------------


def test_consolidation_prunes_skills_with_a_demonstrated_poor_record(library):
    skill = library.propose(name="bad", task_pattern="parse csv",
                            instruction="the wrong approach")
    library.approve(skill.skill_id, actor="reviewer")
    for _ in range(6):
        library.record_use(skill.skill_id, success=False)

    report = Consolidator(library).consolidate()
    assert report.pruned == [skill.skill_id]
    assert report.library_after == 0


def test_consolidation_spares_skills_with_thin_evidence(library):
    """Two failures may be two hard tasks rather than a bad skill."""
    skill = library.propose(name="ok", task_pattern="parse csv",
                            instruction="an approach")
    library.approve(skill.skill_id, actor="r")
    library.record_use(skill.skill_id, success=False)

    report = Consolidator(library).consolidate()
    assert report.pruned == []
    assert report.kept == 1


def test_consolidation_reports_before_and_after(library):
    for i in range(3):
        skill = library.propose(name=f"s{i}", task_pattern="p",
                                instruction=f"approach {i}")
        library.approve(skill.skill_id, actor="r")

    report = Consolidator(library).consolidate()
    assert report.library_before == 3
    assert report.library_after == 3


# --- curriculum ------------------------------------------------------------


def test_a_pass_rate_above_the_band_raises_difficulty():
    """Everything passing means nothing distinguishes good from lucky."""
    curriculum = Curriculum(difficulty=0.5)
    frontier = curriculum.observe([True] * 10)
    assert curriculum.difficulty > 0.5
    assert "too easy" in frontier.verdict


def test_a_pass_rate_below_the_band_lowers_difficulty():
    """Failures that teach nothing still burn quota."""
    curriculum = Curriculum(difficulty=0.5)
    frontier = curriculum.observe([False] * 10)
    assert curriculum.difficulty < 0.5
    assert "too hard" in frontier.verdict


def test_a_pass_rate_inside_the_band_holds():
    curriculum = Curriculum(difficulty=0.5)
    outcomes = [True] * 5 + [False] * 5     # 0.5, inside 0.4-0.7
    frontier = curriculum.observe(outcomes)
    assert curriculum.difficulty == 0.5
    assert "in band" in frontier.verdict


def test_the_curriculum_does_not_chase_noise():
    """Below the sample floor the pass rate is mostly noise."""
    curriculum = Curriculum(difficulty=0.5, min_samples=5)
    frontier = curriculum.observe([True, True])
    assert curriculum.difficulty == 0.5
    assert "insufficient" in frontier.verdict


def test_difficulty_is_bounded():
    curriculum = Curriculum(difficulty=0.95, step=0.1)
    for _ in range(10):
        curriculum.observe([True] * 10)
    assert curriculum.difficulty <= 1.0

    curriculum = Curriculum(difficulty=0.05, step=0.1)
    for _ in range(10):
        curriculum.observe([False] * 10)
    assert curriculum.difficulty >= 0.0


def test_task_selection_picks_the_nearest_difficulty_not_the_hardest():
    """Always picking harder slides into tasks that all fail, measuring nothing."""
    curriculum = Curriculum(difficulty=0.5)
    tasks = [("easy", 0.1), ("mid", 0.5), ("hard", 0.95)]
    ordered = curriculum.select(tasks, lambda t: t[1])
    assert ordered[0][0] == "mid"


def test_selecting_from_an_empty_list_is_safe():
    assert Curriculum().select([], lambda t: 0.0) == []


def test_the_curriculum_reports_its_adjustment_history():
    curriculum = Curriculum()
    curriculum.observe([True] * 10)
    curriculum.observe([False] * 10)
    report = curriculum.report()
    assert report["adjustments"] == 2
    assert len(report["history"]) == 2


# --- distillation ----------------------------------------------------------


def test_a_single_success_does_not_become_a_skill():
    """A skill distilled from one win is a superstition every run inherits."""
    assert distil_candidate(["one good output"], node="solve", task="t") is None


def test_repeated_successes_become_a_candidate():
    candidate = distil_candidate(
        ["a shorter output", "a considerably longer and more detailed output"],
        node="solve", task="t",
    )
    assert candidate is not None
    name, instruction = candidate
    assert name == "solve approach"
    assert "considerably longer" in instruction


def test_empty_outputs_are_not_evidence():
    assert distil_candidate(["", "   "], node="solve", task="t") is None
