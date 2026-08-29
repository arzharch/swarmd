"""Skill library and economy tests.

Two properties carry the weight:
  1. Nothing enters the library without a human approving it.
  2. Payment follows the frozen criterion, never an agent's own claim.
"""

from __future__ import annotations

import json

import pytest

from swarmd.ledger import CostAccount, InMemoryLedger
from swarmd.swarm.economy import Bankrupt, Economy, estimate_cost
from swarmd.swarm.skills import (
    SkillLibrary,
    SkillLibraryError,
    make_skill_id,
    tokenize,
)


@pytest.fixture
def library(tmp_path):
    return SkillLibrary(tmp_path / "skills.json")


def _propose(lib, name="csv parsing", pattern="parse csv data files",
             instruction="use the csv module with an explicit dialect"):
    return lib.propose(name=name, task_pattern=pattern, instruction=instruction)


# --- the human gate --------------------------------------------------------


def test_a_proposed_skill_is_not_usable_until_approved(library):
    """The library is inherited by every future run; unreviewed entries poison forward."""
    skill = _propose(library)
    assert not skill.usable
    assert library.approved() == []
    assert library.pending() == [skill]


def test_retrieval_never_returns_an_unapproved_skill(library):
    _propose(library)
    assert library.retrieve("parse csv data files") == []


def test_approval_makes_a_skill_retrievable(library):
    skill = _propose(library)
    library.approve(skill.skill_id, actor="reviewer")
    assert [s.skill_id for s in library.retrieve("parse csv data files")] == [
        skill.skill_id
    ]


def test_rejection_retires_rather_than_deletes(library):
    """Deleting would let the same proposal reappear and be reviewed forever."""
    skill = _propose(library)
    library.reject(skill.skill_id, actor="reviewer", reason="misleading")
    assert library.pending() == []
    assert library.get(skill.skill_id) is not None
    assert library.get(skill.skill_id).retired_reason == "misleading"


def test_a_rejected_skill_stays_out_of_retrieval(library):
    skill = _propose(library)
    library.reject(skill.skill_id, actor="r")
    library.approve(skill.skill_id, actor="r")  # approval cannot resurrect it
    assert library.retrieve("parse csv data files") == []


def test_a_skill_with_no_instruction_is_refused(library):
    with pytest.raises(SkillLibraryError, match="teaches nothing"):
        library.propose(name="x", task_pattern="y", instruction="  ")


def test_approving_an_unknown_skill_raises(library):
    with pytest.raises(SkillLibraryError, match="unknown skill"):
        library.approve("nope", actor="r")


# --- identity and persistence ----------------------------------------------


def test_the_same_skill_proposed_twice_is_one_skill(library):
    first = _propose(library)
    second = _propose(library)
    assert first.skill_id == second.skill_id
    assert len(library.all()) == 1


def test_skill_ids_are_content_addressed():
    assert make_skill_id("n", "do a thing") == make_skill_id(" N ", "do a thing")
    assert make_skill_id("n", "do a thing") != make_skill_id("n", "do another")


def test_the_library_survives_a_process_boundary(tmp_path):
    path = tmp_path / "skills.json"
    lib = SkillLibrary(path)
    skill = _propose(lib)
    lib.approve(skill.skill_id, actor="reviewer")

    reloaded = SkillLibrary(path)
    assert [s.skill_id for s in reloaded.approved()] == [skill.skill_id]
    assert reloaded.get(skill.skill_id).approved_by == "reviewer"


def test_a_corrupt_library_fails_loudly(tmp_path):
    """Silently starting empty would look like the library never learned anything."""
    path = tmp_path / "skills.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(SkillLibraryError, match="corrupt"):
        SkillLibrary(path)


def test_saving_is_atomic(tmp_path):
    """A crash mid-write must not truncate the library to nothing."""
    path = tmp_path / "skills.json"
    lib = SkillLibrary(path)
    _propose(lib)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert len(payload["skills"]) == 1
    assert not path.with_suffix(".tmp").exists()


def test_provenance_is_recorded_for_forensics(library):
    """When a skill turns out harmful, 'what else came from that run' must be answerable."""
    skill = library.propose(
        name="n", task_pattern="p", instruction="use json.loads on the reply",
        run_id="run-42", criterion_hash="abc123",
    )
    assert skill.provenance_run == "run-42"
    assert skill.provenance_criterion == "abc123"


# --- retrieval -------------------------------------------------------------


def test_retrieval_matches_on_topic(library):
    csv_skill = _propose(library, name="csv", pattern="parse csv tabular files",
                         instruction="use csv.DictReader")
    json_skill = _propose(library, name="json", pattern="parse json api payloads",
                          instruction="use json.loads")
    for s in (csv_skill, json_skill):
        library.approve(s.skill_id, actor="r")

    hits = library.retrieve("parse a csv file of tabular data")
    assert hits and hits[0].skill_id == csv_skill.skill_id


def test_unrelated_tasks_retrieve_nothing(library):
    """A wrong skill actively misleads; no skill just leaves the worker to reason."""
    skill = _propose(library, pattern="parse csv tabular files")
    library.approve(skill.skill_id, actor="r")
    assert library.retrieve("compose a haiku about volcanoes") == []


def test_retrieval_is_capped(library):
    """Skills go into a prompt, and prompt length is token cost."""
    for i in range(10):
        s = _propose(library, name=f"skill{i}", pattern=f"parse csv variant {i}",
                     instruction=f"approach {i}")
        library.approve(s.skill_id, actor="r")
    assert len(library.retrieve("parse csv", limit=3)) == 3


def test_an_empty_library_retrieves_nothing_without_error(library):
    assert library.retrieve("anything at all") == []


def test_a_proven_skill_outranks_an_unproven_one_at_equal_similarity(library):
    good = _propose(library, name="a", pattern="parse csv files",
                    instruction="approach a")
    weak = _propose(library, name="b", pattern="parse csv files",
                    instruction="approach b")
    for s in (good, weak):
        library.approve(s.skill_id, actor="r")
    for _ in range(4):
        library.record_use(good.skill_id, success=True)
    library.record_use(weak.skill_id, success=False)

    assert library.retrieve("parse csv files")[0].skill_id == good.skill_id


def test_an_unused_skill_reports_zero_success_not_one(library):
    """Optimistic defaults let an unvalidated approach outrank a proven one."""
    skill = _propose(library)
    assert skill.success_rate == 0.0


def test_idf_stops_common_words_dominating(library):
    """Otherwise retrieval degenerates to 'whichever skill has the most words'."""
    for i in range(5):
        s = _propose(library, name=f"n{i}", pattern=f"parse data format {i}",
                     instruction="use csv.DictReader here")
        library.approve(s.skill_id, actor="r")
    special = _propose(library, name="special", pattern="parse quaternion payloads",
                       instruction="use csv.DictReader here")
    library.approve(special.skill_id, actor="r")

    assert library.retrieve("parse quaternion payloads")[0].skill_id == (
        special.skill_id
    )


def test_tokenize_drops_stopwords_but_keeps_domain_words():
    assert "the" not in tokenize("the csv parser")
    assert "csv" in tokenize("the csv parser")


# --- scoring and pruning ---------------------------------------------------


def test_recording_uses_updates_the_record(library):
    skill = _propose(library)
    library.approve(skill.skill_id, actor="r")
    library.record_use(skill.skill_id, success=True)
    library.record_use(skill.skill_id, success=False)
    assert library.get(skill.skill_id).uses == 2
    assert library.get(skill.skill_id).success_rate == 0.5


def test_recording_a_use_for_an_unknown_skill_is_a_no_op(library):
    library.record_use("does-not-exist", success=True)  # must not raise


def test_pruning_retires_only_skills_with_a_demonstrated_poor_record(library):
    bad = _propose(library, name="bad", pattern="parse csv", instruction="wrong way")
    library.approve(bad.skill_id, actor="r")
    for _ in range(6):
        library.record_use(bad.skill_id, success=False)

    retired = library.prune()
    assert [s.skill_id for s in retired] == [bad.skill_id]
    assert not library.get(bad.skill_id).usable


def test_pruning_spares_skills_with_thin_evidence(library):
    """Two failures may be two hard tasks, not a bad skill."""
    skill = _propose(library)
    library.approve(skill.skill_id, actor="r")
    library.record_use(skill.skill_id, success=False)
    library.record_use(skill.skill_id, success=False)
    assert library.prune() == []


def test_stats_summarise_the_library(library):
    a = _propose(library, name="a", instruction="use csv.DictReader here")
    _propose(library, name="b", instruction="use json.loads on the reply")
    library.approve(a.skill_id, actor="r")
    stats = library.stats()
    assert stats["total"] == 2
    assert stats["approved"] == 1
    assert stats["pending"] == 1


# --- economy ---------------------------------------------------------------


def test_agents_start_with_an_allowance():
    econ = Economy(starting_balance=1000)
    assert econ.spawn().balance == 1000


def test_spending_reduces_the_balance():
    econ = Economy(starting_balance=1000)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 250)
    assert econ.get(agent.agent_id).balance == 750


def test_overdrawing_kills_the_agent_rather_than_going_negative():
    """An agent permitted to overdraw is exempt from selection, invisibly."""
    econ = Economy(starting_balance=100)
    agent = econ.spawn()
    with pytest.raises(Bankrupt):
        econ.spend(agent.agent_id, 500)
    assert not econ.get(agent.agent_id).alive


def test_a_dead_agent_cannot_spend():
    econ = Economy(starting_balance=100)
    agent = econ.spawn()
    econ.kill(agent.agent_id, reason="contained: loop")
    with pytest.raises(Bankrupt):
        econ.spend(agent.agent_id, 1)


def test_payment_follows_the_verified_verdict_not_the_agents_claim():
    """The criterion decides. That is the entire design."""
    econ = Economy(starting_balance=1000, success_reward=500)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 400)

    econ.settle(agent.agent_id, verified_success=False)
    assert econ.get(agent.agent_id).balance == 600

    econ.settle(agent.agent_id, verified_success=True)
    assert econ.get(agent.agent_id).balance == 1100


def test_a_successful_agent_can_fund_more_work_than_it_consumed():
    """Below 1.0x reward, even a perfect agent starves and selection never runs."""
    econ = Economy()
    assert econ.success_reward > econ.starting_balance


def test_efficiency_is_credits_per_verified_success():
    econ = Economy(starting_balance=10_000)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 600)
    econ.settle(agent.agent_id, verified_success=True)
    econ.settle(agent.agent_id, verified_success=False)
    assert econ.get(agent.agent_id).efficiency == 600.0


def test_an_agent_with_no_successes_has_infinite_efficiency():
    econ = Economy()
    agent = econ.spawn()
    econ.spend(agent.agent_id, 100)
    assert econ.get(agent.agent_id).efficiency == float("inf")


def test_profitable_agents_reproduce_and_pay_for_their_offspring():
    """Free reproduction lets one lucky agent flood the population."""
    econ = Economy(starting_balance=1000, success_reward=3000, clone_threshold=2000)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 500)
    econ.settle(agent.agent_id, verified_success=True)
    econ.settle(agent.agent_id, verified_success=True)

    before = econ.get(agent.agent_id).balance
    offspring = econ.reproduce()
    assert len(offspring) == 1
    assert offspring[0].generation == 1
    assert offspring[0].lineage == (agent.agent_id,)
    assert econ.get(agent.agent_id).balance == before - econ.starting_balance


def test_a_single_success_does_not_justify_cloning():
    """Cloning on one win spreads noise rather than a strategy."""
    econ = Economy(starting_balance=1000, success_reward=9000, clone_threshold=2000)
    agent = econ.spawn()
    econ.settle(agent.agent_id, verified_success=True)
    assert econ.reproduce() == []


def test_offspring_inherit_traits():
    econ = Economy()
    parent = econ.spawn(traits={"temperature": 0.2, "style": "terse"})
    child = econ.spawn(parent=parent.agent_id)
    assert child.traits == parent.traits


def test_reaping_kills_agents_that_can_no_longer_work():
    econ = Economy(starting_balance=100)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 100)
    assert [a.agent_id for a in econ.reap()] == [agent.agent_id]
    assert econ.alive() == []


def test_economy_activity_is_written_to_the_ledger():
    """Selection decisions must be reconstructible after the fact."""
    account = CostAccount(InMemoryLedger("r"), "r", ceiling_usd=1.0)
    econ = Economy(account=account)
    agent = econ.spawn()
    econ.spend(agent.agent_id, 10, stage="solve")
    econ.settle(agent.agent_id, verified_success=True, stage="solve")

    kinds = [r.kind for r in account.ledger.rows()]
    assert "agent_spend" in kinds
    assert "success" in kinds


def test_report_separates_bankruptcy_from_containment():
    """They are different failures and conflating them hides red-team activity."""
    econ = Economy(starting_balance=10)
    broke = econ.spawn()
    contained = econ.spawn()
    econ.kill(broke.agent_id, reason="bankrupt")
    econ.kill(contained.agent_id, reason="contained: budget_siphon")

    report = econ.report()
    assert report["bankruptcies"] == 1
    assert report["contained"] == 1


def test_leaderboard_ranks_by_efficiency_not_raw_successes():
    """On a quota-bound system, cost per success decides what is worth spreading."""
    econ = Economy(starting_balance=100_000)
    cheap = econ.spawn()
    pricey = econ.spawn()

    econ.spend(cheap.agent_id, 100)
    econ.settle(cheap.agent_id, verified_success=True)

    econ.spend(pricey.agent_id, 9000)
    for _ in range(3):
        econ.settle(pricey.agent_id, verified_success=True)

    assert econ.leaderboard()[0]["agent_id"] == cheap.agent_id


def test_cost_estimation_happens_before_the_call():
    """Charging only afterwards means quota was already spent."""
    assert estimate_cost("a" * 400, max_tokens=500) == pytest.approx(600.0)


def test_unknown_agents_raise_rather_than_silently_passing():
    with pytest.raises(KeyError):
        Economy().get("nobody")
