"""The run memo: pay for a criterion and a plan once, not once per asking.

Every run opens with the same serial, always-paid head -- three proposer calls
to author a criterion, three more to author a plan -- and nothing else may
start until both have frozen. Ask the same question twice and the system buys
it twice. That is what this closes, and the tests below are written around the
two things that must stay true while it is closed:

  1. A memo NEVER supplies an answer. Workers still run against the real task
     and candidates are still graded by the frozen criterion. What is reused is
     the definition of done and the decomposition, nothing else.
  2. A memo is only ever reused when it is provably the same question and the
     run it came from actually completed. Everything else -- a paraphrase, a
     tampered file, a run that was interrupted, a criterion garbage now passes
     -- falls through to a cold run rather than degrading to "close enough".

There is no CONTINUOUS similarity anywhere in here on purpose. router/cache.py
records what happened last time this system matched machine-assembled prompts
by cosine: three different plan nodes measured 0.97 similar and one node's
answer was served to another, with the run reporting a high hit rate while
being wrong.

THE NEAR-MATCH TIER, below, is not that mistake in a new shape. It is keyed on
`generalise.abstract_fingerprint` -- an exact equality test on a deterministic,
discrete SHAPE, not a threshold on a continuous score -- and it never serves an
ANSWER either: a near hit rebinds a stored CRITERION and PLAN onto the new
task's literals and re-attacks the criterion before either may be trusted, so
the two invariants above hold for it exactly as they hold for the exact tier.
What a near hit adds is the harder case rule (1) always implied but the exact
key alone could never reach: "the same KIND of question, asked about something
else" is still not an answer being served, so it is still safe to reuse.
"""

from __future__ import annotations

import json

import pytest

from swarmd.router.providers import LLMResponse
from swarmd.swarm.generalise import abstract_fingerprint
from swarmd.swarm.memo import MEMO_MAX_AGE_S, MemoStore, key_for, normalise
from swarmd.swarm.run import PROFILES, SwarmRun
from swarmd.swarm.runstore import RunState, RunStore

TASK = "summarise the source records and report the count"

# The near-match tier's fixture pair: two different subjects, one shape of
# work. Neither carries a currency word ("dollars", "c") after its price, so
# both prices scan as bare NUMBER literals rather than MONEY -- what matters
# for `literal_map` is that the two tasks carry the SAME ORDERED SEQUENCE of
# literal kinds, not which kind each literal is.
PEN_TASK = "total cost of 3 pens at 1.25 each"
PENCIL_TASK = "total cost of 7 pencils at 0.40 each"

STRONG_CRITERION = {
    "description": "the step emits a structured summary artifact",
    "checks": [
        {"kind": "json_parses", "params": {"required_keys": ["summary", "count"]}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
    ],
}

WEAK_CRITERION = {
    "description": "something came out",
    "checks": [{"kind": "output_nonempty", "params": {"min_chars": 1}}],
}

# A criterion that is NOT weak -- `contains_all` is not a `TRIVIAL_KINDS`
# check, so nothing about it looks like the garbage `WEAK_CRITERION` exists to
# model. It is exactly the shape a proposer plausibly writes for PEN_TASK: a
# real content check that also happens to require the subject noun. Rebound
# onto PENCIL_TASK by `literal_map`/`rebind`, "pens" survives untouched --
# TERM is not one of `_LITERAL_KINDS` -- so no candidate about pencils, right
# or wrong, could ever satisfy it.
LEAK_CRITERION = {
    "description": "the output reports the pen order total and confirms it",
    "checks": [
        {"kind": "contains_all", "params": {"substrings": ["pens", "verified"]}},
    ],
}

PLAN = {
    "rationale": "read then verify",
    "nodes": [
        {"name": "gather", "instruction": "produce notes.json", "depends_on": []},
        {"name": "verify", "instruction": "produce report.json",
         "depends_on": ["gather"]},
    ],
}

GOOD_OUTPUT = json.dumps(
    {
        "summary": "loaded the source records, normalised fields, computed "
                   "statistics, and wrote them for downstream verification",
        "count": 128,
        "fields": ["identifier", "timestamp", "value"],
    }
)


class CountingProvider:
    """Answers by prompt shape and counts SYNTHESIS calls separately.

    The synthesis count is the number under test: a memo hit must take it to
    zero while the worker count stays above zero, because a run that stopped
    calling workers has stopped doing the task rather than saving anything.
    """

    name = "counting"

    def __init__(self) -> None:
        self.synthesis_calls = 0
        self.worker_calls = 0
        self.prompts: list[str] = []

    async def complete(self, request):
        self.prompts.append(request.prompt)
        if "matching this schema" in request.prompt and "checks" in request.prompt:
            self.synthesis_calls += 1
            text = json.dumps(STRONG_CRITERION)
        elif "matching this schema" in request.prompt:
            self.synthesis_calls += 1
            text = json.dumps(PLAN)
        else:
            self.worker_calls += 1
            text = GOOD_OUTPUT
        return LLMResponse(
            text=text, provider=self.name, model="counting-v1",
            latency_s=0.001, tokens_in=10, tokens_out=20,
        )


@pytest.fixture
def roots(tmp_path):
    """A run store and a memo store over one throwaway root, as in production."""
    return RunStore(tmp_path / "runs"), MemoStore(tmp_path / "runs" / "memos")


def build(provider, roots, **kw):
    runs, memo = roots
    events: list[dict] = []
    run = SwarmRun(
        provider, profile="smoke", store=runs, memo=memo,
        on_event=events.append, **kw,
    )
    return run, events


def kinds(events: list[dict]) -> list[str]:
    return [e["kind"] for e in events]


def frozen(payload: dict) -> tuple[dict, str]:
    """A criterion dict and its real content hash.

    Written out rather than faked because the store verifies the pair on every
    load -- a stored criterion that does not hash to its recorded hash is
    quarantined, which is the point of a separate test below.
    """
    from swarmd.swarm.criteria import Criterion

    criterion = Criterion.from_dict(payload)
    return criterion.to_dict(), criterion.content_hash()


# --- normalisation: exactly three operations, and no more --------------------


def test_normalisation_forgives_only_layout():
    """Whitespace and case cannot change what is being asked. Everything else
    can, so nothing else is touched."""
    assert normalise("  Count   the\nrecords ") == normalise("count the records")
    assert key_for("Count the records") == key_for("count   the records")


def test_normalisation_does_not_forgive_the_literals():
    """`3 pens at 1.25` and `3 pens at 12.5` are different questions, and so
    are `Compare A to B` and `Compare B to A`. A paraphrase MISSES, which costs
    six proposer calls -- far cheaper than grading one task against another
    task's definition of done."""
    assert key_for("total cost of 3 pens at 1.25") != key_for(
        "total cost of 3 pens at 12.5"
    )
    assert key_for("Compare A to B") != key_for("Compare B to A")
    assert key_for("summarise the records") != key_for("summarise the records twice")


# --- the saving -------------------------------------------------------------


async def test_the_second_run_of_a_task_buys_no_synthesis(roots):
    """The headline claim, stated in provider calls rather than in prose."""
    first_provider = CountingProvider()
    first, _ = build(first_provider, roots)
    original = await first.run(TASK)
    assert original.status == "completed"
    assert first_provider.synthesis_calls == PROFILES["smoke"].proposers * 2

    second_provider = CountingProvider()
    second, events = build(second_provider, roots)
    repeat = await second.run(TASK)

    assert second_provider.synthesis_calls == 0, "the criterion was re-bought"
    assert "criterion_memo_hit" in kinds(events)
    assert "plan_memo_hit" in kinds(events)
    assert repeat.criterion is not None and original.criterion is not None
    assert repeat.criterion.hash == original.criterion.hash
    assert repeat.plan is not None and original.plan is not None
    assert repeat.plan.content_hash() == original.plan.content_hash()


async def test_a_memo_hit_still_runs_the_workers(roots):
    """A memo reuses the QUESTION's definition, never its answer. A run that
    stopped calling workers would not be cheap, it would be fabricated."""
    await build(CountingProvider(), roots)[0].run(TASK)

    provider = CountingProvider()
    run, _ = build(provider, roots)
    repeat = await run.run(TASK)

    assert provider.worker_calls > 0, "no worker ran; the answer was replayed"
    assert repeat.results, "a memo hit produced no graded candidates"
    assert all(r.passed for r in repeat.results)


async def test_a_hit_names_the_run_it_inherited_from(roots):
    """`served_from` exists so an aggregate over runs -- pass rate, cost,
    time-to-first-worker -- can branch on it instead of averaging a
    memo-served run into runs that paid for their own synthesis."""
    first, _ = build(CountingProvider(), roots)
    await first.run(TASK)

    second, _ = build(CountingProvider(), roots)
    repeat = await second.run(TASK)

    assert repeat.served_from == f"memo:{first.run_id}"
    assert repeat.to_dict()["served_from"] == f"memo:{first.run_id}"


async def test_layout_differences_still_hit(roots):
    """The three normalisation operations, end to end: a heredoc's extra
    newline and a capitalised first word are not a different task."""
    await build(CountingProvider(), roots)[0].run(TASK)

    provider = CountingProvider()
    run, events = build(provider, roots)
    await run.run(f"  {TASK.capitalize()}   \n")

    assert provider.synthesis_calls == 0
    assert "criterion_memo_hit" in kinds(events)


async def test_a_different_task_misses(roots):
    """The boundary. Nothing fuzzier than an exact normalised key, so a
    genuinely different question pays for its own criterion."""
    await build(CountingProvider(), roots)[0].run(TASK)

    provider = CountingProvider()
    run, events = build(provider, roots)
    await run.run("draft a migration plan for the billing service")

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert "criterion_memo_hit" not in kinds(events)


async def test_the_saving_is_a_ledger_query_not_a_counter(roots):
    """ADR-007: no component keeps a running total, because a counter can drift
    from its rows and nothing notices. What the memo saved has to be summable
    from rows -- and it is counted in CALLS, since the avoided proposer call
    never named a provider or a model to price."""
    await build(CountingProvider(), roots)[0].run(TASK)

    run, _ = build(CountingProvider(), roots)
    await run.run(TASK)

    rows = [r for r in run.account.ledger.rows() if r.kind == "memo_hit"]
    assert [r.stage for r in rows] == ["criterion", "plan"]
    assert run.account.memo_savings() == {
        "hits": 2,
        "calls_avoided": PROFILES["smoke"].proposers * 2,
        "usd": 0.0,
    }
    assert run.account.total_cost() == 0.0, "a hit must not be billed as a call"


async def test_the_memo_survives_a_new_process(tmp_path):
    """Two stores over one root, the same shape as
    tests/router/test_journal_sharing.py: the memo has to outlive the process
    that wrote it, or it only ever helps the run that needed it least."""
    root = tmp_path / "runs"
    first, _ = build(CountingProvider(), (RunStore(root), MemoStore(root / "memos")))
    await first.run(TASK)

    provider = CountingProvider()
    # Fresh store objects, as a restarted pod would build.
    second, events = build(
        provider, (RunStore(root), MemoStore(root / "memos"))
    )
    await second.run(TASK)

    assert provider.synthesis_calls == 0
    assert "criterion_memo_hit" in kinds(events)


# --- invalidation -----------------------------------------------------------


async def test_a_run_that_did_not_complete_leaves_nothing_to_reuse(roots, monkeypatch):
    """The invalidation rule. A criterion frozen by a run that then errored is
    exactly the criterion not to inherit: unproven at best, and at worst the
    reason the run failed."""
    _, memo = roots
    run, _ = build(CountingProvider(), roots)

    async def explode(*_a, **_kw):
        raise RuntimeError("the executor fell over")

    monkeypatch.setattr(run, "_execute", explode)
    result = await run.run(TASK)
    assert result.status == "error"
    assert memo.get(key_for(TASK)) is None, "a failed run left a reusable memo"

    provider = CountingProvider()
    later, _ = build(provider, roots)
    await later.run(TASK)
    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2


async def test_an_unfinished_provenance_run_is_refused(roots):
    """A memo settles when its run ends normally; a control plane killed
    mid-run marks its documents "interrupted" without touching the memo. The
    run document is consulted for exactly that case."""
    runs, _ = roots
    first, _ = build(CountingProvider(), roots)
    await first.run(TASK)

    # The pod died after the run finished but before anything else: rewrite the
    # document the way `_mark_in_flight_interrupted` would have.
    state = runs.load(first.run_id)
    assert state is not None
    state.status = "interrupted"
    runs.save(state)

    provider = CountingProvider()
    second, events = build(provider, roots)
    await second.run(TASK)

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert "memo_refused" in kinds(events)


async def test_an_interrupted_provenance_run_does_not_poison_the_task_forever(roots):
    """One dead document must cost ONE refusal, not every refusal from here on.

    Before this test, a memo whose provenance run document was later marked
    interrupted was refused correctly but left on disk exactly as it was:
    `entry.status` still read "completed", so `MemoStore.remember` -- which
    exists to stop an unproven run from clobbering a proven memo -- read it as
    proven and refused to let the very run that just paid for a fresh
    criterion write one. Every run after the first would hit the same dead
    entry, pay full synthesis, and be refused again, forever, with no run ever
    managing to write a replacement. The fix is that a document-level refusal
    RETIRES the entry -- see `_memo_admission` -- the same way `_memo_reject`
    already retires a hash mismatch or a criterion that stopped attacking
    clean, so the next run finds nothing and gets to write its own."""
    runs, memo = roots
    first, _ = build(CountingProvider(), roots)
    await first.run(TASK)

    state = runs.load(first.run_id)
    assert state is not None
    state.status = "interrupted"
    runs.save(state)

    # The run that discovers the dead entry: refused, pays cold, but MUST be
    # left free to record its own memo once it completes.
    second_provider = CountingProvider()
    second, second_events = build(second_provider, roots)
    second_result = await second.run(TASK)

    assert second_result.status == "completed"
    assert second_provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert "memo_refused" in kinds(second_events)
    entry = memo.get(key_for(TASK))
    assert entry is not None, "the run that paid cold left nothing behind"
    assert entry.run_id == second.run_id, "the dead entry was never retired"

    # A third run must see THAT fresh memo, not pay again and not be refused.
    third_provider = CountingProvider()
    third, third_events = build(third_provider, roots)
    third_result = await third.run(TASK)

    assert third_provider.synthesis_calls == 0, "the poisoned entry was still blocking writes"
    assert "memo_refused" not in kinds(third_events)
    assert third_result.served_from == f"memo:{second.run_id}"


async def test_a_tampered_memo_is_quarantined_not_served(roots):
    """A document that does not hash to its own contents was changed by
    something that is not this code. Deleting it destroys the evidence; serving
    it hands a run a criterion nobody authored. So it is moved aside -- the
    same discipline SkillLibrary uses -- and the run proceeds cold."""
    _, memo = roots
    await build(CountingProvider(), roots)[0].run(TASK)

    path = memo.path_for(key_for(TASK))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["criterion"] = WEAK_CRITERION  # hash left untouched, as an edit would
    path.write_text(json.dumps(payload), encoding="utf-8")

    provider = CountingProvider()
    run, _ = build(provider, roots)
    await run.run(TASK)

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    quarantined = list((memo.root / "quarantine").glob("*.json"))
    assert quarantined, "the tampered document was deleted rather than kept"
    assert json.loads(quarantined[0].read_text(encoding="utf-8"))["criterion"] == (
        WEAK_CRITERION
    ), "the evidence was not preserved verbatim"


async def test_a_criterion_that_garbage_now_passes_is_refused(roots):
    """RE-ATTACKED on every hit, against THIS run's task text, with the check
    list running NOW. A criterion that survived an older attack is not
    inherited on that basis alone -- and refusing it deletes it, because a
    refusal that leaves it costs the same refusal on every future run."""
    runs, memo = roots
    provenance = RunState(
        run_id="run-weak", task=TASK, profile="smoke", status="completed"
    )
    runs.save(provenance)
    from swarmd.swarm.criteria import Criterion

    weak = Criterion.from_dict(WEAK_CRITERION)
    stored = memo.remember(
        task=TASK, run_id="run-weak",
        criterion=weak.to_dict(), criterion_hash=weak.content_hash(),
        plan=None,
    )
    assert stored is not None
    memo.record_outcome(task=TASK, run_id="run-weak", status="completed")

    provider = CountingProvider()
    run, events = build(provider, roots)
    result = await run.run(TASK)

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert result.criterion is not None
    assert result.criterion.hash != weak.content_hash()
    refusals = [e for e in events if e["kind"] == "memo_refused"]
    assert refusals and "garbage" in refusals[0]["reason"]


async def test_an_expired_memo_is_refused(roots):
    """Not a correctness bound -- the criterion is re-attacked anyway -- but a
    staleness one: check kinds get added and a month-old definition of done
    deserves to be re-asked."""
    _, memo = roots
    first, _ = build(CountingProvider(), roots)
    await first.run(TASK)

    entry = memo.get(key_for(TASK))
    assert entry is not None
    entry.created_ts -= MEMO_MAX_AGE_S * 2
    memo.put(entry)

    provider = CountingProvider()
    later, _ = build(provider, roots)
    await later.run(TASK)
    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2


async def test_a_resume_beats_a_memo(roots):
    """Half a run has already been graded against the stored criterion.
    Swapping in a memo's criterion mid-flight would grade the two halves of one
    run against different targets while the report quoted one hash for both."""
    runs, memo = roots
    first, _ = build(CountingProvider(), roots)
    await first.run(TASK)
    stored = runs.load(first.run_id)
    assert stored is not None

    # A different run, interrupted after freezing its own criterion.
    parked = RunState(
        run_id="run-parked", task=TASK, profile="smoke", status="interrupted",
        criterion=stored.criterion, criterion_hash=stored.criterion_hash,
        plan=stored.plan,
    )
    runs.save(parked)

    provider = CountingProvider()
    resumed = SwarmRun.resume(
        "run-parked", provider, store=runs, memo=memo, on_event=lambda e: None
    )
    events: list[dict] = []
    resumed.on_event = events.append
    await resumed.run(TASK)

    assert "criterion_memo_hit" not in kinds(events)
    assert "criterion_restored" in kinds(events)
    assert provider.synthesis_calls == 0


def test_an_eval_run_may_not_hold_a_memo(roots):
    """Same refusal, and the same reason, as the cache ban one line above it:
    an eval measures variance across repeats, and a memo removes the synthesis
    from every repeat after the first."""
    _, memo = roots
    with pytest.raises(ValueError, match="eval run cannot use the run memo"):
        SwarmRun(CountingProvider(), profile="eval", memo=memo)


# --- store mechanics --------------------------------------------------------


def test_a_completed_memo_is_not_overwritten_by_a_run_in_flight(roots):
    """Two runs of one task overlapping is ordinary for a service. Letting the
    later one replace a PROVEN memo with an unproven one would make the feature
    weakest exactly when it is used most."""
    _, memo = roots
    strong, strong_hash = frozen(STRONG_CRITERION)
    weak, weak_hash = frozen(WEAK_CRITERION)
    memo.remember(task=TASK, run_id="run-one", criterion=strong,
                  criterion_hash=strong_hash)
    memo.record_outcome(task=TASK, run_id="run-one", status="completed")

    memo.remember(task=TASK, run_id="run-two", criterion=weak,
                  criterion_hash=weak_hash)
    kept = memo.get(key_for(TASK))
    assert kept is not None and kept.run_id == "run-one"


def test_only_the_writing_run_may_settle_its_memo(roots):
    """A second run of the same task failing must not retire the first run's
    proven memo, and a second run succeeding must not certify a criterion it
    did not freeze."""
    _, memo = roots
    strong, strong_hash = frozen(STRONG_CRITERION)
    memo.remember(task=TASK, run_id="run-one", criterion=strong,
                  criterion_hash=strong_hash)
    memo.record_outcome(task=TASK, run_id="run-one", status="completed")

    memo.record_outcome(task=TASK, run_id="run-other", status="error")
    survivor = memo.get(key_for(TASK))
    assert survivor is not None and survivor.status == "completed"


def test_pruning_drops_only_what_is_past_its_life(roots):
    """A memo is a saving, never a record -- the ledger is the durable one
    (ADR-007) -- so losing one costs the next run of that task six proposer
    calls, which is what it cost before this existed."""
    _, memo = roots
    strong, strong_hash = frozen(STRONG_CRITERION)
    memo.remember(task=TASK, run_id="r", criterion=strong,
                  criterion_hash=strong_hash)
    memo.remember(task="a second question entirely", run_id="r",
                  criterion=strong, criterion_hash=strong_hash, now=1.0)

    assert memo.prune() == 1
    assert memo.get(key_for(TASK)) is not None
    assert memo.get(key_for("a second question entirely")) is None


# --- the fingerprint index -----------------------------------------------


def test_by_fingerprint_finds_a_same_shape_entry_and_excludes_its_own_key(roots):
    """The near tier's raw lookup, beneath any run: pencils finds pens'
    memo by shape, and a task's own exact key is never handed back to it --
    otherwise the near tier would "discover" the exact hit a second time as
    though it were independent evidence."""
    _, memo = roots
    strong, strong_hash = frozen(STRONG_CRITERION)
    memo.remember(task=PEN_TASK, run_id="r-pen", criterion=strong,
                  criterion_hash=strong_hash)
    memo.record_outcome(task=PEN_TASK, run_id="r-pen", status="completed")

    fingerprint = abstract_fingerprint(PENCIL_TASK)
    assert [m.task_key for m in memo.by_fingerprint(fingerprint)] == [
        key_for(PEN_TASK)
    ]
    assert memo.by_fingerprint(fingerprint, exclude_key=key_for(PEN_TASK)) == []


def test_by_fingerprint_ignores_a_different_shape(roots):
    """The other boundary: a genuinely different kind of question must not
    surface, whatever else is stored."""
    _, memo = roots
    strong, strong_hash = frozen(STRONG_CRITERION)
    memo.remember(task=PEN_TASK, run_id="r-pen", criterion=strong,
                  criterion_hash=strong_hash)
    memo.record_outcome(task=PEN_TASK, run_id="r-pen", status="completed")

    assert memo.by_fingerprint(abstract_fingerprint(TASK)) == []


# --- the near-match tier ---------------------------------------------------
#
# "If it learns a task, the next time a SIMILAR one comes in it should fire
# instantaneously." The exact tier above only ever fires on a repeat of the
# exact same question; these tests are the similar-task claim itself.


def test_pens_and_pencils_share_a_fingerprint_but_not_a_task(roots):
    """The precondition every test below relies on, stated as its own fact
    rather than left implicit: two different subjects, one shape of work,
    abstract to the same fingerprint (see `abstract_fingerprint`'s own
    docstring) while remaining two different exact keys."""
    assert abstract_fingerprint(PEN_TASK) == abstract_fingerprint(PENCIL_TASK)
    assert abstract_fingerprint(PEN_TASK) != abstract_fingerprint(TASK)
    assert key_for(PEN_TASK) != key_for(PENCIL_TASK)


async def test_a_similar_task_fires_the_near_tier_with_zero_synthesis(roots):
    """THE STATED GOAL, in provider calls. Pencils at a different price is a
    different exact task -- the exact tier still misses it -- but it is the
    same KIND of question, and that has to be enough to skip synthesis."""
    first, _ = build(CountingProvider(), roots)
    original = await first.run(PEN_TASK)
    assert original.status == "completed"

    provider = CountingProvider()
    run, events = build(provider, roots)
    repeat = await run.run(PENCIL_TASK)

    assert repeat.status == "completed"
    assert provider.synthesis_calls == 0, "a same-shape task paid for synthesis"
    assert "criterion_memo_revalidated" in kinds(events)
    assert "plan_memo_revalidated" in kinds(events)
    # Distinct from an EXACT hit's events -- an aggregate over runs has to be
    # able to tell "the exact question again" from "a similar question",
    # exactly as `served_from` lets it tell a memo-served run from a cold one.
    assert "criterion_memo_hit" not in kinds(events)
    assert "plan_memo_hit" not in kinds(events)
    assert repeat.served_from == f"memo:{first.run_id}"


async def test_a_near_hit_still_runs_the_workers_fresh(roots):
    """THE NON-NEGOTIABLE. A near hit rebinds a DEFINITION, never an ANSWER --
    replaying pens' stored result for a pencils task would be silently wrong
    in a way an exact hit never could be, because the numbers do not match."""
    await build(CountingProvider(), roots)[0].run(PEN_TASK)

    provider = CountingProvider()
    run, _ = build(provider, roots)
    repeat = await run.run(PENCIL_TASK)

    assert provider.worker_calls > 0, "no worker ran; the answer was replayed"
    assert repeat.results, "a near hit produced no graded candidates"
    assert all(r.passed for r in repeat.results)


async def test_a_near_hit_charges_calls_avoided_like_any_memo_hit(roots):
    """ADR-007 again: what a near hit saved has to be a ledger query too, and
    it is the same real avoided call whichever tier avoided it -- so it is
    summed together with an exact hit's, tagged `tier` for whoever wants to
    split them back apart."""
    await build(CountingProvider(), roots)[0].run(PEN_TASK)

    run, _ = build(CountingProvider(), roots)
    await run.run(PENCIL_TASK)

    rows = [r for r in run.account.ledger.rows() if r.kind == "memo_hit"]
    assert [r.stage for r in rows] == ["criterion", "plan"]
    assert all(r.detail.get("tier") == "near" for r in rows)
    assert run.account.memo_savings() == {
        "hits": 2,
        "calls_avoided": PROFILES["smoke"].proposers * 2,
        "usd": 0.0,
    }


async def test_an_unrelated_task_does_not_hit_the_pens_memo(roots):
    """The boundary the tier still owes: a genuinely different question --
    no shared shape, not just a different subject -- pays for its own
    synthesis exactly as it would with no memo at all. The literal fixture
    from the spec this tier was built against: "summarise the source
    records" must not fire the pens memo just because a memo exists."""
    await build(CountingProvider(), roots)[0].run(PEN_TASK)

    provider = CountingProvider()
    run, events = build(provider, roots)
    await run.run("summarise the source records")

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert "criterion_memo_revalidated" not in kinds(events)
    assert "criterion_memo_hit" not in kinds(events)


async def test_a_near_hit_whose_reattack_fails_is_refused_and_pays_in_full(roots):
    """NON-NEGOTIABLE: a rebound criterion that cannot survive re-attack must
    fall all the way through to synthesis, never degrade to "close enough".
    Built the same way `test_a_criterion_that_garbage_now_passes_is_refused`
    builds its exact-tier twin -- the failure (only degenerate checks) does
    not care which key found the memo."""
    runs, memo = roots
    runs.save(RunState(
        run_id="run-weak-pen", task=PEN_TASK, profile="smoke", status="completed"
    ))
    from swarmd.swarm.criteria import Criterion

    weak = Criterion.from_dict(WEAK_CRITERION)
    stored = memo.remember(
        task=PEN_TASK, run_id="run-weak-pen",
        criterion=weak.to_dict(), criterion_hash=weak.content_hash(),
    )
    assert stored is not None
    memo.record_outcome(task=PEN_TASK, run_id="run-weak-pen", status="completed")

    provider = CountingProvider()
    run, events = build(provider, roots)
    result = await run.run(PENCIL_TASK)

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2
    assert result.criterion is not None
    assert result.criterion.hash != weak.content_hash()
    assert "criterion_memo_revalidated" not in kinds(events)
    refusals = [e for e in events if e["kind"] == "memo_refused"]
    assert refusals and refusals[-1].get("tier") == "near"
    assert "re-attack" in refusals[-1]["reason"]


async def test_a_refused_near_candidate_is_kept_not_deleted(roots):
    """The exact tier deletes a refused memo because the refusal is a property
    of the DOCUMENT -- it fails identically for every future run of that same
    task. A near-tier refusal is a property of the PAIRING: the pens memo
    above is still exactly as good for pens, or for the next pencils-shaped
    task that happens to rebind cleanly, so it must survive being refused
    here."""
    runs, memo = roots
    runs.save(RunState(
        run_id="run-weak-pen", task=PEN_TASK, profile="smoke", status="completed"
    ))
    from swarmd.swarm.criteria import Criterion

    weak = Criterion.from_dict(WEAK_CRITERION)
    memo.remember(
        task=PEN_TASK, run_id="run-weak-pen",
        criterion=weak.to_dict(), criterion_hash=weak.content_hash(),
    )
    memo.record_outcome(task=PEN_TASK, run_id="run-weak-pen", status="completed")

    run, _ = build(CountingProvider(), roots)
    await run.run(PENCIL_TASK)

    assert memo.get(key_for(PEN_TASK)) is not None, (
        "a near-tier refusal deleted a memo that is still valid for its own task"
    )


async def test_a_criterion_that_still_names_the_source_subject_is_refused(roots):
    """THE ADVERSARIAL FINDING. `attack` only tries degenerate candidates, and
    none of them happen to spell out "pens" -- so a criterion that requires
    the literal string "pens" survives re-attack against a pencils task even
    though NO candidate about pencils, correct or not, could ever satisfy it.
    Unlike the garbage-now-passes case above, this is not a criterion attack
    can catch; it needs its own check, made of `leaked_subject_terms`.

    Left unfixed, every worker was graded FAILED against a real, correct
    pencils answer, the run still reported `completed`, and the ledger booked
    a savings credit for a criterion that could never be satisfied for that
    subject -- see the module docstring's `contains_all` leak.
    """
    runs, memo = roots
    runs.save(RunState(
        run_id="run-leak-pen", task=PEN_TASK, profile="smoke", status="completed"
    ))
    from swarmd.swarm.criteria import Criterion

    leaky = Criterion.from_dict(LEAK_CRITERION)
    memo.remember(
        task=PEN_TASK, run_id="run-leak-pen",
        criterion=leaky.to_dict(), criterion_hash=leaky.content_hash(),
    )
    memo.record_outcome(task=PEN_TASK, run_id="run-leak-pen", status="completed")

    provider = CountingProvider()
    run, events = build(provider, roots)
    result = await run.run(PENCIL_TASK)

    assert provider.synthesis_calls == PROFILES["smoke"].proposers * 2, (
        "a criterion that can never pass for this subject was trusted instead "
        "of falling through to synthesis"
    )
    assert "criterion_memo_revalidated" not in kinds(events)
    refusals = [e for e in events if e["kind"] == "memo_refused"]
    assert refusals and refusals[-1].get("tier") == "near"
    assert "pens" in refusals[-1]["reason"]
    # Kept, not deleted -- a leaked-subject refusal is a property of THIS
    # pairing, exactly like a failed-re-attack refusal, not of the document.
    assert memo.get(key_for(PEN_TASK)) is not None
    # The run still completes and grades a real, correct answer honestly --
    # it just paid for its own criterion rather than inheriting a broken one.
    assert result.status == "completed"
