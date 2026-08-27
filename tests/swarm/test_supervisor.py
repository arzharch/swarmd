"""The supervisor, in the swarm loop rather than only in the example.

PRD section 7 lists a supervisor under the flagship. One existed only in
`examples/leadops`, where the stages were known at startup, so the swarm --
whose stage names come from a plan generated minutes earlier -- had no
self-correction at all.

The properties worth defending, in order of how quietly they break:

  a patch that never reaches a worker is a report, not a correction
  a patch nobody measures is a constraint that accumulates forever
  a patch measured against a window that predates it flatters itself
"""

from __future__ import annotations

from dataclasses import dataclass, field

from swarmd.swarm.supervisor import WORKER_STAGE, Supervisor


@dataclass
class FakeResult:
    passed: bool
    failures: tuple[str, ...] = ()
    node: str = "solve"
    agent_id: str = "a1"
    attempts: int = 1
    skill_used: str = ""
    contained: bool = False
    thoughts: list = field(default_factory=list)


def _failures(kind: str, n: int) -> list[FakeResult]:
    return [FakeResult(False, (f"{kind}: detail {i}",)) for i in range(n)]


# --- when it acts -----------------------------------------------------------


def test_one_failure_is_a_hard_task_not_a_pattern():
    """Patching on the first failure encodes one task's quirk into every run."""
    sup = Supervisor()
    sup.observe(_failures("json_parses", 1))
    assert not sup.should_intervene()


def test_clustered_failures_trigger_an_intervention():
    sup = Supervisor()
    sup.observe(_failures("json_parses", 3))
    assert sup.should_intervene()


def test_failures_of_different_kinds_do_not_add_up_into_a_cluster():
    """Three unrelated failures are three hard tasks, not one missing rule."""
    sup = Supervisor()
    sup.observe(
        _failures("json_parses", 1)
        + _failures("numeric_range", 1)
        + _failures("regex_match", 1)
    )
    assert not sup.should_intervene()


def test_a_kind_with_no_guidance_yields_no_patch():
    """Better to name the gap than to append 'avoid failing' to the prompt."""
    sup = Supervisor()
    sup.observe(_failures("some_new_check", 5))
    assert sup.should_intervene()
    assert sup.propose() is None


# --- what it writes ---------------------------------------------------------


def test_the_patch_addresses_the_kind_that_actually_failed():
    sup = Supervisor(base_prompt="BASE")
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()

    assert patch is not None
    assert "BASE" in patch.text
    assert "markdown fence" in patch.text
    assert patch.kinds == ("json_parses",)


def test_the_patch_keeps_the_prompt_it_extends():
    """A patch that replaces the prompt loses everything already learned."""
    sup = Supervisor(base_prompt="original instructions")
    sup.observe(_failures("exit_code", 4))
    patch = sup.propose()
    assert patch is not None
    assert patch.text.startswith("original instructions")


def test_recording_a_patch_makes_it_the_new_base():
    sup = Supervisor(base_prompt="BASE")
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()
    assert patch is not None
    sup.record(patch)
    assert sup.base_prompt == patch.text
    assert sup.interventions == 1


def test_recording_resets_the_window():
    """Otherwise the next patch is proposed against failures already addressed."""
    sup = Supervisor()
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()
    assert patch is not None
    sup.record(patch)
    assert sup.taxonomy() == {}
    assert not sup.should_intervene()


# --- measurement ------------------------------------------------------------


def test_a_patch_that_helped_is_marked_effective():
    sup = Supervisor()
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()
    assert patch is not None
    sup.record(patch)

    sup.observe([FakeResult(True) for _ in range(4)])
    assert sup.measure(patch.patch_id) is True


def test_a_patch_that_did_not_help_is_marked_ineffective():
    sup = Supervisor()
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()
    assert patch is not None
    sup.record(patch)

    sup.observe(_failures("json_parses", 3))
    assert sup.measure(patch.patch_id) is False


def test_an_unmeasured_patch_is_not_effective_by_default():
    """The error here would go in the flattering direction."""
    sup = Supervisor()
    sup.observe(_failures("json_parses", 3))
    patch = sup.propose()
    assert patch is not None
    sup.record(patch)

    assert sup.measure(patch.patch_id) is None
    assert patch.effective is None


def test_rollback_restores_the_previous_prompt():
    sup = Supervisor(base_prompt="BASE")
    sup.observe(_failures("json_parses", 3))
    first = sup.propose()
    assert first is not None
    sup.record(first)

    sup.rollback(first.patch_id)
    assert sup.base_prompt == ""
    assert first.effective is False


def test_rolling_back_invalidates_later_patches_to_the_same_stage():
    """They were written against a prompt that no longer exists."""
    sup = Supervisor(base_prompt="BASE")
    sup.observe(_failures("json_parses", 3))
    first = sup.propose()
    assert first is not None
    sup.record(first)

    sup.observe(_failures("exit_code", 3))
    second = sup.propose()
    assert second is not None
    sup.record(second)

    sup.rollback(first.patch_id)
    assert second.effective is False


def test_measuring_an_unknown_patch_returns_none():
    assert Supervisor().measure("nope") is None


# --- inside a session -------------------------------------------------------


async def _session(library, supervisor, results, tasks=10):
    from swarmd.swarm.session import SwarmSession

    seen: list[str] = []

    async def run_factory(task, index, system_prompt=""):
        seen.append(system_prompt)

        class Result:
            status = "completed"
            duration_s = 0.1
            proposed_skills: list = []
            contained: list = []

            def __init__(self):
                self.results = results(index)

        result = Result()
        return result, {"cost": {"total_usd": 0.0}}

    session = SwarmSession(run_factory, library, supervisor=supervisor)
    report = await session.run([f"task {i}" for i in range(tasks)])
    return report, seen


async def test_the_patched_prompt_reaches_the_next_run(tmp_path):
    """The property the LeadOps supervisor had and the swarm did not."""
    from swarmd.swarm.skills import SkillLibrary

    sup = Supervisor()
    report, prompts = await _session(
        SkillLibrary(tmp_path / "s.json"),
        sup,
        lambda index: _failures("json_parses", 3),
    )

    assert sup.interventions >= 1
    assert any("RECURRING FAILURES" in p for p in prompts), (
        "the supervisor patched the prompt and no run ever saw it"
    )


async def test_a_session_without_a_supervisor_uses_the_stock_prompt(tmp_path):
    """A patched prompt is a confound; an arm must know which prompt it ran."""
    from swarmd.swarm.skills import SkillLibrary

    _, prompts = await _session(
        SkillLibrary(tmp_path / "s.json"),
        None,
        lambda index: _failures("json_parses", 3),
    )
    assert set(prompts) == {""}


async def test_a_useless_patch_is_rolled_back_by_the_session(tmp_path):
    """Failures that keep coming after a patch mean the hypothesis was wrong."""
    from swarmd.swarm.skills import SkillLibrary

    sup = Supervisor()
    report, _ = await _session(
        SkillLibrary(tmp_path / "s.json"),
        sup,
        lambda index: _failures("json_parses", 3),
    )

    rolled = [s for s in report.supervisions if s.get("rolled_back")]
    assert rolled, "a patch that never helped was kept anyway"


async def test_supervision_is_recorded_in_the_session_report(tmp_path):
    from swarmd.swarm.skills import SkillLibrary

    sup = Supervisor()
    report, _ = await _session(
        SkillLibrary(tmp_path / "s.json"),
        sup,
        lambda index: _failures("json_parses", 3),
    )
    assert report.supervisions
    assert report.to_dict()["supervisions"]


async def test_a_healthy_session_produces_no_patches(tmp_path):
    """Nothing to correct means nothing to change."""
    from swarmd.swarm.skills import SkillLibrary

    sup = Supervisor()
    report, prompts = await _session(
        SkillLibrary(tmp_path / "s.json"),
        sup,
        lambda index: [FakeResult(True) for _ in range(3)],
    )

    assert sup.interventions == 0
    assert all(not s.get("intervened") for s in report.supervisions)


async def test_the_prompt_version_history_records_the_patch(tmp_path):
    """Auditable and revertible, through the same consolidator every other
    prompt change goes through."""
    from swarmd.swarm.session import SwarmSession
    from swarmd.swarm.skills import SkillLibrary

    sup = Supervisor()

    async def run_factory(task, index, system_prompt=""):
        class Result:
            status = "completed"
            duration_s = 0.1
            proposed_skills: list = []
            contained: list = []
            results = _failures("json_parses", 3)

        return Result(), {"cost": {"total_usd": 0.0}}

    session = SwarmSession(
        run_factory, SkillLibrary(tmp_path / "s.json"), supervisor=sup
    )
    await session.run([f"t{i}" for i in range(5)])

    history = session.consolidator.prompts[WORKER_STAGE]
    assert len(history.versions) > 1
