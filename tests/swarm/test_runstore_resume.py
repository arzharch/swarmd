"""A paused run resumes without buying anything twice.

The pacer parks a run for hours when a session's ration is spent. A pause that
long crosses laptop sleeps, deploys and Ctrl-C, so "paused" only means anything
if the run survives the process. These tests hold that line by COUNTING
PROVIDER CALLS: a resume that re-synthesizes the criterion still produces a
correct answer, which is exactly why correctness assertions cannot catch it.
"""

from __future__ import annotations

import json

import pytest

from swarmd.swarm.criteria import Criterion
from swarmd.swarm.run import SwarmRun
from swarmd.swarm.runstore import (
    IncompatibleRunState,
    RunState,
    RunStore,
)
from swarmd.task import Checkpoint
from tests.swarm.test_run import ScriptedProvider

TASK = "summarise the source records"


class CountingProvider(ScriptedProvider):
    """ScriptedProvider that separates synthesis calls from worker calls.

    The counts are the assertion. A resumed run that re-plans looks identical
    in its output and differs only here.
    """

    def __init__(self, **kw):
        super().__init__(**kw)
        self.criterion_calls = 0
        self.plan_calls = 0
        self.worker_calls = 0

    async def complete(self, request):
        prompt = request.prompt
        if "checks" in prompt and "matching this schema" in prompt:
            self.criterion_calls += 1
        elif "matching this schema" in prompt:
            self.plan_calls += 1
        elif "STEP:" in prompt:
            self.worker_calls += 1
        return await super().complete(request)


# --- the store ---------------------------------------------------------------


def test_a_run_document_round_trips(tmp_path):
    store = RunStore(tmp_path)
    state = RunState(
        run_id="run-1", task=TASK, profile="smoke", agents=4,
        criterion_hash="abc", drafts={"n": ["a", "b"]},
        balances={"a0001": 1500.0}, contained=["a0002"],
    )
    store.save(state)

    back = store.load("run-1")
    assert back is not None
    assert back.drafts == {"n": ["a", "b"]}
    assert back.balances == {"a0001": 1500.0}
    assert back.contained == ["a0002"]


def test_a_document_from_another_build_is_refused(tmp_path):
    """A resume that half-reads an old shape produces a run that looks correct
    and is not, which is worse than starting over knowingly."""
    store = RunStore(tmp_path)
    (tmp_path / "run-old.json").write_text(
        json.dumps({"run_id": "run-old", "schema_version": 0}), encoding="utf-8"
    )
    with pytest.raises(IncompatibleRunState):
        store.load("run-old")


def test_an_unwritable_store_degrades_rather_than_failing_the_run(tmp_path):
    """Losing durability must not cost the run that was already paid for."""
    store = RunStore(tmp_path / "file-not-a-dir")
    (tmp_path / "file-not-a-dir").write_text("x", encoding="utf-8")
    store.save(RunState(run_id="run-1", task=TASK, profile="smoke"))  # no raise


def test_checkpoints_survive_the_document(tmp_path):
    store = RunStore(tmp_path)
    state = RunState(run_id="run-1", task=TASK, profile="smoke")
    state.remember(
        Checkpoint(task_id="n", agent_id="a0001", completed_steps=["generate:1"])
    )
    store.save(state)

    restored = store.load("run-1")
    assert restored is not None
    checkpoint = restored.checkpoint_for("a0001")
    assert checkpoint is not None
    assert checkpoint.completed_steps == ["generate:1"]


# --- resume buys nothing twice ------------------------------------------------


async def test_a_resumed_run_does_not_re_synthesize_the_criterion(tmp_path):
    """N proposer calls, and the criterion is content-addressed.

    Re-deriving it would also grade the second half of a run against a target
    the first half never saw.
    """
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)
    after_first = provider.criterion_calls
    assert after_first > 0, "the first run should have synthesized a criterion"

    second = SwarmRun.resume(first.run_id, provider, store=store)
    await second.run(TASK)

    assert provider.criterion_calls == after_first, (
        "the resumed run re-synthesized a criterion it already owned"
    )


async def test_a_resumed_run_does_not_re_plan(tmp_path):
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)
    after_first = provider.plan_calls
    assert after_first > 0, "the first run should have planned"

    second = SwarmRun.resume(first.run_id, provider, store=store)
    await second.run(TASK)

    assert provider.plan_calls == after_first, "the resumed run re-planned"


async def test_a_run_interrupted_mid_execution_finishes_the_rest_and_no_more(tmp_path):
    """The case the pacer actually produces, and the one the tests above cannot
    reach: a run parked partway, with some nodes done and some never started.

    Resuming a FINISHED run skips everything, so it would pass even if the
    resume path re-ran nodes. This one interrupts execution, then asserts both
    halves of the contract -- the finished nodes cost nothing, and the
    unfinished ones actually run.
    """
    store = RunStore(tmp_path)

    class DiesPartway(CountingProvider):
        """Fails once enough nodes are done to leave real work behind."""

        limit = 1

        async def complete(self, request):
            if "STEP:" in request.prompt and self.worker_calls >= self.limit:
                raise KeyboardInterrupt("pretend the process was killed here")
            return await super().complete(request)

    provider = DiesPartway()
    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    with pytest.raises(KeyboardInterrupt):
        await first.run(TASK)

    state = store.load(first.run_id)
    assert state is not None
    assert state.criterion, "the criterion was not persisted before the interrupt"
    assert state.plan, "the plan was not persisted before the interrupt"
    done_before = state.finished_nodes
    planned = {n["name"] for n in (state.plan or {}).get("nodes", [])}
    assert done_before, "nothing finished, so there is no reuse to prove"
    assert planned - done_before, "nothing was left over, so there is no work to prove"

    survivor = CountingProvider()
    second = SwarmRun.resume(first.run_id, survivor, store=store)
    result = await second.run(TASK)

    assert survivor.criterion_calls == 0, "re-synthesized a stored criterion"
    assert survivor.plan_calls == 0, "re-planned a stored plan"
    assert survivor.worker_calls > 0, "did no work, so the resume stalled"
    # Every node the interrupted run had already finished is still in the
    # report: a resume that dropped them would return a partial answer while
    # claiming the run completed.
    assert done_before <= {r.node for r in result.results}


async def test_a_restored_result_keeps_its_full_output(tmp_path):
    """The report's `to_dict` truncates output to a 280-character preview.

    Restoring from that form would give a resumed run a different
    `integrity_hash` than the same run uninterrupted -- and that hash is what
    the chaos gate compares, so every resume would read as a corruption.
    """
    from swarmd.swarm.criteria import Candidate
    from swarmd.swarm.worker import WorkerResult

    long_output = "x" * 4000
    original = WorkerResult(
        agent_id="a0001",
        node="gather",
        candidate=Candidate(output=long_output, artifacts={"k": 1}, source="reply"),
        passed=True,
    )
    restored = WorkerResult.from_state(
        json.loads(json.dumps(original.to_state()))
    )

    assert restored.candidate.output == long_output
    assert restored.candidate.artifacts == {"k": 1}
    assert restored.candidate.source == "reply"


async def test_an_interrupted_run_reports_the_hash_it_would_have(tmp_path):
    """End to end: interrupt, resume, and compare against the uninterrupted run.

    Equal hashes mean the resume returned the same work, not merely a run that
    finished.
    """
    store = RunStore(tmp_path)

    clean = SwarmRun(CountingProvider(), profile="smoke", agents=4, store=store)
    expected = (await clean.run(TASK)).integrity_hash()

    class DiesPartway(CountingProvider):
        async def complete(self, request):
            if "STEP:" in request.prompt and self.worker_calls >= 1:
                raise KeyboardInterrupt("killed")
            return await super().complete(request)

    broken = SwarmRun(DiesPartway(), profile="smoke", agents=4, store=store)
    with pytest.raises(KeyboardInterrupt):
        await broken.run(TASK)

    resumed = SwarmRun.resume(broken.run_id, CountingProvider(), store=store)
    assert (await resumed.run(TASK)).integrity_hash() == expected


async def test_a_resumed_run_reuses_the_batch_drafts(tmp_path):
    """One provider call per node bought K variants. The most expensive thing
    a crash can lose."""
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)
    assert first.state.drafts, "drafts were never persisted"
    after_first = provider.worker_calls

    second = SwarmRun.resume(first.run_id, provider, store=store)
    await second.run(TASK)

    assert provider.worker_calls == after_first, (
        "the resumed run re-generated batches it had already stored"
    )


async def test_a_resumed_run_does_not_re_run_finished_nodes(tmp_path):
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    result = await first.run(TASK)
    finished = {r.node for r in result.results}
    assert finished

    second = SwarmRun.resume(first.run_id, provider, store=store)
    assert second.state.finished_nodes == finished


async def test_a_resumed_run_keeps_the_economy_it_had(tmp_path):
    """Fresh allowances on resume would hand a spent population a second budget."""
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)
    balances = dict(first.state.balances)
    assert balances

    second = SwarmRun.resume(first.run_id, provider, store=store)
    for agent_id, balance in balances.items():
        assert second.economy.get(agent_id).balance == pytest.approx(balance)


async def test_a_resumed_run_keeps_the_contained_set(tmp_path):
    """Forgetting it un-contains a rogue the red-team already stopped."""
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(
        provider, profile="smoke", agents=4, store=store, seed_rogues="all"
    )
    await first.run(TASK)
    contained = set(first.redteam.contained_agents)
    assert contained, "the seeded run contained nobody"

    second = SwarmRun.resume(first.run_id, provider, store=store)
    assert set(second.redteam.contained_agents) == contained


async def test_a_restored_agent_id_cannot_collide_with_a_new_one(tmp_path):
    """`restore` is not `spawn`: the id counter must advance past what it read,
    or the resumed run hands a fresh agent a name already in use."""
    store = RunStore(tmp_path)
    provider = CountingProvider()

    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)

    second = SwarmRun.resume(first.run_id, provider, store=store)
    existing = set(second.economy._accounts)
    assert second.economy.spawn().agent_id not in existing


async def test_resuming_an_unknown_run_says_so(tmp_path):
    with pytest.raises(FileNotFoundError, match="no stored run"):
        SwarmRun.resume("run-nope", CountingProvider(), store=RunStore(tmp_path))


async def test_a_tampered_criterion_is_refused(tmp_path):
    """The hash is what makes the criterion the same target across a restart.

    A document whose criterion no longer matches its hash would grade the
    second half of a run against something the first half never saw, while the
    report still quoted the original hash.
    """
    store = RunStore(tmp_path)
    provider = CountingProvider()
    first = SwarmRun(provider, profile="smoke", agents=4, store=store)
    await first.run(TASK)

    state = store.load(first.run_id)
    assert state is not None and state.criterion
    state.criterion = Criterion.from_dict(
        {"description": "swapped", "checks": [{"kind": "output_nonempty",
                                               "params": {"min_chars": 1}}]}
    ).to_dict()
    store.save(state)

    resumed = SwarmRun.resume(first.run_id, provider, store=store)
    with pytest.raises(ValueError, match="does not match"):
        resumed.restored_criterion()


# --- retention ----------------------------------------------------------------


def test_finished_runs_age_out(tmp_path):
    """Each document carries every batch draft the run generated, so a store
    that only grows is a disk leak proportional to throughput."""
    import os
    import time

    store = RunStore(tmp_path)
    store.save(RunState(run_id="run-old", task=TASK, profile="smoke",
                        status="completed"))
    old = store.path_for("run-old")
    ancient = time.time() - 30 * 86400
    os.utime(old, (ancient, ancient))

    assert store.prune(older_than_s=14 * 86400) == 1
    assert not old.exists()


def test_a_parked_run_is_never_pruned_however_old(tmp_path):
    """Age is not evidence of abandonment: a ration pause plus a weekend looks
    exactly like an abandoned run, and deleting it throws away everything the
    run already paid for."""
    import os
    import time

    store = RunStore(tmp_path)
    store.save(RunState(run_id="run-parked", task=TASK, profile="smoke",
                        status="paused"))
    parked = store.path_for("run-parked")
    ancient = time.time() - 400 * 86400
    os.utime(parked, (ancient, ancient))

    assert store.prune(older_than_s=1.0) == 0
    assert parked.exists()


def test_a_recent_finished_run_survives(tmp_path):
    store = RunStore(tmp_path)
    store.save(RunState(run_id="run-new", task=TASK, profile="smoke",
                        status="completed"))
    assert store.prune(older_than_s=14 * 86400) == 0
