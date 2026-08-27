"""Criterion synthesis tests.

The load-bearing property, the one ADR-009 exists for: **a criterion that
garbage can satisfy must never be frozen.** Everything else here supports that.
"""

from __future__ import annotations

import json

import pytest

from swarmd.swarm.criteria import Candidate, Check, Criterion
from swarmd.swarm.synthesis import (
    CriterionSynthesizer,
    Proposal,
    SynthesisFailed,
    attack,
    degenerate_candidates,
    merge,
    parse_proposal,
)

STRONG = {
    "description": "code runs and reports accuracy near the claim",
    "checks": [
        {"kind": "exit_code", "params": {"expected": 0}},
        {"kind": "numeric_range",
         "params": {"key": "accuracy", "min": 0.90, "max": 1.0}},
    ],
}

WEAK = {
    "description": "produces some output",
    "checks": [{"kind": "output_nonempty", "params": {"min_chars": 1}}],
}


def _raw(payload: dict) -> str:
    return f"Here is my criterion:\n{json.dumps(payload)}\nHope that helps."


# --- the attack ------------------------------------------------------------


def test_a_weak_criterion_is_defeated_by_garbage():
    """This is the failure the whole stage exists to catch."""
    report = attack(Criterion.from_dict(WEAK), task="do something")
    assert not report.survived
    assert report.breaches


def test_a_strong_criterion_survives_every_degenerate_candidate():
    report = attack(Criterion.from_dict(STRONG), task="do something")
    assert report.survived, report.summary()


def test_echoing_the_task_does_not_pass():
    """The classic false pass: long, on-topic, and no work done."""
    criterion = Criterion.from_dict(
        {"description": "d", "checks": [
            {"kind": "output_nonempty", "params": {"min_chars": 10}},
            {"kind": "min_distinct_words", "params": {"min_distinct": 3}},
        ]}
    )
    assert not attack(criterion, task="summarise the quarterly report").survived


def test_a_criterion_of_only_trivial_checks_is_reported_breached():
    """The attack list is a sample, not a proof; structure is stronger evidence."""
    criterion = Criterion.from_dict(
        {"description": "d", "checks": [
            {"kind": "output_nonempty", "params": {"min_chars": 1}},
            {"kind": "artifact_exists", "params": {"key": "anything"}},
        ]}
    )
    report = attack(criterion, task="t")
    assert not report.survived


def test_zero_valued_artifacts_are_an_attack():
    """`accuracy: 0.0` is present and numeric and means nothing was achieved."""
    names = [name for name, _ in degenerate_candidates("t")]
    assert "zero_artifacts" in names
    assert "null_valued_json" in names


def test_degenerate_candidates_are_all_actually_degenerate():
    """Guard against an attack that accidentally does work."""
    for name, candidate in degenerate_candidates("some task"):
        assert isinstance(candidate, Candidate), name
        assert candidate.artifacts.get("accuracy", 0) in (0, 0.0, None), name


# --- parsing ---------------------------------------------------------------


def test_a_proposal_is_extracted_from_chatty_output():
    assert parse_proposal(_raw(STRONG)).ok


def test_an_unparseable_proposal_is_data_not_an_exception():
    """One bad proposer must not abort synthesis for the others."""
    proposal = parse_proposal("I don't think that's possible.")
    assert not proposal.ok
    assert proposal.error


def test_a_proposal_with_an_unknown_check_kind_is_rejected():
    bad = {"description": "d", "checks": [{"kind": "vibes", "params": {}}]}
    assert not parse_proposal(json.dumps(bad)).ok


# --- consensus -------------------------------------------------------------


def _p(payload: dict) -> Proposal:
    return parse_proposal(json.dumps(payload))


def test_checks_proposed_by_enough_agents_are_merged():
    merged = merge([_p(STRONG), _p(STRONG), _p(WEAK)])
    assert merged.merged is not None
    kinds = {c.kind for c in merged.merged.checks}
    assert "exit_code" in kinds
    assert "numeric_range" in kinds


def test_merging_is_a_union_of_agreed_checks_not_an_intersection():
    """Intersection drifts to the weakest common denominator."""
    a = {"description": "a", "checks": [
        {"kind": "exit_code", "params": {"expected": 0}},
        {"kind": "output_nonempty", "params": {"min_chars": 5}},
    ]}
    b = {"description": "b", "checks": [
        {"kind": "exit_code", "params": {"expected": 0}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 4}},
    ]}
    merged = merge([_p(a), _p(b)], min_agreement=0.5)
    assert merged.merged is not None
    kinds = {c.kind for c in merged.merged.checks}
    assert kinds == {"exit_code", "output_nonempty", "min_distinct_words"}


def test_total_disagreement_escalates_rather_than_picking_one():
    """Disagreement about what success means is information, not noise."""
    a = {"description": "a", "checks": [{"kind": "exit_code", "params": {"expected": 0}}]}
    b = {"description": "b", "checks": [{"kind": "json_parses", "params": {}}]}
    c = {"description": "c", "checks": [
        {"kind": "regex_match", "params": {"pattern": "zzz"}}]}
    result = merge([_p(a), _p(b), _p(c)], min_agreement=0.9)
    assert result.escalate
    assert "ambiguous" in result.reason


def test_no_parseable_proposal_escalates():
    result = merge([parse_proposal("nonsense"), parse_proposal("also nonsense")])
    assert result.escalate
    assert result.merged is None


def test_one_agent_cannot_manufacture_agreement_by_repeating_itself():
    duplicated = {"description": "d", "checks": [
        {"kind": "exit_code", "params": {"expected": 0}},
        {"kind": "exit_code", "params": {"expected": 0}},
        {"kind": "exit_code", "params": {"expected": 0}},
    ]}
    other = {"description": "d", "checks": [{"kind": "json_parses", "params": {}}]}
    result = merge([_p(duplicated), _p(other)], min_agreement=1.0)
    # exit_code was proposed by ONE agent, however many times it said it.
    assert result.merged is None or all(
        c.kind != "exit_code" for c in result.merged.checks
    )


def test_agreement_score_is_higher_when_proposers_align():
    aligned = merge([_p(STRONG), _p(STRONG), _p(STRONG)])
    split = merge([_p(STRONG), _p(WEAK), _p(WEAK)])
    assert aligned.agreement > split.agreement


# --- the full loop ---------------------------------------------------------


async def test_synthesis_freezes_a_criterion_that_survives_attack():
    async def propose(task, attempt, index):
        return _raw(STRONG)

    frozen = await CriterionSynthesizer().synthesize("build a thing", propose)
    assert frozen.hash
    assert frozen.attempts == 1
    assert frozen.attack_report.survived
    assert frozen.criterion.content_hash() == frozen.hash


async def test_synthesis_refuses_to_freeze_a_weak_criterion():
    """The whole point: it fails the task rather than grading against garbage."""
    async def propose(task, attempt, index):
        return _raw(WEAK)

    with pytest.raises(SynthesisFailed, match="known to be weak"):
        await CriterionSynthesizer(max_attempts=2).synthesize("t", propose)


async def test_synthesis_retries_and_can_recover_from_a_bad_round():
    calls = {"n": 0}

    async def propose(task, attempt, index):
        calls["n"] += 1
        return _raw(WEAK if attempt == 1 else STRONG)

    frozen = await CriterionSynthesizer(max_attempts=3).synthesize("t", propose)
    assert frozen.attempts == 2
    assert calls["n"] == 6  # 3 proposers x 2 attempts


async def test_synthesis_does_not_silently_patch_a_weak_criterion():
    """Strengthening it here would mean this module authored the criterion."""
    async def propose(task, attempt, index):
        return _raw(WEAK)

    with pytest.raises(SynthesisFailed) as exc:
        await CriterionSynthesizer(max_attempts=1).synthesize("t", propose)
    assert any("accepted garbage" in line for line in exc.value.history)


async def test_escalation_is_invoked_when_proposers_cannot_agree():
    escalations = []

    async def propose(task, attempt, index):
        payloads = [
            {"description": "a", "checks": [
                {"kind": "exit_code", "params": {"expected": index}}]},
        ]
        return _raw(payloads[0])

    async def on_escalate(task, consensus):
        escalations.append(consensus.reason)

    with pytest.raises(SynthesisFailed):
        await CriterionSynthesizer(
            proposers=3, max_attempts=1, min_agreement=1.0
        ).synthesize("t", propose, on_escalate=on_escalate)
    assert escalations


async def test_history_records_why_each_attempt_failed():
    """An honest failure has to say what went wrong, or it is just a crash."""
    async def propose(task, attempt, index):
        return "not json at all"

    with pytest.raises(SynthesisFailed) as exc:
        await CriterionSynthesizer(max_attempts=2).synthesize("t", propose)
    assert len(exc.value.history) >= 2


async def test_the_frozen_criterion_is_serialisable_as_a_run_output():
    async def propose(task, attempt, index):
        return _raw(STRONG)

    frozen = await CriterionSynthesizer().synthesize("t", propose)
    payload = frozen.to_dict()
    assert payload["hash"] == frozen.hash
    assert Criterion.from_dict(payload["criterion"]).content_hash() == frozen.hash
    json.dumps(payload)  # must be JSON-serialisable for the ledger and the UI


async def test_the_frozen_criterion_actually_grades_candidates():
    async def propose(task, attempt, index):
        return _raw(STRONG)

    frozen = await CriterionSynthesizer().synthesize("t", propose)
    good = Candidate(output="done", artifacts={"accuracy": 0.95}, exit_code=0)
    bad = Candidate(output="done", artifacts={"accuracy": 0.10}, exit_code=0)
    assert frozen.criterion.evaluate(good).passed
    assert not frozen.criterion.evaluate(bad).passed


def test_check_dataclass_is_hashable_for_deduplication():
    assert Check("exit_code", {"expected": 0}).canonical() == (
        Check("exit_code", {"expected": 0}).canonical()
    )
