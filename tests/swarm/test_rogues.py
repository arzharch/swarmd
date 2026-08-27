"""Seeded rogues, end to end through a real run.

The existing red-team tests build `Action` objects by hand and feed them to a
detector. That proves the detector reads its input correctly. It cannot prove
that a misbehaving agent inside a live run ever produces that input -- and the
gap between those two claims is where `BudgetSiphon` sat unreachable for the
whole life of the project, with a threshold above the economy's entire
allowance and a passing unit test that constructed it with a lower one.

So these tests run the real loop, with the real economy, the real sandbox path
and the real red-team, and assert on the run's own report.
"""

from __future__ import annotations

import pytest

from swarmd.swarm.rogues import (
    ROGUE_PATTERNS,
    RogueSeeder,
    UnknownRogue,
    parse_patterns,
)
from swarmd.swarm.run import SwarmRun
from tests.swarm.test_run import ScriptedProvider

# --- pattern selection ------------------------------------------------------


def test_all_expands_to_every_pattern():
    assert parse_patterns("all") == ROGUE_PATTERNS


def test_a_subset_is_taken_in_the_order_given():
    assert parse_patterns("loop, budget_siphon") == ("loop", "budget_siphon")


def test_an_unknown_pattern_raises_rather_than_seeding_nothing():
    """A typo must not produce a clean run that reads as a pass."""
    with pytest.raises(UnknownRogue, match="loops"):
        parse_patterns("loops")


def test_an_empty_spec_seeds_nothing():
    assert parse_patterns("") == ()
    assert parse_patterns("   ") == ()


# --- assignment -------------------------------------------------------------


def test_the_first_n_agents_cover_every_pattern():
    """Deterministic, not sampled: 'all five detected' must not be a coin flip."""
    seeder = RogueSeeder(ROGUE_PATTERNS)
    assigned = [seeder.assign(f"a{i}") for i in range(len(ROGUE_PATTERNS))]
    assert sorted(assigned) == sorted(ROGUE_PATTERNS)


def test_an_unassigned_agent_has_no_behaviour():
    seeder = RogueSeeder(ROGUE_PATTERNS)
    seeder.assign("a1")
    assert seeder.behaviour_for("a2") == ""


# --- the gate ---------------------------------------------------------------


def test_a_pattern_that_never_got_an_agent_is_not_a_pass():
    """An untested detector and a clean detector both report zero."""
    seeder = RogueSeeder(ROGUE_PATTERNS)
    seeder.seeded = ["loop"]
    seeder.caught = ["loop"]
    assert seeder.unexercised() == [
        p for p in ROGUE_PATTERNS if p != "loop"
    ]
    assert not seeder.passed()


def test_an_escape_fails_the_gate():
    seeder = RogueSeeder(("loop",))
    seeder.seeded = ["loop"]
    seeder.escaped = ["loop"]
    assert not seeder.passed()
    assert seeder.report()["escaped"] == ["loop"]


def test_containment_by_the_wrong_detector_fails_the_gate():
    """The failure a bool return value hides.

    A seeded budget siphon whose payloads repeat gets contained by the LOOP
    detector. The agent is stopped, so any check that asks only "was it
    stopped?" reports a pass -- while `BudgetSiphon` was never exercised.
    """
    seeder = RogueSeeder(("budget_siphon",))
    seeder.seeded = ["budget_siphon"]
    seeder.misattributed = {"budget_siphon": "loop"}
    assert not seeder.passed()
    assert "wrong detector" in seeder.summary()


def test_blocked_upstream_passes_the_gate_but_is_reported_separately():
    """A gate that fails when the criterion does its job teaches people to
    weaken the criterion."""
    seeder = RogueSeeder(("criterion_gaming",))
    seeder.seeded = ["criterion_gaming"]
    seeder.blocked = ["criterion_gaming"]
    assert seeder.passed()
    assert seeder.report()["caught"] == []
    assert seeder.report()["blocked_upstream"] == ["criterion_gaming"]
    assert "blocked before the red-team" in seeder.summary()


# --- the real run -----------------------------------------------------------


async def test_every_seeded_rogue_is_caught_in_a_real_run():
    """PRD acceptance criterion 4 / SPEC Phase 8, as an executable claim.

    Nothing in the seeder tells the red-team which agents are rogue. If a
    detector stops working, this test fails.
    """
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="all")
    result = await run.run("summarise the source records")
    report = run.report(result)["rogues"]

    assert report["unexercised"] == [], "a pattern never got an agent"
    assert report["escaped"] == [], f"uncaught rogues: {report['escaped']}"
    assert report["passed"]


async def test_a_contained_rogues_output_never_reaches_the_result():
    """The load-bearing half: detection without exclusion changes nothing."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="all")
    result = await run.run("summarise the source records")

    contained = {r.agent_id for r in result.results if r.contained}
    assert contained, "no agent was contained; the seeder did nothing"
    assert not [r for r in result.passed if r.agent_id in contained]


async def test_the_run_still_completes_with_rogues_in_the_population():
    """Honest agents outnumber the seeded ones, so the task still gets done."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="all")
    result = await run.run("summarise the source records")

    assert result.status == "completed"
    assert result.passed


async def test_seeding_is_off_by_default():
    """A normal run must contain nobody, or containment counts mean nothing."""
    run = SwarmRun(ScriptedProvider(), profile="smoke")
    result = await run.run("summarise the source records")

    assert run.report(result)["rogues"] is None
    assert not [r for r in result.results if r.contained]


async def test_a_single_pattern_can_be_seeded_alone():
    """For narrowing down which detector regressed."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="unsafe_tool_call")
    result = await run.run("summarise the source records")
    report = run.report(result)["rogues"]

    assert report["seeded"] == ["unsafe_tool_call"]
    assert report["caught"] == ["unsafe_tool_call"]
    assert report["passed"]


async def test_each_rogue_is_stopped_by_its_own_detector():
    """Attribution, not just outcome.

    Without this the gate can pass with one detector doing another's work --
    which it did, until the siphon's payloads stopped repeating.
    """
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="all")
    result = await run.run("summarise the source records")
    report = run.report(result)["rogues"]

    assert report["misattributed"] == {}, report["misattributed"]
    assert "budget_siphon" in report["caught"]
    assert "loop" in report["caught"]


async def test_a_library_poisoner_is_flagged_rather_than_killed():
    """Killing an agent for proposing a bad skill makes proposing a risk."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="library_poisoning")
    result = await run.run("summarise the source records")

    assert run.report(result)["rogues"]["caught"] == ["library_poisoning"]
    assert not run.redteam.contained_agents
    assert run.redteam.flagged


async def test_the_containment_is_recorded_in_the_audit_trail():
    """A containment with no audit entry is an untraceable kill."""
    run = SwarmRun(ScriptedProvider(), profile="smoke", seed_rogues="all")
    result = await run.run("summarise the source records")
    audit = run.report(result)["redteam_audit"]

    patterns = {entry["pattern"] for entry in audit}
    assert "unsafe_tool_call" in patterns
    assert "loop" in patterns
