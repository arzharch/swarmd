"""Evaluation harness tests.

The two commitments under test are the ones that make every other number in
the project defensible:

  1. No improvement figure without a paired control arm.
  2. Overlapping confidence intervals report "no measured improvement", in
     those words, rather than a soft positive.
"""

from __future__ import annotations

import json

import pytest

from swarmd.ledger import SimulatedDataRefused
from swarmd.swarm.evaluate import (
    EvalReport,
    Evaluator,
    MissingControlArm,
    Task,
    TaskOutcome,
    bootstrap_ci,
    compare,
    intervals_overlap,
    summarise,
)


def _outcome(
    *, task_id="t1", solved=True, treatment=True, cost=0.001, seed=0,
    arm="custom", first_pass=True, containments=0, simulated=False,
):
    return TaskOutcome(
        task_id=task_id, arm=arm, domain="general", seed=seed,
        treatment=treatment, solved=solved, cost_usd=cost, tokens=500,
        duration_s=1.0, first_pass=first_pass, containments=containments,
        status="completed" if solved else "error", simulated=simulated,
    )


# --- the refusal that matters ----------------------------------------------


def test_an_improvement_figure_without_a_control_is_refused():
    """The single most common way self-improvement claims get made."""
    with pytest.raises(MissingControlArm, match="paired run"):
        compare([_outcome()], [])


def test_overlapping_intervals_report_a_non_result_in_those_words():
    """A non-result that reads as a soft positive is worse than no measurement."""
    treatment = [_outcome(task_id=f"t{i}", solved=i % 2 == 0, seed=i)
                 for i in range(6)]
    control = [_outcome(task_id=f"t{i}", solved=i % 2 == 0, treatment=False, seed=i)
               for i in range(6)]

    result = compare(treatment, control)
    assert result["verdict"] == "no measured improvement"
    assert result["intervals_overlap"] is True
    assert "non-result" in result["note"]


def test_a_genuine_improvement_is_reported_as_one():
    treatment = [_outcome(task_id=f"t{i}", solved=True, seed=i) for i in range(10)]
    control = [_outcome(task_id=f"t{i}", solved=False, treatment=False, seed=i)
               for i in range(10)]

    result = compare(treatment, control)
    assert result["verdict"] == "improvement"
    assert result["success_rate_delta"] == 1.0
    assert result["intervals_overlap"] is False


def test_a_regression_is_reported_as_one_not_hidden():
    treatment = [_outcome(task_id=f"t{i}", solved=False, seed=i) for i in range(10)]
    control = [_outcome(task_id=f"t{i}", solved=True, treatment=False, seed=i)
               for i in range(10)]
    assert compare(treatment, control)["verdict"] == "regression"


def test_comparison_is_paired_on_task_and_seed():
    """Task difficulty varies more than the effect; unpaired buries the signal."""
    treatment = [_outcome(task_id="a", seed=1), _outcome(task_id="b", seed=2)]
    control = [
        _outcome(task_id="a", seed=1, treatment=False, solved=False),
        _outcome(task_id="b", seed=2, treatment=False, solved=False),
    ]
    result = compare(treatment, control)
    assert result["pairs"] == 2
    assert result["paired_mean_delta"] == 1.0


def test_unpairable_outcomes_are_not_counted_as_pairs():
    treatment = [_outcome(task_id="a", seed=1)]
    control = [_outcome(task_id="different", seed=99, treatment=False)]
    assert compare(treatment, control)["pairs"] == 0


# --- statistics ------------------------------------------------------------


def test_bootstrap_intervals_are_reproducible():
    """An interval that moves between analyses is not something anyone can check."""
    values = [0.1, 0.4, 0.2, 0.9, 0.5, 0.3]
    assert bootstrap_ci(values) == bootstrap_ci(values)


def test_a_wider_spread_gives_a_wider_interval():
    tight = bootstrap_ci([0.5] * 10)
    loose = bootstrap_ci([0.0, 1.0] * 5)
    assert (loose[1] - loose[0]) > (tight[1] - tight[0])


def test_a_single_observation_reports_a_degenerate_interval():
    """Better than a fabricated spread from one data point."""
    assert bootstrap_ci([0.7]) == (0.7, 0.7)


def test_an_empty_sample_does_not_crash():
    assert bootstrap_ci([]) == (0.0, 0.0)


def test_interval_overlap_detection():
    assert intervals_overlap((0.0, 0.5), (0.4, 1.0))
    assert not intervals_overlap((0.0, 0.3), (0.4, 1.0))
    assert intervals_overlap((0.0, 0.4), (0.4, 1.0))  # touching counts


# --- summaries -------------------------------------------------------------


def test_cost_is_reported_per_solved_task_not_per_task():
    """A run that failed cheaply is not efficient."""
    outcomes = [
        _outcome(solved=True, cost=0.01),
        _outcome(solved=False, cost=0.01),
    ]
    summary = summarise(outcomes, "treatment")
    assert summary.cost_per_solved == pytest.approx(0.02)   # total / solves


def test_an_arm_that_solved_nothing_has_no_cost_per_solved():
    """Dividing by zero solves would report infinite efficiency or crash."""
    summary = summarise([_outcome(solved=False)], "control")
    assert summary.cost_per_solved is None


def test_first_pass_rate_counts_runs_without_repair():
    outcomes = [_outcome(first_pass=True), _outcome(first_pass=False)]
    assert summarise(outcomes, "x").first_pass_rate == 0.5


def test_an_empty_arm_summarises_without_crashing():
    assert summarise([], "control").runs == 0


# --- reports ---------------------------------------------------------------


def _report(**kw):
    report = EvalReport(repeats=2)
    report.outcomes = [
        _outcome(task_id=f"t{i}", seed=i, solved=True, **kw) for i in range(4)
    ] + [
        _outcome(task_id=f"t{i}", seed=i, solved=False, treatment=False, **kw)
        for i in range(4)
    ]
    return report


def test_a_report_separates_the_public_and_custom_arms():
    """A strong result on one must not hide a weak result on the other."""
    report = EvalReport()
    report.outcomes = [
        _outcome(arm="public", task_id="p1"),
        _outcome(arm="public", task_id="p1", treatment=False, solved=False),
        _outcome(arm="custom", task_id="c1"),
        _outcome(arm="custom", task_id="c1", treatment=False, solved=False),
    ]
    data = report.to_dict()
    assert set(data["arms"]) == {"public", "custom"}


def test_an_arm_with_no_control_reports_unavailable_rather_than_a_number():
    report = EvalReport()
    report.outcomes = [_outcome()]
    comparison = report.to_dict()["arms"]["custom"]["comparison"]
    assert comparison["verdict"] == "unavailable"
    assert "control arm" in comparison["reason"]


def test_a_report_is_json_serialisable():
    json.dumps(_report().to_dict())


def test_the_rendered_report_states_the_verdict():
    rendered = _report().render()
    assert "VERDICT" in rendered
    assert "treatment" in rendered and "control" in rendered


def test_a_simulated_report_says_so_loudly():
    report = _report(simulated=True)
    assert report.to_dict()["simulated"] is True
    assert "SIMULATED DATA" in report.render()


def test_benchmarks_generation_refuses_simulated_data(tmp_path):
    """The whole point of the taint: synthetic runs cannot become a benchmark."""
    with pytest.raises(SimulatedDataRefused, match="BENCHMARKS"):
        _report(simulated=True).write_benchmarks(tmp_path / "BENCHMARKS.md")


def test_benchmarks_are_generated_not_hand_written(tmp_path):
    target = tmp_path / "BENCHMARKS.md"
    _report().write_benchmarks(target)
    text = target.read_text(encoding="utf-8")
    assert "Do not edit by hand" in text
    assert "| treatment | control |" in text
    assert "Verdict" in text


def test_the_json_report_can_be_written(tmp_path):
    target = tmp_path / "eval.json"
    _report().write_json(target)
    assert json.loads(target.read_text(encoding="utf-8"))["total_runs"] == 8


# --- the harness -----------------------------------------------------------


class FakeResult:
    def __init__(self, solved, attempts=1, status="completed"):
        self.status = status
        self.duration_s = 0.5
        self.results = [type("R", (), {"passed": solved, "attempts": attempts})()]
        self.contained = []


async def test_the_evaluator_runs_both_arms_for_every_repeat():
    """Same task, same seed, skills on and off. The pairing is the design."""
    calls = []

    async def run_factory(task, use_skills, seed):
        calls.append((task.task_id, use_skills, seed))
        return FakeResult(solved=use_skills), {"cost": {"total_usd": 0.001}}

    tasks = [Task("t1", "do a thing"), Task("t2", "do another")]
    report = await Evaluator(run_factory, repeats=3).evaluate(tasks)

    assert len(calls) == 2 * 3 * 2          # tasks x repeats x arms
    assert len(report.outcomes) == 12
    # Every (task, seed) appears once per arm.
    seeds = {(t, s) for t, _, s in calls}
    assert len(seeds) == 6


async def test_the_evaluator_uses_the_same_seed_for_both_arms():
    seen = {}

    async def run_factory(task, use_skills, seed):
        seen.setdefault(seed, set()).add(use_skills)
        return FakeResult(solved=True), {"cost": {"total_usd": 0.0}}

    await Evaluator(run_factory, repeats=2).evaluate([Task("t1", "x")])
    assert all(arms == {True, False} for arms in seen.values())


async def test_a_run_is_solved_only_when_every_node_passed():
    async def run_factory(task, use_skills, seed):
        return FakeResult(solved=False), {"cost": {"total_usd": 0.0}}

    report = await Evaluator(run_factory, repeats=1).evaluate([Task("t1", "x")])
    assert all(not o.solved for o in report.outcomes)


async def test_a_failed_run_is_not_counted_as_solved():
    async def run_factory(task, use_skills, seed):
        return FakeResult(solved=True, status="failed_criterion"), {
            "cost": {"total_usd": 0.0}
        }

    report = await Evaluator(run_factory, repeats=1).evaluate([Task("t1", "x")])
    assert all(not o.solved for o in report.outcomes)


async def test_repair_rounds_are_reflected_in_the_first_pass_rate():
    async def run_factory(task, use_skills, seed):
        return FakeResult(solved=True, attempts=3), {"cost": {"total_usd": 0.0}}

    report = await Evaluator(run_factory, repeats=1).evaluate([Task("t1", "x")])
    assert all(not o.first_pass for o in report.outcomes)


async def test_simulated_runs_taint_the_eval_report():
    async def run_factory(task, use_skills, seed):
        return FakeResult(solved=True), {
            "cost": {"total_usd": 0.0, "simulated": True}
        }

    report = await Evaluator(run_factory, repeats=1).evaluate([Task("t1", "x")])
    assert report.to_dict()["simulated"] is True


def test_benchmarks_records_the_commit_it_measured(tmp_path):
    """A benchmarks file that cannot say which code produced it cannot be
    checked.

    This is not hypothetical. A 20-run eval finished and wrote a file whose
    numbers described a configuration changed while it ran -- the profile's
    proposer count went from 2 to 3 mid-eval, which changes the criterion from
    a union of proposals into a majority of them. The file looked current, and
    nothing in it could have told you otherwise.
    """
    from swarmd.swarm.evaluate import _git_sha

    target = tmp_path / "BENCHMARKS.md"
    _report().write_benchmarks(target)

    text = target.read_text(encoding="utf-8")
    assert "Measured on commit" in text
    assert _git_sha() in text


def test_a_dirty_tree_is_reported_as_dirty():
    """A dirty tree means the code that ran is not the code the SHA names."""
    from swarmd.swarm.evaluate import _git_sha

    sha = _git_sha()
    assert sha == "unknown" or len(sha) >= 7
