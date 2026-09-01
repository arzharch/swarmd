"""Does what the system learns TRANSFER, or has it just memorised a task?

The library used to answer "no" twice over, in two places that looked
different and were the same mistake:

  THE ADVICE   `_distil_instruction` kept `plan_node.instruction` verbatim, so
               a stored skill read "Compute the total cost of 3 pens at 1.25
               dollars each" -- one task's arithmetic in a method's grammar.
  THE INDEX    the same task text was stored as `task_pattern`, so a skill was
               retrieved because a later task LOOKED LIKE the one it came
               from. Even with perfect advice, that channel alone reproduces
               the bug: the library would still be matching on memorised text.

  THE EVIDENCE and the bar for offering advice to a human was "two agents
               passed the same node of the same run" -- one observation drawn
               twice, sharing a task, a criterion and a prompt. No number of
               redraws from one task says anything about a second one.

The property under test is the one the feature is named for: a skill learned
from pens is offered to a task about pencils, and carries none of the pens.
"""

from __future__ import annotations

import json

import pytest

from swarmd.hitl.approvals import ApprovalManager, InMemoryApprovalStore
from swarmd.router.providers import LLMResponse
from swarmd.swarm.generalise import task_signature
from swarmd.swarm.planner import PlanNode
from swarmd.swarm.run import SwarmRun
from swarmd.swarm.skills import (
    MIN_DISTINCT_TASKS,
    PRUNE_MIN_USES,
    Skill,
    SkillLibrary,
    SkillLibraryError,
    make_skill_id,
    validate_instruction,
)

PEN_TASK = "Compute the total cost of 3 pens at 1.25 dollars each"
PENCIL_TASK = "Compute the total cost of 7 pencils at 2.50 dollars each"
# Same question, new digits. The system must count this as the SAME task.
PEN_AGAIN = "Compute the total cost of 9 pens at 4.75 dollars each"
# Same question, same digits, politely. The system must count this as the SAME
# task too -- and keyed on the abstracted sentence it did not.
PEN_POLITELY = "Please compute the total cost of 3 pens at 1.25 dollars each for me"

CRITERION = {
    "description": "the step emits the computed cost as a JSON object",
    "checks": [
        {
            "kind": "json_parses",
            "params": {"required_keys": ["total_cost", "unit_price"]},
        },
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
    ],
}

OUTPUT = json.dumps(
    {
        "total_cost": 3.75,
        "unit_price": 1.25,
        "method": "multiplied the quantity by the unit price stated in the task",
    }
)


def plan_for(node: str, instruction: str) -> dict[str, object]:
    return {
        "rationale": "one arithmetic step",
        "nodes": [{"name": node, "instruction": instruction, "depends_on": []}],
    }


class ScriptedProvider:
    """Answers by prompt shape. No network, fully deterministic.

    The plan it returns carries the task's literals in the node instruction,
    which is exactly the shape that produced the bug: the planner writes the
    step by restating the question.
    """

    name = "scripted"

    def __init__(self, plan: dict[str, object]) -> None:
        self.plan = plan
        self.calls = 0

    async def complete(self, request):  # type: ignore[no-untyped-def]
        self.calls += 1
        if "matching this schema" in request.prompt and "checks" in request.prompt:
            text = json.dumps(CRITERION)
        elif "matching this schema" in request.prompt:
            text = json.dumps(self.plan)
        else:
            text = OUTPUT
        return LLMResponse(
            text=text, provider=self.name, model="scripted-v1",
            latency_s=0.001, tokens_in=10, tokens_out=20,
        )


async def distil(
    library: SkillLibrary,
    task: str,
    node: str,
    instruction: str,
    approvals: ApprovalManager | None = None,
) -> Skill:
    """Run one task end to end and return the single skill it distilled.

    `approvals` is optional because most of these tests read the library
    directly, but it is the only way to observe the bar that matters: whether a
    HUMAN was asked. `library.promotable()` is a proxy for that; the approval
    queue is the thing itself.
    """
    run = SwarmRun(
        ScriptedProvider(plan_for(node, instruction)),
        profile="smoke",
        skills=library,
        approvals=approvals,
    )
    result = await run.run(task)
    assert result.status == "completed", result.status
    assert result.proposed_skills, "a pool of verified successes proposed nothing"
    skill = library.get(result.proposed_skills[0])
    assert skill is not None
    return skill


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "skills.json")


# --- the transfer property ---------------------------------------------------


async def test_a_skill_learned_from_pens_carries_none_of_the_pens(library):
    """THE ONE THAT HAPPENED, end to end.

    The stored advice must not contain the quantity, the price, or the thing
    being counted. A later run about pencils that reads "3 pens at 1.25" is
    being handed another task's answer and told it worked -- which is worse
    than no advice, because it is confident.
    """
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)

    assert "3" not in skill.instruction
    assert "1.25" not in skill.instruction
    assert "pens" not in skill.instruction
    # And what SHOULD survive: the method.
    assert "total cost" in skill.instruction

    # THE KEY NAMES DO NOT SURVIVE, which reverses what this test used to
    # assert. `total_cost` and `unit_price` are the keys ONE task's worker
    # chose; measured on a real library, 17 of 31 live records carried key
    # names taken straight from their source task -- a larger leak than any
    # other. They are redundant as well as harmful: the worker is shown its own
    # frozen criterion's exact requirements, so it already knows which keys it
    # must produce, and a skill naming different ones can only agree by
    # accident. What transfers is that the work produced a structured object,
    # and of what kinds of value.
    assert "total_cost" not in skill.instruction
    assert "unit_price" not in skill.instruction
    assert "float" in skill.instruction


async def test_a_skill_learned_from_pens_is_retrieved_for_pencils(library):
    """The whole point. Different noun, different quantity, different currency
    notation -- the same kind of question, so the same approach applies.

    This is the assertion the old index could not pass: matching on task text,
    "7 pencils at 40c each" shares almost nothing with "3 pens at 1.25 dollars
    each", and the skill that solves it would never have been offered.
    """
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    # One distillation is one evidence shape, short of MIN_DISTINCT_TASKS;
    # these tests are about retrieval, not the evidence bar, so force past it
    # the way a reviewer who has actually looked at the candidate would.
    library.approve(skill.skill_id, actor="reviewer", force=True)

    hits = library.retrieve("7 pencils at 40c each")
    assert [s.skill_id for s in hits] == [skill.skill_id]


async def test_an_unrelated_task_still_retrieves_nothing(library):
    """Without this the transfer test above proves only that retrieval got
    looser. A wrong skill actively misleads a worker; no skill just leaves it
    to reason from the task."""
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    # One distillation is one evidence shape, short of MIN_DISTINCT_TASKS;
    # these tests are about retrieval, not the evidence bar, so force past it
    # the way a reviewer who has actually looked at the candidate would.
    library.approve(skill.skill_id, actor="reviewer", force=True)

    assert library.retrieve("compose a haiku about volcanoes") == []


async def test_a_task_that_only_shares_generic_words_retrieves_nothing(library):
    """The near miss, which a haiku does not test.

    A haiku shares no vocabulary at all, so passing that says only that
    retrieval is not returning everything. These two share the verb "compute"
    and the fact that a number appears somewhere -- and on that alone, both
    were offered the pen skill. Neither is a unit-price question: one has no
    price in it and the other measures distance, so the approach cannot apply,
    and a confident wrong skill misleads a worker where no skill would have
    left it to reason from the task.

    What separates them from pencils is the shape, not the words: the stored
    pattern is about a quantity AND a price, and only the pencils task carries
    both.
    """
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    # One distillation is one evidence shape, short of MIN_DISTINCT_TASKS;
    # these tests are about retrieval, not the evidence bar, so force past it
    # the way a reviewer who has actually looked at the candidate would.
    library.approve(skill.skill_id, actor="reviewer", force=True)

    assert library.retrieve("Compute the average rainfall in millimetres for 12 cities") == []
    assert library.retrieve("Compute the total distance of 5 marathons at 42.2 km each") == []
    # And the boundary in the other direction, so this is a precision rule and
    # not a mute button: another unit-price question still matches.
    hits = library.retrieve("Compute the total cost of 12 notebooks at 3.40 dollars each")
    assert [s.skill_id for s in hits] == [skill.skill_id]


async def test_the_retrieval_index_holds_no_literal_from_the_task(library):
    """The second memorisation channel, closed at its source.

    `task_pattern` is what retrieval scores against. A literal stored there is
    a hit waiting to happen for the wrong reason, whatever the advice says.
    """
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)

    assert "pens" not in skill.task_pattern
    assert "1.25" not in skill.task_pattern
    assert "slot_number" in skill.task_pattern
    # The kind of grading it satisfied travels with the shape: that is what
    # recurs across tasks, unlike the node name it used to be keyed by.
    assert "json_parses" in skill.task_pattern


# --- the evidence bar --------------------------------------------------------


async def test_one_task_is_not_evidence_that_an_approach_transfers(library):
    """The defect being fixed, stated as a bar.

    Two agents passing the same node of the same run share a task, a criterion
    and a prompt. That is one observation drawn twice -- it says the work is
    repeatable, which is worth recording, and says nothing about whether the
    approach applies anywhere else. The candidate is kept; a human is not
    asked about it yet, because the question has no evidence either way.
    """
    skill = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)

    assert len(skill.evidence_tasks) == 1
    assert not skill.promotable
    assert library.pending() == [skill]      # recorded, accruing evidence
    assert library.promotable() == []        # but not put to a reviewer
    assert library.approved() == []


async def test_a_second_distinct_task_shape_makes_it_promotable(library):
    """The positive case: the same advice, derived independently by a task
    about a different thing, is what "this transfers" looks like.

    It lands on the SAME skill because ids are content-addressed and the
    advice is now literal-free -- which is the mechanism that lets evidence
    accumulate instead of fragmenting into one unusable candidate per run.
    """
    first = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    second = await distil(library, PENCIL_TASK, "calculate_pencil_cost", PENCIL_TASK)

    assert second.skill_id == first.skill_id, "the same advice became two skills"
    assert len(second.evidence_tasks) == MIN_DISTINCT_TASKS
    assert second.promotable
    assert library.promotable() == [second]


async def test_the_same_question_with_new_numbers_is_not_a_second_task(library):
    """Otherwise the bar is trivially farmable.

    A task generator emitting the same question with fresh digits would
    manufacture unlimited "distinct task" evidence, and the promotion rule
    would certify approaches on exactly the correlated draws it exists to
    reject. Number-swapped near-duplicates share an abstract fingerprint.
    """
    await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    again = await distil(library, PEN_AGAIN, "calculate_pen_cost", PEN_AGAIN)

    assert len(again.evidence_tasks) == 1
    assert not again.promotable


async def test_the_same_question_reworded_is_not_a_second_task(library):
    """The farming case the number-swap test misses, and the likelier one.

    A task generator varying digits is a hypothetical; a person re-asking the
    same question with "please" and "for me" around it, or a noisy upstream
    rephrasing it, is Tuesday. Keyed on the abstracted SENTENCE this passed as
    two distinct tasks -- the added words are not literals, so nothing
    collapsed them -- and one request, cosmetically reworded, put a candidate
    in front of a reviewer labelled as having transferred. The distilled advice
    is byte-identical in both runs, which is the tell: nothing was learned the
    second time.
    """
    first = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    polite = await distil(library, PEN_POLITELY, "calculate_pen_cost", PEN_TASK)

    assert polite.skill_id == first.skill_id
    assert len(polite.evidence_tasks) == 1
    assert not polite.promotable
    assert library.promotable() == []

    # ...and the bar is still clearable by a task that is actually different.
    pencils = await distil(library, PENCIL_TASK, "calculate_pencil_cost", PENCIL_TASK)
    assert len(pencils.evidence_tasks) == MIN_DISTINCT_TASKS
    assert pencils.promotable


async def test_no_human_is_asked_until_a_second_task_agrees(library):
    """The bar stated in the units it is actually about: a reviewer's attention.

    Every other test here reads `library.promotable()`, which is a PROXY for
    "was a human asked". This wires the real durable approval queue -- the
    thing `SkillGate.submit` writes to and `swarmd list` reads -- so the
    assertion is the decision itself and not a stand-in for it. Asking a
    reviewer "does this approach transfer?" after one task is asking a question
    with no evidence either way, and a queue of those is how the gate stops
    being read.
    """
    approvals = ApprovalManager(InMemoryApprovalStore())

    await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK, approvals)
    assert await approvals.pending() == []

    await distil(library, PENCIL_TASK, "calculate_pencil_cost", PENCIL_TASK, approvals)
    queued = await approvals.pending()
    assert len(queued) == 1
    assert queued[0].item["skill_id"] == library.promotable()[0].skill_id


async def test_evidence_survives_a_process_boundary(library, tmp_path):
    """Evidence accrues across runs, so it has to accrue across restarts too:
    a bar that resets on every process start can never be cleared."""
    first = await distil(library, PEN_TASK, "calculate_pen_cost", PEN_TASK)
    reloaded = SkillLibrary(tmp_path / "skills.json")
    stored = reloaded.get(first.skill_id)

    assert stored is not None
    assert stored.evidence_tasks == first.evidence_tasks
    assert isinstance(stored.evidence_tasks, tuple), "JSON lists must come back as tuples"


def test_evidence_from_the_same_shape_twice_is_not_two_pieces_of_evidence(library):
    """Idempotent by construction. Re-running one task must not walk a
    candidate over the bar by repetition."""
    skill = library.propose(
        name="approach: produce total_cost",
        task_pattern="slot_number slot_term",
        instruction="derive the value from the task at hand and report it",
        evidence_task="abcd1234",
    )
    library.propose(
        name="approach: produce total_cost",
        task_pattern="slot_number slot_term",
        instruction="derive the value from the task at hand and report it",
        evidence_task="abcd1234",
    )
    assert skill.evidence_tasks == ("abcd1234",)
    assert not skill.promotable


# --- the literal gate --------------------------------------------------------


def test_an_instruction_sharing_a_number_with_its_task_is_refused():
    """Raises, never repairs -- the same policy as the serialised-output gate.

    A silently rewritten skill is one nobody reviewed, and the human approval
    gate is the only thing between a hallucinated approach and every future
    run's prompt. A caller that gets a mutated string back cannot tell that
    anything was wrong with what it proposed.
    """
    with pytest.raises(SkillLibraryError, match="shares the literal"):
        validate_instruction(
            "Compute the total cost of 3 pens at 1.25 dollars each",
            source_task=PEN_TASK,
        )


def test_the_same_instruction_is_accepted_when_no_task_is_claimed():
    """A check that cannot see the task cannot claim anything about the task.

    The default has to stay permissive or every existing caller quietly starts
    asserting something it never verified.
    """
    assert validate_instruction("Compute the total cost of 3 pens at 1.25 each")


def test_an_instruction_that_only_shares_method_words_is_accepted():
    """The gate is about literals, not vocabulary. Refusing "compute the total"
    because the task also said it would reject every correct instruction."""
    assert validate_instruction(
        "Compute the total cost from the quantity and the unit price given",
        source_task=PEN_TASK,
    )


def test_a_number_that_is_the_method_is_not_a_leak():
    """"Round to 2 decimal places" is advice. Comparing literals as whole
    tokens is what keeps a correct instruction from being refused because a
    digit of the task's price appears inside one of its numbers."""
    assert validate_instruction(
        "Compute the total and round to 2 decimal places", source_task=PEN_TASK
    )


def test_a_method_number_is_not_a_leak_when_the_task_uses_the_same_digit():
    """The case the test above cannot see, because PEN_TASK has no 2 in it.

    The leak check read the instruction with one regex per literal kind,
    independently of the pass that decides what a literal IS -- so it saw a
    value in "round to 2 decimal places" that `abstract` had already ruled was
    method vocabulary. Any task carrying a bare 2 then collided with it, and
    `_distill` swallows the refusal in a blanket `except`: no skill, no
    reviewer, no event saying why. Exactly the numeric, quantity-heavy tasks
    this feature was built for are the ones that trip it.
    """
    assert validate_instruction(
        "Compute the total and round to 2 decimal places",
        source_task="Compute the total cost of 2 pens at 1.25 dollars each",
    )


def test_a_price_restated_as_a_bare_number_is_still_a_leak():
    """The strictness that must survive fixing the above. One amount has
    several spellings, and an instruction that writes the task's price without
    its currency word is carrying the answer just the same."""
    with pytest.raises(SkillLibraryError, match="shares the literal"):
        validate_instruction(
            "Start every unit from 1.25 and multiply", source_task=PEN_TASK
        )


# --- distillation without a run ----------------------------------------------


def test_a_step_is_abstracted_even_with_no_task_to_compare_against():
    """Shape abstraction does not need the task. A caller with no source text
    still gets the numbers removed -- it only loses the bare-noun pass, which
    is the part that genuinely requires knowing what the question said."""
    run = SwarmRun(ScriptedProvider(plan_for("n", "i")), profile="smoke")
    node = PlanNode(name="calc", instruction=PEN_TASK)

    step = run._distil_step(node, "calc", "")
    assert "1.25" not in step
    assert "pens" in step, "with no task, a bare noun cannot be identified as one"

    step_with_task = run._distil_step(node, "calc", PEN_TASK)
    assert "pens" not in step_with_task


# --- schema compatibility ----------------------------------------------------


def test_a_library_written_by_an_older_build_still_loads(tmp_path):
    """New fields are additive and defaulted, so old EVIDENCE still reads.

    This test used to also assert that an old file marking a skill `approved`
    loads. It no longer does, and the split is deliberate -- see
    `test_an_old_approval_without_an_attestation_is_refused`. Reading old
    evidence is compatibility; honouring an old approval that carries no
    attestation is the hole the attestation exists to close, and "it is an old
    file" is precisely the excuse an attacker would offer.
    """
    instruction = "use csv.DictReader with an explicit dialect"
    path = tmp_path / "skills.json"
    path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "skill_id": make_skill_id("old", instruction),
                        "name": "old",
                        "task_pattern": "parse csv files",
                        "instruction": instruction,
                        "approved": False,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loaded = SkillLibrary(path)
    assert loaded.all()[0].evidence_tasks == ()
    assert not loaded.all()[0].approved


def test_an_old_approval_without_an_attestation_is_refused(tmp_path):
    """And the refusal has to say how to fix it, or it is just breakage.

    A library that predates the attestation cannot prove its approvals were
    ever made by a human, so it is refused rather than trusted -- but the
    operator is told the remedy (re-approve through the gate) instead of being
    left with a file that will not load and no next step.
    """
    instruction = "use csv.DictReader with an explicit dialect"
    path = tmp_path / "skills.json"
    path.write_text(
        json.dumps(
            {
                "skills": [
                    {
                        "skill_id": make_skill_id("old", instruction),
                        "name": "old",
                        "task_pattern": "parse csv files",
                        "instruction": instruction,
                        "approved": True,
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(SkillLibraryError, match="no attestation"):
        SkillLibrary(path)

def test_the_evidence_fields_do_not_move_a_skill_id():
    """Ids hash name+instruction and nothing else. If accruing evidence moved
    the id, every load-time integrity check would fail on the skills that had
    been learning the most -- and the id is the only thing making a hand-edited
    `approved: true` detectable."""
    before = make_skill_id("n", "do the thing carefully")
    skill = Skill(
        skill_id=before,
        name="n",
        task_pattern="p",
        instruction="do the thing carefully",
        evidence_tasks=("a", "b"),
        generality=0.9,
    )
    assert make_skill_id(skill.name, skill.instruction) == before


def test_the_shape_of_a_task_is_never_stored_as_its_text(library):
    """An evidence key is a fingerprint, not a task. A record that cannot hold
    a literal cannot leak one, whoever reads it -- the same minimisation that
    keeps literals out of the advice and the index."""
    fingerprint = task_signature(PEN_TASK)
    skill = library.propose(
        name="n",
        task_pattern="slot_number slot_term",
        instruction="derive the value from the task at hand and report it",
        evidence_task=fingerprint,
    )
    stored = json.dumps(skill.to_dict())
    assert "pens" not in stored
    assert "1.25" not in stored


# --- evidence has to be able to accumulate (ADR-014) -----------------------


def test_the_same_approach_worded_differently_is_one_skill(tmp_path):
    """The first of two reasons nothing ever reached the approval queue.

    `make_skill_id` hashes the instruction TEXT, and the instruction is written
    by a model, so the same approach distilled from two runs came back phrased
    differently and minted a second record starting again from one piece of
    evidence. `promotable` wants two distinct shapes, so the queue stayed
    empty, the library never held an approved skill, and the treatment arm of
    every ablation had nothing to retrieve.
    """
    library = SkillLibrary(str(tmp_path / "skills.json"))

    first = library.propose(
        name="approach: produce diagnosis, minimal_fix",
        task_pattern="determine why the slot_term failed and state the change",
        instruction="Name the mechanism that fails, then the smallest edit.",
        evidence_task="shape-permissions",
    )
    second = library.propose(
        name="approach: produce diagnosis, minimal_fix",
        task_pattern="determine why the slot_term failed and state the change",
        instruction="State the failing mechanism first, then the minimal edit.",
        evidence_task="shape-timezones",
    )

    assert second.skill_id == first.skill_id, "one approach, one record"
    assert len(library.all()) == 1
    assert set(second.evidence_tasks) == {"shape-permissions", "shape-timezones"}
    assert second.promotable, "two distinct shapes is exactly what the bar asks"


def test_merging_keeps_the_instruction_that_was_actually_stored(tmp_path):
    """`skill_id` still addresses the CONTENT, so a record verifies against its
    own hash. The second wording is discarded, not blended in -- blending model
    text is how a library fills with advice nobody wrote."""
    library = SkillLibrary(str(tmp_path / "skills.json"))
    first = library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term items",
        instruction="Sum the line items and round once at the end.",
        evidence_task="shape-aaa",
    )
    merged = library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term items",
        instruction="Add each line, rounding only the final figure.",
        evidence_task="shape-bbb",
    )
    assert merged.instruction == first.instruction


def test_two_steps_with_the_same_shape_and_checks_are_one_approach(tmp_path):
    """The accepted cost of ADR-014, asserted so it stays deliberate.

    Identity is the artifact shape plus the kinds of check that graded it. Two
    steps of one task that produce the same shape under the same checks
    therefore merge, and the instruction kept is the one proposed first.

    Why that is the right trade: the alternative is including the plan step,
    and plan steps are synthesised per task. A key containing one can only ever
    match another proposal from the SAME task -- which is exactly the evidence
    the promotion bar refuses to count -- so it can never accrue the second
    shape, at any sample size. Those two records were also already competing
    for the same retrieval slot, because `_terms` indexes on the same name.
    """
    library = SkillLibrary(str(tmp_path / "skills.json"))
    library.propose(
        name="approach: produce dates, amounts",
        task_pattern="identify every calendar date in the slot_term json_parses",
        instruction="Scan in document order and keep the original spelling.",
        evidence_task="shape-extract",
    )
    library.propose(
        name="approach: produce dates, amounts",
        task_pattern="identify every monetary amount in the slot_term json_parses",
        instruction="Keep the currency symbol with the figure it belongs to.",
        evidence_task="shape-extract",
    )

    assert len(library.all()) == 1
    assert library.all()[0].instruction.startswith("Scan in document order")


def test_the_same_work_graded_differently_is_still_one_approach(tmp_path):
    """This reverses an earlier version of this test, on measured evidence.

    The old rule put the criterion's check kinds in the identity, reasoning
    that an artifact which must parse as JSON is not the same approach as one
    checked for being non-empty. That is true in principle and wrong in
    practice: the criterion is authored fresh for every run (ADR-009), so its
    check set varies between two runs of the SAME work. Keying on it
    reintroduced exactly the fragmentation the key exists to remove -- the same
    approach from two runs landed on two records, each with one task shape,
    and neither could ever clear the bar.

    Measured on a 38-record library: name plus check kinds gave 38 approaches
    and 3 that cleared the bar; the name alone gave 34 and 5.
    """
    library = SkillLibrary(str(tmp_path / "skills.json"))
    library.propose(
        name="approach: produce verdict",
        task_pattern="decide whether the slot_term holds json_parses contains_all",
        instruction="State the verdict first, then each unmet requirement.",
        evidence_task="shape-uptime",
    )
    library.propose(
        name="approach: produce verdict",
        task_pattern="decide whether the slot_term holds output_nonempty",
        instruction="Answer in prose; there is no structure to satisfy.",
        evidence_task="shape-halved-time",
    )

    merged = library.all()
    assert len(merged) == 1, "one approach, graded two ways by two criteria"
    assert set(merged[0].evidence_tasks) == {"shape-uptime", "shape-halved-time"}
    assert merged[0].promotable, "and the evidence now reaches the bar"


def test_a_retired_approach_is_not_revived_by_re_proposing_it(tmp_path):
    """Pruning is a decision. A re-proposal must not undo it through the back
    door of evidence accrual, leaving `retired_reason` describing a live
    skill."""
    library = SkillLibrary(str(tmp_path / "skills.json"))
    original = library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term items",
        instruction="Sum the line items and round once at the end.",
        evidence_task="shape-aaa",
    )
    library.reject(original.skill_id, actor="reviewer", reason="failed six times")

    fresh = library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term items",
        instruction="Add each line, rounding only the final figure.",
        evidence_task="shape-bbb",
    )

    assert fresh.skill_id != original.skill_id
    retired = library.get(original.skill_id)
    assert retired is not None and retired.retired


def test_a_skill_that_has_already_failed_enough_is_not_offered_again(tmp_path):
    """Measured cost of waiting for consolidation: 26 uses at 0% success.

    `prune` runs every N TASKS, and within one task a skill can be retrieved by
    every node -- so a skill the library already had the evidence to withdraw
    kept being handed to workers until consolidation caught up. Retrieval now
    applies the same rule, against the same constants, so the damage is capped
    at the evidence threshold rather than at the consolidation interval.
    """
    library = SkillLibrary(str(tmp_path / "skills.json"))
    skill = library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term line items into a total",
        instruction="Sum the line items and round once at the end.",
        evidence_task="shape-a",
    )
    library.propose(
        name="approach: produce total",
        task_pattern="add the slot_term line items into a total",
        instruction="Sum the line items and round once at the end.",
        evidence_task="shape-b",
    )
    library.approve(skill.skill_id, actor="reviewer")
    query = "add the line items into a total"
    assert library.retrieve(query), "offered while it still has standing"

    for _ in range(PRUNE_MIN_USES):
        library.record_use(skill.skill_id, success=False)

    assert library.retrieve(query) == [], "and withheld once the record is in"
    # Consolidation still does the retiring; this only stops the bleeding.
    assert not library.get(skill.skill_id).retired
    assert library.prune()
