"""Corroboration: a skill serves only what more than one task shape attested.

ADR-015 named domain-as-method as the leak class no rule can detect by looking
at one instruction -- `probes` and `monitoring` are structurally identical to
`parse` and `validate`. These tests pin the mechanism that removes it WITHOUT
deciding what a domain word is: a second task shape either used the word or it
did not.
"""

from __future__ import annotations

import pytest

from swarmd.swarm.generalise import corroborate
from swarmd.swarm.skills import (
    INSTRUCTION_PREFIX,
    INSTRUCTION_SHAPE_CLAUSE,
    Skill,
    SkillLibrary,
    split_instruction,
)

SHAPE_TAIL = "number, string. Take the KEY NAMES from your own criterion, never from here."


def instruction(prose: str) -> str:
    return f"{INSTRUCTION_PREFIX}{prose}{INSTRUCTION_SHAPE_CLAUSE}{SHAPE_TAIL}"


UPTIME = instruction(
    "Run synthetic probes and real-user monitoring to collect the downtime figures."
)
LEDGER = instruction(
    "Collect the ledger figures and check them against the stated totals."
)


def skill(*variants: str) -> Skill:
    return Skill(
        skill_id="s1",
        name="approach: produce a, b",
        task_pattern="pattern",
        instruction=variants[0],
        evidence_tasks=tuple(f"shape{i}" for i in range(len(variants))),
        evidence_instructions=variants,
    )


def test_domain_vocabulary_only_one_task_used_is_not_served() -> None:
    served = skill(UPTIME, LEDGER).served_instruction
    for domain_word in ("probes", "monitoring", "synthetic", "downtime", "ledger", "totals"):
        assert domain_word not in served.lower()
    # ...and what BOTH tasks did say survives, so the advice is reduced rather
    # than deleted.
    assert "collect" in served.lower()
    assert "figures" in served.lower()


def test_the_structured_tail_is_never_corroborated_away() -> None:
    served = skill(UPTIME, LEDGER).served_instruction
    assert INSTRUCTION_SHAPE_CLAUSE.strip() in served
    assert "number, string" in served


def test_a_single_variant_is_served_verbatim() -> None:
    """One wording corroborated against itself would LOOK verified."""
    assert skill(UPTIME).served_instruction == UPTIME


def test_a_repeated_wording_is_not_two_variants() -> None:
    """`merge_identity` replays one instruction once per accrued shape."""
    assert skill(UPTIME, UPTIME).served_instruction == UPTIME


def test_prose_that_survives_nothing_falls_back_to_structure_only() -> None:
    a = instruction("Tally the pens.")
    b = instruction("Weigh the flour.")
    served = skill(a, b).served_instruction
    assert served.startswith("Produce a JSON object")
    assert "pens" not in served and "flour" not in served


def test_an_instruction_not_in_the_distiller_format_is_untouched() -> None:
    hand_written = "Sort the rows before summing them."
    s = skill(hand_written, "Anything else entirely.")
    assert s.served_instruction == hand_written


def test_split_instruction_refuses_to_guess() -> None:
    assert split_instruction("no prefix here") is None
    prefix, prose, tail = split_instruction(instruction("Do the thing.")) or ("", "", "")
    assert prefix == INSTRUCTION_PREFIX
    assert prose == "Do the thing."
    assert tail.startswith("Produce a JSON object")


@pytest.mark.parametrize("variants", [[], [""], ["   "]])
def test_corroborate_handles_nothing_to_corroborate(variants: list[str]) -> None:
    assert corroborate(variants) == ""


def test_a_second_shape_records_its_own_wording(tmp_path) -> None:
    """The evidence a skill needs to be VERIFIED, not just counted."""
    library = SkillLibrary(tmp_path / "skills.json")
    first = library.propose(
        name="approach: produce total, method",
        task_pattern="count slot_number slot_term",
        instruction=UPTIME,
        evidence_task="shape-a",
    )
    second = library.propose(
        name="approach: produce total, method",
        task_pattern="count slot_number slot_term",
        instruction=LEDGER,
        evidence_task="shape-b",
    )
    assert second.skill_id == first.skill_id, "same approach, one record"
    assert len(second.evidence_instructions) == 2
    assert "probes" not in second.served_instruction.lower()
    assert second.instruction == UPTIME, "the stored text is half the content address"


def test_merging_identities_keeps_every_recorded_wording(tmp_path) -> None:
    """A migration must not un-verify advice two shapes had corroborated."""
    source = SkillLibrary(tmp_path / "before.json")
    source.propose(
        name="approach: produce total, method",
        task_pattern="count slot_number slot_term",
        instruction=UPTIME,
        evidence_task="shape-a",
    )
    source.propose(
        name="approach: produce total, method",
        task_pattern="count slot_number slot_term",
        instruction=LEDGER,
        evidence_task="shape-b",
    )
    merged, _ = source.merge_identity(tmp_path / "after.json")
    [record] = merged.all()
    assert len(record.evidence_instructions) == 2
    assert "probes" not in record.served_instruction.lower()
