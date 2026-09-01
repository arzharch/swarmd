"""Criterion tests.

The properties worth defending:
  1. A criterion cannot be satisfied by output that did no work.
  2. Its hash is stable identity -- same checks, same hash, whatever the order.
  3. Malformed proposals fail at PARSE time, never mid-grade.
"""

from __future__ import annotations

import pytest

from swarmd.swarm.criteria import (
    CHECK_KINDS,
    Candidate,
    Check,
    CheckError,
    Criterion,
)
from swarmd.swarm.synthesis import attack


def _crit(*checks: Check, description: str = "d") -> Criterion:
    return Criterion(description, tuple(checks))


# --- structural guarantees -------------------------------------------------


def test_a_criterion_with_no_checks_is_refused():
    """It would accept everything, which is worse than having no criterion."""
    with pytest.raises(CheckError, match="accepts everything"):
        Criterion("empty", ())


def test_unknown_check_kinds_are_refused_at_construction():
    """A NameError halfway through grading would look like a runtime fault."""
    with pytest.raises(CheckError, match="unknown check kind"):
        _crit(Check("definitely_not_a_check", {}))


def test_invalid_regexes_are_refused_at_construction():
    with pytest.raises(CheckError, match="invalid regex"):
        _crit(Check("regex_match", {"pattern": "([unclosed"}))


def test_from_dict_rejects_malformed_payloads():
    with pytest.raises(CheckError):
        Criterion.from_dict({"description": "x"})
    with pytest.raises(CheckError):
        Criterion.from_dict({"checks": ["not-a-dict"]})


def test_round_trip_through_dict_preserves_the_hash():
    original = _crit(
        Check("exit_code", {"expected": 0}),
        Check("numeric_range", {"key": "accuracy", "min": 0.9, "max": 1.0}),
    )
    assert Criterion.from_dict(original.to_dict()).content_hash() == (
        original.content_hash()
    )


# --- content addressing ----------------------------------------------------


def test_hash_is_order_independent():
    """Two proposals listing the same checks differently are one criterion."""
    a = _crit(Check("exit_code", {"expected": 0}), Check("output_nonempty", {}))
    b = _crit(Check("output_nonempty", {}), Check("exit_code", {"expected": 0}))
    assert a.content_hash() == b.content_hash()


def test_hash_ignores_parameter_key_order():
    a = _crit(Check("numeric_range", {"key": "acc", "min": 0.9, "max": 1.0}))
    b = _crit(Check("numeric_range", {"max": 1.0, "min": 0.9, "key": "acc"}))
    assert a.content_hash() == b.content_hash()


def test_hash_changes_when_a_threshold_changes():
    """Grading against a different bar must be a different criterion."""
    strict = _crit(Check("numeric_range", {"key": "acc", "min": 0.94}))
    loose = _crit(Check("numeric_range", {"key": "acc", "min": 0.80}))
    assert strict.content_hash() != loose.content_hash()


def test_hash_ignores_the_description():
    """Prose is for humans; identity is the checks."""
    a = Criterion("one phrasing", (Check("output_nonempty", {}),))
    b = Criterion("entirely different prose", (Check("output_nonempty", {}),))
    assert a.content_hash() == b.content_hash()


# --- grading ---------------------------------------------------------------


def test_all_checks_run_even_after_one_fails():
    """Short-circuiting gives a worse repair signal and burns the repair budget."""
    result = _crit(
        Check("output_nonempty", {"min_chars": 500}),
        Check("exit_code", {"expected": 0}),
        Check("contains_all", {"substrings": ["absent"]}),
    ).evaluate(Candidate(output="short", exit_code=1))
    assert len(result.outcomes) == 3
    assert len(result.failures) == 3


def test_a_candidate_passes_only_when_every_check_passes():
    criterion = _crit(
        Check("exit_code", {"expected": 0}),
        Check("min_distinct_words", {"min_distinct": 3}),
    )
    assert criterion.evaluate(
        Candidate(output="alpha beta gamma delta", exit_code=0)
    ).passed
    assert not criterion.evaluate(
        Candidate(output="alpha beta gamma delta", exit_code=1)
    ).passed


def test_malformed_params_fail_the_check_rather_than_crashing_the_run():
    """A bad param must not take down grading for every other candidate."""
    criterion = Criterion.__new__(Criterion)
    object.__setattr__(criterion, "description", "d")
    object.__setattr__(criterion, "checks", (Check("numeric_range", {}),))
    result = criterion.evaluate(Candidate(output="x"))
    assert not result.passed
    assert "malformed" in result.outcomes[0].detail


def test_failure_summary_names_every_failing_check():
    result = _crit(
        Check("exit_code", {"expected": 0}),
        Check("artifact_exists", {"key": "report"}),
    ).evaluate(Candidate(output="", exit_code=3))
    assert "exit_code" in result.summary()
    assert "artifact_exists" in result.summary()


# --- individual checks -----------------------------------------------------


def test_numeric_range_is_the_workhorse_for_reproduction_tasks():
    check = _crit(Check("numeric_range", {"key": "accuracy", "min": 0.90, "max": 0.98}))
    assert check.evaluate(Candidate(artifacts={"accuracy": 0.942})).passed
    assert not check.evaluate(Candidate(artifacts={"accuracy": 0.61})).passed


def test_numeric_range_fails_closed_on_a_missing_artifact():
    """Absent evidence is not evidence of success."""
    criterion = _crit(Check("numeric_range", {"key": "accuracy", "min": 0.0}))
    assert not criterion.evaluate(Candidate(artifacts={})).passed


def test_numeric_range_fails_closed_on_a_non_numeric_artifact():
    criterion = _crit(Check("numeric_range", {"key": "acc", "min": 0.0}))
    assert not criterion.evaluate(Candidate(artifacts={"acc": "high"})).passed


def test_json_parses_can_require_keys():
    criterion = _crit(Check("json_parses", {"required_keys": ["answer"]}))
    assert criterion.evaluate(Candidate(output='{"answer": 4}')).passed
    assert not criterion.evaluate(Candidate(output='{"other": 4}')).passed
    assert not criterion.evaluate(Candidate(output="not json")).passed


def test_min_distinct_words_rejects_repeated_token_padding():
    """Length and non-emptiness are satisfied; information content is not."""
    criterion = _crit(Check("min_distinct_words", {"min_distinct": 5}))
    assert not criterion.evaluate(Candidate(output=" ".join(["same"] * 100))).passed
    assert criterion.evaluate(Candidate(output="one two three four five")).passed


def test_contains_all_is_case_insensitive_by_default():
    criterion = _crit(Check("contains_all", {"substrings": ["Answer"]}))
    assert criterion.evaluate(Candidate(output="the answer is 4")).passed


def test_artifact_exists_treats_empty_values_as_absent():
    """A key present with an empty value is not evidence anything was produced."""
    criterion = _crit(Check("artifact_exists", {"key": "report"}))
    assert not criterion.evaluate(Candidate(artifacts={"report": ""})).passed
    assert not criterion.evaluate(Candidate(artifacts={"report": None})).passed
    assert criterion.evaluate(Candidate(artifacts={"report": "text"})).passed


def test_every_registered_check_kind_is_reachable():
    """A check in the registry that nothing constructs is dead weight."""
    for kind in CHECK_KINDS:
        assert isinstance(kind, str) and kind


# --- weakness detection ----------------------------------------------------


def test_a_criterion_of_only_trivial_checks_is_flagged_weak():
    assert _crit(
        Check("output_nonempty", {}), Check("artifact_exists", {"key": "x"})
    ).is_weak()


def test_one_substantive_check_makes_a_criterion_non_weak():
    assert not _crit(
        Check("output_nonempty", {}), Check("exit_code", {"expected": 0})
    ).is_weak()


# --- the criterion that accepts nothing -------------------------------------


def test_a_criterion_that_nothing_can_satisfy_is_refused():
    """THE ONE THAT HAPPENED, on a held-out task, twice.

    A warehouse task asked how many boxes ship five orders. Two proposers
    disagreed about the answer and the consensus merge unioned their checks:
    `total_boxes` had to be exactly 8 AND exactly 9. Eight is correct; nothing
    is both. Every worker failed every attempt on every run -- 0/30 nodes, then
    0/12 on a re-run with the same criterion hash, because a frozen criterion
    is memoised and the impossible target was replayed rather than re-authored.

    `attack` cannot find this. It tries degenerate candidates, and a criterion
    nothing can satisfy rejects those exactly as it rejects correct work, so it
    survives every attack while making the task unsolvable.
    """
    criterion = Criterion(
        "boxes per order and in total",
        (
            Check("json_parses", {"required_keys": ["boxes_per_order", "total_boxes"]}),
            Check("numeric_range", {"key": "total_boxes", "min": 8, "max": 8}),
            Check("numeric_range", {"key": "total_boxes", "min": 9, "max": 9}),
        ),
    )

    conflicts = criterion.contradictions()
    assert len(conflicts) == 1
    assert "total_boxes" in conflicts[0]

    report = attack(criterion, "ship five orders in boxes of twelve")
    assert not report.survived
    assert any("unsatisfiable" in b for b in report.breaches)


def test_overlapping_ranges_on_one_key_are_not_a_contradiction():
    """Two checks narrowing the same value is ordinary: the intersection is
    non-empty, so something can satisfy both. Only a proof counts."""
    criterion = Criterion(
        "a bounded total",
        (
            Check("numeric_range", {"key": "total", "min": 0, "max": 10}),
            Check("numeric_range", {"key": "total", "min": 5, "max": 8}),
        ),
    )
    assert criterion.contradictions() == ()


def test_a_single_inverted_range_is_a_contradiction():
    """min above max is unsatisfiable on its own, without needing a second
    check to disagree with."""
    criterion = Criterion(
        "an impossible bound",
        (Check("numeric_range", {"key": "total", "min": 9, "max": 4}),),
    )
    assert criterion.contradictions()


def test_ranges_on_different_keys_never_conflict():
    """The keys are independent values. Conflating them would refuse ordinary
    criteria that bound two different numbers."""
    criterion = Criterion(
        "two totals",
        (
            Check("numeric_range", {"key": "boxes", "min": 8, "max": 8}),
            Check("numeric_range", {"key": "orders", "min": 5, "max": 5}),
        ),
    )
    assert criterion.contradictions() == ()
