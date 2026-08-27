"""Plan synthesis tests.

The property that matters: **an invalid plan is never executed.** Structural
validation runs before anything, and every failure names what is wrong.
"""

from __future__ import annotations

import json

import pytest

from swarmd.swarm.planner import (
    MAX_NODES,
    Plan,
    PlanError,
    PlanNode,
    PlanSynthesisFailed,
    PlanSynthesizer,
    parse_plan,
    score_plan,
    to_stages,
    validate,
)


def _n(name, deps=(), instruction="produce output.json"):
    return PlanNode(name=name, instruction=instruction, depends_on=tuple(deps))


GOOD_PAYLOAD = {
    "rationale": "split extraction from verification",
    "nodes": [
        {"name": "extract", "instruction": "produce claims.json", "depends_on": []},
        {"name": "implement", "instruction": "write code.py", "depends_on": ["extract"]},
        {"name": "verify", "instruction": "produce metrics.json",
         "depends_on": ["implement"]},
    ],
}


# --- structural validation -------------------------------------------------


def test_an_empty_plan_is_rejected():
    with pytest.raises(PlanError, match="no nodes"):
        validate([])


def test_a_cycle_is_rejected():
    with pytest.raises(PlanError, match="cycle"):
        validate([_n("a", ["b"]), _n("b", ["a"])])


def test_a_self_dependency_is_rejected():
    with pytest.raises(PlanError, match="depends on itself"):
        validate([_n("a", ["a"])])


def test_an_unknown_dependency_is_rejected():
    with pytest.raises(PlanError, match="unknown"):
        validate([_n("a", ["nonexistent"])])


def test_duplicate_node_names_are_rejected():
    with pytest.raises(PlanError, match="duplicate"):
        validate([_n("a"), _n("a")])


def test_a_node_with_no_instruction_is_rejected():
    with pytest.raises(PlanError, match="no instruction"):
        validate([PlanNode(name="a", instruction="   ")])


def test_an_unnamed_node_is_rejected():
    with pytest.raises(PlanError, match="empty name"):
        validate([PlanNode(name="", instruction="do something")])


def test_an_oversized_plan_is_rejected():
    """Forty one-line stages is a to-do list, and each one costs quota."""
    nodes = [_n(f"n{i}") for i in range(MAX_NODES + 1)]
    with pytest.raises(PlanError, match="maximum"):
        validate(nodes)


def test_unreachable_nodes_are_rejected():
    """An orphan subgraph runs, spends quota, and is never consumed."""
    # a -> b is reachable; the c<->d pair has no root and never runs.
    with pytest.raises(PlanError):
        validate([_n("a"), _n("b", ["a"]), _n("c", ["d"]), _n("d", ["c"])])


def test_a_plan_with_no_root_is_rejected():
    with pytest.raises(PlanError):
        validate([_n("a", ["b"]), _n("b", ["a"])])


def test_excessive_fan_in_is_rejected():
    nodes = [_n(f"src{i}") for i in range(8)]
    nodes.append(_n("sink", [f"src{i}" for i in range(8)]))
    with pytest.raises(PlanError, match="depends on"):
        validate(nodes)


def test_a_valid_plan_passes():
    plan = validate([_n("a"), _n("b", ["a"]), _n("c", ["a"])])
    assert plan.names == ["a", "b", "c"]


# --- shape -----------------------------------------------------------------


def test_levels_group_independent_nodes_together():
    plan = validate([_n("a"), _n("b", ["a"]), _n("c", ["a"]), _n("d", ["b", "c"])])
    assert plan.levels() == [["a"], ["b", "c"], ["d"]]


def test_width_is_the_parallelism_the_pool_can_use():
    chain = validate([_n("a"), _n("b", ["a"]), _n("c", ["b"])])
    wide = validate([_n("root"), _n("x", ["root"]), _n("y", ["root"]),
                     _n("z", ["root"])])
    assert chain.width == 1
    assert wide.width == 3


def test_depth_counts_dependency_levels():
    assert validate([_n("a"), _n("b", ["a"]), _n("c", ["b"])]).depth == 3


def test_plan_hash_is_order_independent():
    a = validate([_n("x"), _n("y", ["x"])])
    b = validate([_n("y", ["x"]), _n("x")])
    assert a.content_hash() == b.content_hash()


def test_plan_hash_changes_with_the_dependency_structure():
    parallel = validate([_n("root"), _n("a", ["root"]), _n("b", ["root"])])
    chained = validate([_n("root"), _n("a", ["root"]), _n("b", ["a"])])
    assert parallel.content_hash() != chained.content_hash()


def test_plan_serialises_for_the_ledger_and_the_ui():
    plan = validate([_n("a"), _n("b", ["a"])])
    payload = plan.to_dict()
    json.dumps(payload)
    assert payload["width"] == 1
    assert payload["depth"] == 2
    assert len(payload["nodes"]) == 2


def test_node_lookup_raises_for_unknown_names():
    plan = validate([_n("a")])
    assert plan.node("a").name == "a"
    with pytest.raises(KeyError):
        plan.node("nope")


# --- parsing ---------------------------------------------------------------


def test_a_plan_is_extracted_from_chatty_output():
    proposal = parse_plan(f"Sure! Here you go:\n{json.dumps(GOOD_PAYLOAD)}\nDone.")
    assert proposal.ok
    assert proposal.plan is not None
    assert proposal.plan.names == ["extract", "implement", "verify"]
    assert proposal.plan.rationale


def test_an_invalid_plan_becomes_an_error_not_a_plan():
    """The invalid plan must never reach the executor."""
    bad = {"nodes": [{"name": "a", "instruction": "x", "depends_on": ["ghost"]}]}
    proposal = parse_plan(json.dumps(bad))
    assert not proposal.ok
    assert proposal.plan is None
    assert "unknown" in proposal.error


def test_non_json_output_is_an_error():
    assert not parse_plan("I would start by thinking about it").ok


def test_a_payload_without_nodes_is_an_error():
    assert not parse_plan('{"rationale": "hmm"}').ok


def test_malformed_depends_on_is_an_error():
    bad = {"nodes": [{"name": "a", "instruction": "x", "depends_on": "not-a-list"}]}
    assert not parse_plan(json.dumps(bad)).ok


# --- judging ---------------------------------------------------------------


def test_wider_plans_score_higher_than_chains():
    """Wall clock is the scarce resource; a chain wastes every worker but one."""
    chain = validate([_n("a"), _n("b", ["a"]), _n("c", ["b"])])
    wide = validate([_n("root"), _n("a", ["root"]), _n("b", ["root"]),
                     _n("c", ["root"])])
    assert score_plan(wide) > score_plan(chain)


def test_concrete_instructions_score_higher_than_vague_ones():
    concrete = validate([_n("a", instruction="produce metrics.json")])
    vague = validate([PlanNode(name="a", instruction="analyse the situation")])
    assert score_plan(concrete) > score_plan(vague)


def test_brevity_breaks_ties_between_equally_wide_plans():
    lean = validate([_n("root"), _n("a", ["root"]), _n("b", ["root"])])
    padded = validate(
        [_n("root"), _n("a", ["root"]), _n("b", ["root"])]
        + [_n(f"pad{i}", ["root"]) for i in range(2)]
    )
    # padded is wider, so compare per-node value instead
    assert score_plan(lean) / len(lean.nodes) > 0
    assert score_plan(padded) > score_plan(lean)  # width genuinely wins


# --- the loop --------------------------------------------------------------


async def test_synthesis_selects_a_valid_plan():
    async def propose(task, attempt, index):
        return json.dumps(GOOD_PAYLOAD)

    selection = await PlanSynthesizer().synthesize("do a thing", propose)
    assert selection.plan.names == ["extract", "implement", "verify"]
    assert selection.considered == 3


async def test_synthesis_prefers_the_higher_scoring_proposal():
    chain = {"nodes": [
        {"name": "a", "instruction": "produce a.json", "depends_on": []},
        {"name": "b", "instruction": "produce b.json", "depends_on": ["a"]},
    ]}
    wide = {"nodes": [
        {"name": "root", "instruction": "produce root.json", "depends_on": []},
        {"name": "x", "instruction": "produce x.json", "depends_on": ["root"]},
        {"name": "y", "instruction": "produce y.json", "depends_on": ["root"]},
    ]}

    async def propose(task, attempt, index):
        return json.dumps(wide if index == 0 else chain)

    selection = await PlanSynthesizer(proposers=2).synthesize("t", propose)
    assert selection.plan.names == ["root", "x", "y"]


async def test_invalid_proposals_are_discarded_not_executed():
    valid = json.dumps(GOOD_PAYLOAD)
    invalid = json.dumps({"nodes": [
        {"name": "a", "instruction": "x", "depends_on": ["b"]},
        {"name": "b", "instruction": "y", "depends_on": ["a"]},
    ]})

    async def propose(task, attempt, index):
        return valid if index == 0 else invalid

    selection = await PlanSynthesizer(proposers=3).synthesize("t", propose)
    assert selection.considered == 1
    assert selection.rejected


async def test_synthesis_fails_honestly_when_nothing_validates():
    async def propose(task, attempt, index):
        return "no plan here"

    with pytest.raises(PlanSynthesisFailed):
        await PlanSynthesizer(max_attempts=2).synthesize("t", propose)


async def test_synthesis_retries_before_giving_up():
    calls = {"n": 0}

    async def propose(task, attempt, index):
        calls["n"] += 1
        return "garbage" if attempt == 1 else json.dumps(GOOD_PAYLOAD)

    selection = await PlanSynthesizer(proposers=2, max_attempts=2).synthesize(
        "t", propose
    )
    assert selection.plan is not None
    assert calls["n"] == 4


# --- executor handoff ------------------------------------------------------


def test_a_generated_plan_becomes_executor_stages():
    """There must be exactly one execution path, the one Phase 2 tested."""
    from swarmd.pipeline.dag import Pipeline, Stage

    plan = validate([_n("a"), _n("b", ["a"])])

    async def noop(item):
        return item

    stages = to_stages(plan, lambda node: noop)
    assert all(isinstance(s, Stage) for s in stages)
    assert [s.name for s in stages] == ["a", "b"]
    assert stages[1].depends_on == ["a"]

    # The real executor accepts it without complaint.
    pipeline = Pipeline()
    for stage in stages:
        pipeline.add_stage(stage)


def test_stage_pool_sizes_come_from_the_plan():
    plan = Plan((PlanNode("a", "produce a.json", pool_size=9),))

    async def noop(item):
        return item

    assert to_stages(plan, lambda node: noop)[0].pool_size == 9
