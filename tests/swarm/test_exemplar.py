"""Worked examples: the unit of learning that has a contract.

ADR-017. Prose distilled from one task was measured not to transfer -- 49
records over a 24-task corpus gave six approaches past the evidence bar, five
corroborating to a single generic word. Every published system that reports
transferable skills stores something checkable instead: executable code
(Voyager), an abstracted action template (Agent Workflow Memory), or the
successful trajectory as a few-shot example (ExpeL). This is the third, with
the leak guard swarmd's own rules require.
"""

from __future__ import annotations

from swarmd.swarm.skills import MAX_EXEMPLAR_CHARS, Skill, SkillLibrary
from swarmd.swarm.worker import skills_block

ARTIFACT = '{"invalid": 6, "total": 24, "valid": 18}'


def a_skill(**over) -> Skill:
    base = {
        "skill_id": "s1",
        "name": "approach: produce count, reasoning",
        "task_pattern": "count slot_number slot_term",
        "instruction": "When a step calls for this: separate valid from invalid.",
        "exemplar": ARTIFACT,
        "exemplar_task": "shape-a",
    }
    base.update(over)
    return Skill(**base)  # type: ignore[arg-type]


def test_an_exemplar_is_served_to_a_task_that_shares_no_literal() -> None:
    served = a_skill().exemplar_for("How many ways can four books be shelved?")
    assert served == ARTIFACT


def test_an_exemplar_is_withheld_from_a_task_that_shares_a_literal() -> None:
    """The case the no-literal rule was written for: a worker handed values
    that look like the ones it is supposed to derive."""
    assert a_skill().exemplar_for("Given 24 items, how many are valid?") == ""


def test_no_exemplar_means_no_claim() -> None:
    assert a_skill(exemplar="", exemplar_task="").exemplar_for("anything") == ""


def test_the_prompt_labels_an_example_as_another_task_s(tmp_path) -> None:
    block = skills_block([a_skill()], "How many ways can four books be shelved?")
    assert "A DIFFERENT task's step of this kind produced" in block
    assert "its keys and values are not yours" in block
    assert ARTIFACT in block


def test_a_block_without_an_example_does_not_warn_about_one() -> None:
    block = skills_block([a_skill(exemplar="")], "any task")
    assert "A DIFFERENT task" not in block


def test_a_block_built_without_a_task_serves_no_example() -> None:
    """No task text means the leak guard cannot run, so nothing is served."""
    block = skills_block([a_skill()])
    assert ARTIFACT not in block


def test_the_first_exemplar_is_the_one_that_stays(tmp_path) -> None:
    """A skill's exemplar is part of what a human approved."""
    library = SkillLibrary(tmp_path / "skills.json")
    library.propose(
        name="approach: produce count, reasoning",
        task_pattern="count slot_number slot_term",
        instruction="When a step calls for this: separate valid from invalid.",
        evidence_task="shape-a",
        exemplar=ARTIFACT,
    )
    second = library.propose(
        name="approach: produce count, reasoning",
        task_pattern="count slot_number slot_term",
        instruction="When a step calls for this: count what the rule allows.",
        evidence_task="shape-b",
        exemplar='{"other": 1}',
    )
    assert second.exemplar == ARTIFACT
    assert second.exemplar_task == "shape-a"


def test_an_exemplar_is_bounded(tmp_path) -> None:
    library = SkillLibrary(tmp_path / "skills.json")
    skill = library.propose(
        name="approach: produce count, reasoning",
        task_pattern="count slot_number slot_term",
        instruction="When a step calls for this: separate valid from invalid.",
        evidence_task="shape-a",
        exemplar="x" * (MAX_EXEMPLAR_CHARS * 3),
    )
    assert len(skill.exemplar) == MAX_EXEMPLAR_CHARS


def test_the_reviewer_is_shown_the_worked_example(tmp_path) -> None:
    """The gate decides what a human sees, and the example is the evidence.

    Corroborated prose reduces to a couple of generic words. A reviewer asked
    "does this approach transfer?" needs the artifact, or they are approving a
    word list.
    """
    import asyncio

    from swarmd.hitl.approvals import ApprovalManager
    from swarmd.hitl.skill_gate import SkillGate
    from swarmd.hitl.stores import build_approval_store

    library = SkillLibrary(tmp_path / "skills.json")
    library.propose(
        name="approach: produce count, reasoning",
        task_pattern="count slot_number slot_term",
        instruction="When a step calls for this: separate valid from invalid.",
        evidence_task="shape-a",
        exemplar=ARTIFACT,
    )
    gate = SkillGate(
        ApprovalManager(build_approval_store(path=tmp_path / "approvals.db")), library
    )

    async def go() -> str:
        _, request = await gate.submit(
            name="approach: produce count, reasoning",
            task_pattern="count slot_number slot_term",
            instruction="When a step calls for this: separate valid from invalid.",
        )
        return str(request.item.get("exemplar", ""))

    assert asyncio.run(go()) == ARTIFACT
