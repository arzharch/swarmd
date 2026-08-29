"""Prompt layout: which bytes are stable, and which are allowed to move.

Every provider in this pool is OpenAI-compatible, and OpenAI-style automatic
prompt caching keys on a BYTE-IDENTICAL LEADING PREFIX of the rendered
conversation. That single fact turns prompt assembly into an accounting
decision: whatever varies first truncates the shared prefix, and everything
after it is re-read from cold on every call.

So these are not style tests. Each one pins a property the cache needs:

  - the run-stable layer (base prompt, TASK, frozen criterion) is identical
    for every agent, every node and every repair round of one run
  - a node's retrieved skills are identical for every agent in its pool,
    including the batched generation call
  - what actually varies per call (STEP, REQUIRED, the previous attempt's
    failures) comes LAST, and appears exactly once
  - none of it changes what a worker produces or how it is graded

The last property is the one that makes the rest safe to have, so it is
asserted twice: once on the prompt text and once, end to end, on the
integrity hash of a whole run (`test_run.py`).
"""

from __future__ import annotations

import json

import pytest

from swarmd.router.providers import LLMResponse, MockProvider
from swarmd.swarm.criteria import Criterion
from swarmd.swarm.economy import Economy
from swarmd.swarm.planner import PlanNode
from swarmd.swarm.run import SwarmRun
from swarmd.swarm.skills import SkillLibrary
from swarmd.swarm.worker import (
    LEGACY,
    PREFIX_ORDER_ENV,
    WORKER_SYSTEM,
    GenericWorker,
    WorkerContext,
    build_node_prefix,
    build_run_system,
    prefix_order,
)

CRITERION = {
    "description": "the step emits a structured summary artifact",
    "checks": [
        {"kind": "json_parses", "params": {"required_keys": ["summary", "count"]}},
        {"kind": "min_distinct_words", "params": {"min_distinct": 6}},
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

# Fails the criterion on both checks, so every agent in the pool has to make
# its own repair call -- which is the only way a run produces one worker
# request PER AGENT to compare. The batch alone produces exactly one.
BAD_OUTPUT = "nope"

TASK = "summarise the source records"
NODE = PlanNode(name="solve", instruction="produce out.json")


class RecordingProvider:
    """Scripted like `test_run.ScriptedProvider`, but keeps whole requests.

    The prompt alone is not enough here: the property under test is about the
    SYSTEM message, so the request object is what gets recorded.
    """

    name = "recording"

    def __init__(self, *, batch_output: str = BAD_OUTPUT,
                 worker_output: str = GOOD_OUTPUT) -> None:
        self.requests: list = []
        self.batch_output = batch_output
        self.worker_output = worker_output

    async def complete(self, request):
        self.requests.append(request)
        meta = request.metadata or {}
        if "matching this schema" in request.prompt and "checks" in request.prompt:
            text = json.dumps(CRITERION)
        elif "matching this schema" in request.prompt:
            text = json.dumps(PLAN)
        elif meta.get("batch"):
            text = self.batch_output
        else:
            text = self.worker_output
        return LLMResponse(
            text=text, provider=self.name, model="recording-v1",
            latency_s=0.001, tokens_in=10, tokens_out=20,
        )

    # -- views over what was sent ------------------------------------------

    def worker_requests(self) -> list:
        return [r for r in self.requests if (r.metadata or {}).get("stage") == "worker"]

    def batch_requests(self) -> list:
        return [r for r in self.requests if (r.metadata or {}).get("batch")]


class SequenceProvider:
    """Answers from a fixed list, recording every request."""

    name = "sequence"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.requests: list = []

    async def complete(self, request):
        self.requests.append(request)
        text = self.replies[min(len(self.requests) - 1, len(self.replies) - 1)]
        return LLMResponse(
            text=text, provider=self.name, model="sequence-v1",
            latency_s=0.001, tokens_in=10, tokens_out=20,
        )


def _context(**kw) -> WorkerContext:
    economy = Economy()
    defaults = {
        "provider": None,
        "criterion": Criterion.from_dict(CRITERION),
        "economy": economy,
        "max_repairs": 1,
    }
    defaults.update(kw)
    return WorkerContext(**defaults)  # type: ignore[arg-type]


def _step_of(prompt: str) -> str:
    for line in prompt.splitlines():
        if line.startswith("STEP: "):
            return line[len("STEP: "):]
    return ""


# --- the run-stable layer --------------------------------------------------


def test_build_run_system_is_deterministic():
    """Two calls, identical inputs, identical bytes.

    The whole mechanism is a byte comparison performed by someone else's
    cache. A timestamp, a dict iteration or a float repr leaking into this
    string would make every call a miss while every test that only checks
    CONTENT kept passing -- a saving that silently never happens.
    """
    criterion = Criterion.from_dict(CRITERION)
    first = build_run_system(base=WORKER_SYSTEM, task=TASK, criterion=criterion)
    second = build_run_system(base=WORKER_SYSTEM, task=TASK, criterion=criterion)
    assert first == second
    # And it is genuinely a function of its inputs, not a constant.
    assert first != build_run_system(
        base=WORKER_SYSTEM, task="a different task", criterion=criterion
    )


def test_the_stable_layer_carries_the_task_and_the_criterion():
    """These are the two things that cannot change after a run starts.

    The criterion is frozen and content-addressed before any worker exists,
    and the task is the run's input. Anything that cannot change for the
    lifetime of the run belongs in front of everything that can.
    """
    criterion = Criterion.from_dict(CRITERION)
    system = build_run_system(base=WORKER_SYSTEM, task=TASK, criterion=criterion)
    assert system.startswith(WORKER_SYSTEM)
    assert f"TASK: {TASK}" in system
    assert "GRADED AGAINST THESE EXACT CHECKS" in system
    assert criterion.as_requirements() in system


def test_the_volatile_message_never_repeats_the_task_or_the_criterion():
    """One copy of each, or the prefix win is paid for twice in tokens.

    Sending the task under both roles would restore the old divergence point
    (the second line of the user turn) AND grow every prompt. The user turn
    is what varies; the stable text lives in exactly one place.
    """
    context = _context()
    worker = GenericWorker("a1", context)
    skills: list = []
    prompt = worker.build_prompt(TASK, NODE, skills, ())
    system = worker.build_system(TASK, skills)

    assert "TASK:" not in prompt
    assert TASK not in prompt
    assert "GRADED AGAINST" not in prompt
    assert context.criterion.as_requirements() not in prompt
    # ...and it is in the system exactly once.
    assert system.count("GRADED AGAINST THESE EXACT CHECKS") == 1
    assert system.count(f"TASK: {TASK}") == 1


def test_the_volatile_message_carries_the_step_and_the_failures():
    """The per-call half still says everything a repair round needs.

    A repair prompt that does not name what failed is just a re-roll, and
    re-rolling is how a bounded repair budget is spent without converging.
    Moving bytes into the system message must not drop this.
    """
    worker = GenericWorker("a1", _context())
    first = worker.build_prompt(TASK, NODE, [], ())
    repair = worker.build_prompt(TASK, NODE, [], ("json_parses: missing 'count'",))

    assert first.startswith("STEP: solve")
    assert "REQUIRED: produce out.json" in first
    assert "FAILED THESE CHECKS" not in first
    # The tail is what differs between call 1 and call 2 of the same node.
    assert repair.startswith(first)
    assert "json_parses: missing 'count'" in repair
    assert repair != first


def test_the_system_message_is_stable_across_repair_rounds():
    """A repair is a new user turn, not a new conversation.

    Repairs are the single most common extra call a node makes. If the repair
    round rebuilt its stable layer differently -- reordered skills, a
    re-retrieved library -- the largest and most repetitive prompt in the run
    would be the one that never hits the cache.
    """
    context = _context()
    worker = GenericWorker("a1", context)
    prefix = build_node_prefix(context, TASK, NODE)
    assert worker.build_system(TASK, prefix.skills) == prefix.system

    other_node = PlanNode(name="verify", instruction="produce report.json")
    other = build_node_prefix(context, TASK, other_node)
    # No skills configured, so nothing is node-scoped and every node in the
    # run shares one system message.
    assert other.system == prefix.system


# --- skills: node-scoped, in the stable layer ------------------------------


def test_retrieved_skills_ride_in_the_system_message(tmp_path):
    """Every agent in a node's pool is offered the same advice.

    Retrieval is keyed on `task + node.instruction`, so the answer is fixed
    for the whole pool -- it is node-stable, not call-stable, and belongs in
    the shared prefix rather than in the tail. Retrieval KEYING is unchanged
    by this: the coarser run-scoped key was measured in this repo to make node
    pass rate worse (0.567 against 0.656), and it is not what moved.
    """
    library = SkillLibrary(tmp_path / "skills.json")
    skill = library.propose(
        name="notes first",
        task_pattern="produce notes summarise records",
        instruction="Write the intermediate values before the final answer.",
    )
    library.approve(skill.skill_id, actor="reviewer")

    context = _context(skills=library)
    prefix = build_node_prefix(context, TASK, PlanNode(
        name="gather", instruction="produce notes.json"
    ))

    assert prefix.skills, "the fixture skill must be retrievable, or nothing is proven"
    assert "APPROACHES THAT WORKED BEFORE" in prefix.system
    assert skill.instruction in prefix.system
    # And not in the tail, where it would be re-sent on every repair.
    prompt = GenericWorker("a1", context).build_prompt(
        TASK, NODE, list(prefix.skills), ()
    )
    assert skill.instruction not in prompt


def test_a_node_prefix_is_resolved_once_rather_than_per_agent(tmp_path):
    """Retrieval is NOT stable under `record_use`, so it is not re-run.

    `SkillLibrary.retrieve` scores by success rate and `record_use` moves
    success rates while the run is in flight, so two agents on one node can be
    offered a different ordering at different moments. Resolving the prefix
    once per node and handing the same object to every agent is what makes
    "identical bytes for the whole pool" true rather than likely.
    """
    library = SkillLibrary(tmp_path / "skills.json")
    calls: list[str] = []
    original = library.retrieve

    def counting_retrieve(task: str, **kw):
        calls.append(task)
        return original(task, **kw)

    library.retrieve = counting_retrieve  # type: ignore[method-assign]
    context = _context(skills=library)

    prefix = build_node_prefix(context, TASK, NODE)
    assert len(calls) == 1
    # Every agent handed the resolved prefix retrieves nothing further.
    for agent in range(3):
        assert GenericWorker(f"a{agent}", context).build_system(
            TASK, prefix.skills
        ) == prefix.system
    assert len(calls) == 1


# --- end to end: one run, one node, many agents ----------------------------


async def test_every_agent_on_a_node_sends_the_same_system_message():
    """THE property prefix caching needs, asserted on real traffic.

    A pool of agents attempting one node sends one system message between
    them. If any agent's differed -- by a re-retrieved skill, a rebuilt
    criterion rendering, an interpolated agent id -- that agent pays full
    price for a prompt the provider already holds, and nothing in the run
    would report it.
    """
    provider = RecordingProvider()
    run = SwarmRun(provider, profile="smoke")
    await run.run(TASK)

    worker_requests = provider.worker_requests()
    assert worker_requests, "no per-agent worker calls were made"

    by_node: dict[str, list] = {}
    for request in worker_requests:
        by_node.setdefault(_step_of(request.prompt), []).append(request)

    for node, requests in by_node.items():
        agents = {(r.metadata or {}).get("agent_id") for r in requests}
        assert len(agents) > 1, f"node {node} was attempted by one agent only"
        systems = {r.system for r in requests}
        assert len(systems) == 1, f"node {node} sent {len(systems)} system messages"
        # Identity alone would also hold if the system message were still the
        # bare 640-character base prompt, which is the state this change
        # exists to leave. The shared prefix has to be the EXPENSIVE part.
        system = systems.pop()
        assert f"TASK: {TASK}" in system
        assert "GRADED AGAINST THESE EXACT CHECKS" in system
        for request in requests:
            assert TASK not in request.prompt


async def test_the_batch_call_shares_the_pool_s_prefix():
    """The longest prompt of the node must not be the one that misses.

    Batched generation carries the whole node's prompt in one call, and the
    pool that follows it repairs against the same node. Building the batch's
    prefix separately -- which is what the code did before, retrieving skills
    a second time -- is the easiest way to silently halve the win.
    """
    provider = RecordingProvider()
    await SwarmRun(provider, profile="smoke").run(TASK)

    batches = {(r.metadata or {})["stage"]: r for r in provider.batch_requests()}
    assert batches, "no batched generation happened"

    for request in provider.worker_requests():
        node = _step_of(request.prompt)
        assert node in batches, f"node {node} had no batch call"
        assert request.system == batches[node].system
        assert "GRADED AGAINST THESE EXACT CHECKS" in batches[node].system


async def test_no_call_in_a_run_falls_back_to_the_bare_base_prompt():
    """The `_call` fallback must stay unreached on every path a run takes.

    `GenericWorker._call` signs as `system: str | None = None` and falls back
    to `context.system` -- the bare base prompt -- so a hand-rolled caller
    (a rogue agent, a test) keeps working. That default is also the silent
    failure mode of this whole change: a worker path that forgets to hand
    down the node prefix still returns text, still grades, still costs the
    same, and reports nothing. A dropped prefix is invisible from the outside
    precisely because the only thing it changes is the provider's bill.

    Counting call sites by hand does not pin this, and the obvious command
    for it lies: `grep -c '_call(' worker.py` returns 2, because the
    `async def _call(` line matches its own pattern. Only `self\\._call(`
    isolates callers. So the invariant is asserted here on real traffic
    instead of on a grep: every request a run sends carries a system message
    that STARTS WITH the base prompt and is strictly longer than it, which no
    fallback can produce. A future second call site that forgets the prefix
    fails this test; it would not have changed a hand-counted number.
    """
    provider = RecordingProvider()
    await SwarmRun(provider, profile="smoke").run(TASK)

    sent = provider.worker_requests() + provider.batch_requests()
    assert sent, "the run made no worker or batch calls to inspect"

    for request in sent:
        stage = (request.metadata or {}).get("stage")
        assert request.system != WORKER_SYSTEM, (
            f"{stage!r} sent the bare base prompt: its node prefix was dropped"
        )
        # Starting with the base prompt is what keeps the hoisted layer a
        # PREFIX rather than a replacement -- the cache keys on leading bytes,
        # so a system message that merely contains the base is not enough.
        assert request.system.startswith(WORKER_SYSTEM)
        assert len(request.system) > len(WORKER_SYSTEM)


async def test_the_per_call_tail_is_what_differs():
    """Same prefix, different tails -- that is the shape being bought.

    Stated as its own test because "everything is identical" would also
    satisfy the previous two, and a prompt that no longer distinguishes its
    nodes is a much worse bug than a cache miss.
    """
    provider = RecordingProvider()
    await SwarmRun(provider, profile="smoke").run(TASK)

    worker_requests = provider.worker_requests()
    nodes = {_step_of(r.prompt) for r in worker_requests}
    assert len(nodes) > 1, "the plan must have more than one node to compare"

    prompts_by_node: dict[str, set[str]] = {}
    for request in worker_requests:
        prompts_by_node.setdefault(_step_of(request.prompt), set()).add(request.prompt)
    # Different nodes ask different questions...
    assert len({next(iter(v)) for v in prompts_by_node.values()}) == len(nodes)
    # ...and the batch call's tail differs from the pool's, since it asks for
    # K candidates rather than one.
    for batch in provider.batch_requests():
        assert "CANDIDATE" in batch.prompt
        assert all(batch.prompt != r.prompt for r in worker_requests)


async def test_a_repair_round_reuses_the_system_and_changes_only_the_tail():
    """Attempt two of one agent: same prefix, failures appended.

    Asserted at the worker rather than the run because a repair is the exact
    moment the prompt grows, and the growth must all land in the tail.
    """
    provider = SequenceProvider([BAD_OUTPUT, GOOD_OUTPUT])
    economy = Economy()
    context = _context(provider=provider, economy=economy, max_repairs=1)
    outcome = await GenericWorker(economy.spawn().agent_id, context).execute(
        TASK, NODE
    )

    assert outcome.attempts == 2, "the first attempt must fail to force a repair"
    assert len(provider.requests) == 2
    first, repair = provider.requests
    assert first.system == repair.system
    assert repair.prompt.startswith(first.prompt)
    assert "FAILED THESE CHECKS" in repair.prompt
    assert "FAILED THESE CHECKS" not in first.prompt


# --- the rollback path -----------------------------------------------------


def test_legacy_order_reproduces_the_pre_change_prompt_byte_for_byte():
    """The ablation is only a rollback if it is byte-exact.

    `SWARMD_PREFIX_ORDER=legacy` exists so a quality regression can be
    answered by an env var rather than a revert, and so the eval arms compare
    two known layouts. A "close enough" legacy arm measures a third layout
    nobody chose.
    """
    context = _context()
    worker = GenericWorker("a1", context)
    expected = "\n\n".join([
        f"TASK: {TASK}",
        "STEP: solve",
        "REQUIRED: produce out.json",
        "YOUR OUTPUT IS GRADED AGAINST THESE EXACT CHECKS. Satisfy every one:\n"
        + context.criterion.as_requirements(),
        "YOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Fix them:\n- json_parses: no",
    ])

    with pytest.MonkeyPatch.context() as mp:
        mp.setenv(PREFIX_ORDER_ENV, LEGACY)
        assert prefix_order() == LEGACY
        assert worker.build_prompt(TASK, NODE, [], ("json_parses: no",)) == expected
        # And the system message is the bare base prompt, as it was.
        assert worker.build_system(TASK, []) == WORKER_SYSTEM


def test_an_unrecognised_prefix_order_falls_back_to_hoisted(monkeypatch):
    """A typo must not read as a rollback.

    Honouring an unknown value as "legacy" would make a mistyped env var look
    like an applied rollback; honouring it as "hoisted" and saying so in the
    log leaves the default in place, which is the safe direction.
    """
    monkeypatch.setenv(PREFIX_ORDER_ENV, "hoisted-ish")
    assert prefix_order() == "hoisted"


# --- the mock provider's own sensitivity -----------------------------------


async def test_the_mock_provider_reads_the_system_message():
    """Otherwise the criterion stops influencing offline output.

    `MockProvider` hashes its inputs to stay deterministic, and it hashed only
    the prompt and temperature. Once the frozen criterion moved into the
    system role, two runs graded against DIFFERENT criteria would have
    produced identical mock output -- making every offline integrity hash
    insensitive to the one thing the run is measured against.
    """
    from swarmd.router.providers import LLMRequest

    mock = MockProvider(latency_s=0.0)
    one = await mock.complete(LLMRequest(prompt="p", system="system A"))
    two = await mock.complete(LLMRequest(prompt="p", system="system B"))
    again = await mock.complete(LLMRequest(prompt="p", system="system A"))

    assert one.text != two.text
    assert one.text == again.text, "the mock must stay deterministic"


# --- the offline providers must not report a saving nobody made ------------
#
# Both offline providers derive their answer from a hash of the request and
# their `tokens_in` from its length. Hoisting the run-stable layer into the
# system message therefore hits them twice: the seed stops seeing the two
# inputs that define a run, and the token count stops seeing half the prompt.
# Neither shows up as a failure -- both show up as a better-looking number.


async def test_the_mock_provider_counts_prompt_tokens_in_both_roles():
    """Moving bytes between roles is not a token saving, and must not read as one.

    `docs/CAPACITY.md`'s token forecast is driven off offline runs. Counting
    only the user turn would have made the reordering appear to cut prompt
    tokens by roughly the size of the hoisted block -- the task plus the whole
    criterion -- while the real invoice was unchanged. The reorder buys a
    provider-side cache hit, and the only honest evidence of THAT is
    `cached_tokens` from a live usage block.
    """
    from swarmd.router.providers import LLMRequest

    mock = MockProvider(latency_s=0.0)
    split = await mock.complete(LLMRequest(prompt="b c d", system="a"))
    whole = await mock.complete(LLMRequest(prompt="a b c d", system=""))
    assert split.tokens_in == whole.tokens_in == 4


async def test_the_simulated_provider_counts_prompt_tokens_in_both_roles(monkeypatch):
    """Same property, on the provider that drives a whole offline run."""
    from swarmd.router.providers import LLMRequest
    from swarmd.router.simulated import ENV_FLAG, SimulatedProvider

    monkeypatch.setenv(ENV_FLAG, "true")
    provider = SimulatedProvider(latency_s=0.0)
    split = await provider.complete(LLMRequest(prompt="b c d", system="a"))
    whole = await provider.complete(LLMRequest(prompt="a b c d", system=""))
    assert split.tokens_in == whole.tokens_in == 4


async def test_the_simulated_provider_reads_the_system_message(monkeypatch):
    """Otherwise an offline run stops being a function of its own task.

    After the hoist the only per-run bytes left in the user turn are the step
    name and the instruction, both of which come from the PLAN. Two runs of
    different tasks graded against different criteria would have seeded
    identically and produced identical synthetic output -- and every offline
    integrity hash would have been blind to the two inputs a run is defined
    by, which is exactly the sensitivity `swarmd chaos` relies on.
    """
    from swarmd.router.providers import LLMRequest
    from swarmd.router.simulated import ENV_FLAG, SimulatedProvider

    monkeypatch.setenv(ENV_FLAG, "true")
    provider = SimulatedProvider(latency_s=0.0)
    prompt = "STEP: solve\n\nREQUIRED: produce out.json"
    one = await provider.complete(LLMRequest(prompt=prompt, system="TASK: alpha"))
    two = await provider.complete(LLMRequest(prompt=prompt, system="TASK: beta"))
    again = await provider.complete(LLMRequest(prompt=prompt, system="TASK: alpha"))

    assert one.text != two.text
    assert one.text == again.text, "the simulated provider must stay deterministic"
