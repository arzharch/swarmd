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
# What a worker actually sees: every number gone, structure kept. Serving is
# where redaction is applied, so a library written before that guard existed
# cannot leak one task's answer into a later task's prompt.
SERVED_ARTIFACT = '{"invalid": "<NUMBER>", "total": "<NUMBER>", "valid": "<NUMBER>"}'
# A literal that redaction does NOT remove, because it is not a quantity a
# reader could mistake for their own answer.
PATHED = '{"log": "/var/log/app.log", "verdict": "stale"}'


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
    assert served == SERVED_ARTIFACT


def test_an_exemplar_is_withheld_from_a_task_that_shares_a_literal() -> None:
    """The guard's remaining job, now that numbers never reach it.

    Redaction removes every quantity before this test runs, so a shared NUMBER
    can no longer trigger it -- and does not need to, because there is nothing
    numeric left to leak. What the guard still catches is the literal kinds
    redaction deliberately keeps: paths, URLs, quoted strings.
    """
    pathed = a_skill(exemplar=PATHED)
    assert pathed.exemplar_for("Why can nothing write to /var/log/app.log?") == ""
    assert pathed.exemplar_for("Why can nothing write to the audit sink?") != ""


def test_a_shared_number_no_longer_needs_the_guard() -> None:
    """Belt and braces, in that order: redaction is the defence, not the guard."""
    served = a_skill().exemplar_for("Given 24 items, how many are valid?")
    assert "24" not in served


def test_no_exemplar_means_no_claim() -> None:
    assert a_skill(exemplar="", exemplar_task="").exemplar_for("anything") == ""


def test_the_prompt_labels_an_example_as_another_task_s(tmp_path) -> None:
    block = skills_block([a_skill()], "How many ways can four books be shelved?")
    assert "A DIFFERENT task's step of this kind produced" in block
    assert "its keys and values are not yours" in block
    assert SERVED_ARTIFACT in block


def test_a_block_without_an_example_does_not_warn_about_one() -> None:
    block = skills_block([a_skill(exemplar="")], "any task")
    assert "A DIFFERENT task" not in block


def test_a_block_built_without_a_task_serves_no_example() -> None:
    """No task text means the leak guard cannot run, so nothing is served."""
    block = skills_block([a_skill()])
    assert ARTIFACT not in block
    assert SERVED_ARTIFACT not in block


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


class TestAnswersAreRedacted:
    """A worked example demonstrates a shape. A number in it is an answer.

    Found reviewing a real candidate: the library was ready to offer
    `{"count": 60, "reasoning": "There are 5 runners ... 5! = 120 ..."}` to any
    counting task. The incoming-task literal guard does not catch that -- a
    task about four books shares no literal with five runners -- so a worker
    asked to compute its own number would have been handed a confident 60.
    """

    def test_a_computed_answer_does_not_survive(self) -> None:
        from swarmd.swarm.generalise import redact_answers

        out = redact_answers(
            '{"count": 60, "reasoning": "There are 5 runners, 5! = 120 orders"}'
        )
        for answer in ("60", "5", "120"):
            assert answer not in out
        # ...and the METHOD does survive, which is the whole point.
        assert "runners" in out and "orders" in out

    def test_structure_without_numbers_is_untouched(self) -> None:
        from swarmd.swarm.generalise import redact_answers

        shape = '{"checkable": false, "missing_information": ["service_scope"]}'
        assert redact_answers(shape) == shape

    def test_a_boolean_verdict_is_not_a_quantity(self) -> None:
        from swarmd.swarm.generalise import redact_answers

        assert '"checkable": false' in redact_answers('{"checkable": false}')

    def test_nested_numbers_are_reached(self) -> None:
        from swarmd.swarm.generalise import redact_answers

        out = redact_answers('{"rows": [{"total": 42}], "note": "sum was 42"}')
        assert "42" not in out

    def test_text_that_is_not_json_is_still_redacted(self) -> None:
        from swarmd.swarm.generalise import redact_answers

        assert "17" not in redact_answers("the total came to 17 units")


def test_a_quoted_literal_is_compared_in_the_same_form_as_the_task() -> None:
    """The guard was inert for the only input it ever sees.

    `abstract`'s QUOTED slot captures a quoted span WITH its quotes, and every
    value in a JSON exemplar is quoted -- so `"/var/log/app.log"` never matched
    the bare `/var/log/app.log` in a task, and nothing was ever withheld.
    """
    pathed = a_skill(exemplar=PATHED)
    assert pathed.exemplar_for("Why can nothing write to /var/log/app.log?") == ""


def test_an_unparseable_exemplar_errs_toward_withholding() -> None:
    """Raw text is scanned as-is: more literals found, not fewer."""
    broken = a_skill(exemplar="not json at all: /var/log/app.log")
    assert broken.exemplar_for("Why can nothing write to /var/log/app.log?") == ""
