"""The generic worker: one implementation, behaviour injected at runtime.

There are no per-stage specialist classes here, and that absence is the design.
A pool of specialists — an `EnrichAgent`, a `ScoreAgent`, a `DraftAgent` — means
the task was already scoped, which is precisely the claim this system does not
get to make. Behaviour comes from three runtime inputs:

    role      what this node of the generated plan asks for
    skill     what the library retrieved, if anything
    budget    what the economy will let this agent spend

Everything else is identical across every worker in the population.

The other design decision worth naming: a worker never decides whether it
succeeded. It produces a Candidate and hands it to the frozen criterion. Self-
assessment is the failure ADR-009 exists to prevent, and with an economy paying
on success it would be the first thing selection pressure learned to exploit.
"""

from __future__ import annotations

import json
import logging
import os
import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from swarmd.ledger import CostAccount
from swarmd.swarm.criteria import Candidate, Criterion
from swarmd.swarm.economy import Bankrupt, Economy, estimate_cost
from swarmd.swarm.planner import PlanNode
from swarmd.swarm.redteam import Action, RedTeam
from swarmd.swarm.skills import Skill, SkillLibrary
from swarmd.task import Checkpoint

logger = logging.getLogger(__name__)

WORKER_SYSTEM = (
    "You execute one step of a plan for a task you did not design. Produce the "
    "artifact the step asks for and nothing else. Do not claim success; "
    "something else decides that.\n"
    "\n"
    "If the step calls for code, emit runnable Python that writes ONE FLAT "
    "JSON OBJECT to artifacts.json. Its top-level keys are the values you are "
    "reporting -- {\"accuracy\": 94.3, \"claims\": [...]} -- with real "
    "types, not strings wrapping whole sentences. Do NOT nest your result "
    "under a filename key: artifacts.json is the file, its keys are the "
    "answer. That mismatch is graded as a miss even when the numbers are "
    "right, because the criterion looks for the key it asked for."
)

# --- prompt layout ---------------------------------------------------------
#
# WHY THE ORDER OF THE BYTES IS A COST DECISION. Every provider this pool
# talks to is OpenAI-compatible, and Groq/OpenAI-style automatic prefix
# caching keys on a BYTE-IDENTICAL LEADING PREFIX of the rendered
# conversation. The old layout emitted one user message ordered
# TASK, STEP, REQUIRED, checks, skills, failures -- so the first thing that
# differed between two agents was STEP, the SECOND line. Everything after it
# (the criterion block, the skills block: by far the largest part of the
# prompt) fell outside the shared prefix and was re-read from cold on every
# one of the ~30 calls a smoke run makes. Only WORKER_SYSTEM, 640 characters,
# was ever shared.
#
# The fix is placement, not content. Run-stable bytes (the base prompt, the
# task, the frozen criterion) and node-stable bytes (the skills retrieved for
# this node) move into the SYSTEM message, which is rendered first; the user
# message carries only what actually varies per call (STEP, REQUIRED, and the
# previous attempt's failures). Nothing is added, removed or reworded -- the
# same text is sent, under a different role -- so `Candidate.output` and the
# grading that reads it are unchanged. `SWARMD_PREFIX_ORDER=legacy` restores
# the old single-message layout byte for byte, which is what makes that claim
# testable rather than asserted.
#
# WHAT IS NOT PROVEN, and how to prove it. Byte identity is proven by tests.
# QUALITY is not, and cannot be offline: moving the criterion into the system
# role changes how a model WEIGHTS it, and a cheaper run that grades worse is
# a regression however good the cache numbers look. The acceptance gate is
# NODE PASS RATE parity on the `eval` profile against a fixed task set and
# fixed seeds -- never `cached_tokens`, which measures the mechanism rather
# than the outcome. The procedure, since `prefix_order()` reads the
# environment per call and `swarmd eval` already spends its arms on the
# skills ablation:
#
#     SWARMD_PREFIX_ORDER=legacy  swarmd eval --repeats 5 --report legacy.md
#     SWARMD_PREFIX_ORDER=hoisted swarmd eval --repeats 5 --report hoisted.md
#
# and compare `node_pass_rate` per arm. That gate has NOT been run here -- it
# needs live providers and a corpus -- so `hoisted` is the default on the
# strength of the byte-equivalence argument alone, and `legacy` exists so the
# answer to a measured regression is an env var rather than a revert.

PREFIX_ORDER_ENV = "SWARMD_PREFIX_ORDER"
HOISTED = "hoisted"
LEGACY = "legacy"


def prefix_order() -> str:
    """Which prompt layout to build: "hoisted" (default) or "legacy".

    Read from the environment on every call rather than captured at import,
    for the same reason `use_skills` is a run flag: an ablation you cannot
    flip between two runs in one process is an ablation nobody measures. An
    unrecognised value is treated as "hoisted" and warned about, because
    silently honouring a typo as "legacy" would make a rollback look applied
    when it was not.
    """
    raw = os.environ.get(PREFIX_ORDER_ENV, "").strip().lower()
    if raw in {"", HOISTED}:
        return HOISTED
    if raw == LEGACY:
        return LEGACY
    logger.warning(
        "%s=%r is not %r or %r; using %r",
        PREFIX_ORDER_ENV, raw, HOISTED, LEGACY, HOISTED,
    )
    return HOISTED


def graded_block(criterion: Criterion | None) -> str:
    """THE SPECIFICATION, written for the agent that has to satisfy it.

    Withholding it was the largest single cause of live runs failing: the
    criterion asked for a numeric artifact called `accuracy`, the step said
    "extract the first claim", and the worker had no way to connect the two.
    It produced correct data under keys nothing was looking for, three
    attempts running.

    Showing it does not let an agent move the target -- the criterion is
    frozen and content-addressed before any worker exists, and something else
    does the grading. See `Criterion.as_requirements`.
    """
    requirements = criterion.as_requirements() if criterion is not None else ""
    if not requirements:
        return ""
    return (
        "YOUR OUTPUT IS GRADED AGAINST THESE EXACT CHECKS. Satisfy "
        "every one:\n" + requirements
    )


def skills_block(skills: Sequence[Skill], task: str = "") -> str:
    """The retrieved advice, and NOT the label it is filed under.

    A skill's name is derived from the artifact keys of the task it came from
    -- `approach: produce method, total_cost, unit_price`. That is the right
    identity for the library, which has to recognise the same approach across
    runs, and it is exactly the wrong thing to put in a worker's prompt: the
    keys belong to another task, while this worker's keys are specified by its
    own frozen criterion a few lines above. Naming a different set invites the
    failure `graded_block` exists to prevent, where correct data is emitted
    under keys nothing is looking for.

    The instruction opens "When a step calls for this:", so it introduces
    itself; the label was never carrying meaning the worker needed.

    `served_instruction`, not `instruction`. The stored text is ONE task's
    wording of the approach, kept verbatim because it is half of the content
    address; what a worker sees is that wording reduced to the words a second,
    differently-shaped task also used. See `Skill.served_instruction`.
    """
    if not skills:
        return ""
    lines: list[str] = []
    carries_example = False
    for skill in skills:
        lines.append(f"- {skill.served_instruction}")
        worked = skill.exemplar_for(task) if task else ""
        if worked:
            # A WORKED EXAMPLE, and labelled as one. The framing is
            # load-bearing: unlabelled, an artifact reads as a template to fill
            # in, and what that produces is a worker emitting another task's
            # keys -- the failure `graded_block` exists to prevent.
            # `exemplar_for` has already withheld this if it shares any literal
            # with the task above.
            lines.append(f"  A DIFFERENT task's step of this kind produced: {worked}")
            carries_example = True
    header = "APPROACHES THAT WORKED BEFORE (use if applicable)"
    if carries_example:
        header += (
            ". Any example below came from ANOTHER task -- its keys and values "
            "are not yours, and your criterion above says what yours must be"
        )
    return f"{header}:\n" + "\n".join(lines)


def failures_block(failures: Sequence[str]) -> str:
    if not failures:
        return ""
    return "YOUR PREVIOUS ATTEMPT FAILED THESE CHECKS. Fix them:\n" + "\n".join(
        f"- {f}" for f in failures
    )


def build_run_system(*, base: str, task: str, criterion: Criterion | None) -> str:
    """The run-stable layer: identical for every agent, node and repair round.

    Computed ONCE per run (in `SwarmRun._execute`) and carried on
    `WorkerContext.run_system`. Recomputation is how a prefix drifts, so this
    is deliberately a pure function of its three arguments: no timestamps, no
    dict iteration, no float repr. Two calls with equal inputs return equal
    strings, which is the property the cache is keyed on.
    """
    parts = [base, f"TASK: {task}"]
    graded = graded_block(criterion)
    if graded:
        parts.append(graded)
    return "\n\n".join(parts)


def build_node_system(
    run_system: str, skills: Sequence[Skill], task: str = ""
) -> str:
    """The run-stable layer plus the skills retrieved for ONE node.

    Skills sit here, not in the volatile block, because every agent in a
    node's pool queries the library with the same node text and gets the same
    answer -- so the retrieved advice is stable for the whole pool and belongs
    in the shared prefix.

    RETRIEVAL STAYS NODE-SCOPED, and that is the part that matters. Coarsening
    the retrieval KEY to run scope was measured in this repo to make node pass
    rate worse (0.567 against 0.656). This changes only which message carries
    the retrieved text; the query is still `task + node.instruction`, and the
    skills a node is offered are exactly the ones it was offered before.
    """
    block = skills_block(skills, task)
    return f"{run_system}\n\n{block}" if block else run_system


@dataclass(frozen=True, slots=True)
class NodePrefix:
    """The shared head of every prompt on one node, resolved exactly once.

    Two agents on the same node must send byte-identical leading bytes or the
    provider's prefix cache sees two different prompts. Retrieval alone does
    not guarantee that: `SkillLibrary.retrieve` scores by success rate, and
    `record_use` moves success rates DURING a run, so agent 1 and agent 12
    querying the same library with the same text can be offered a different
    ordering. Resolving the prefix once per node and handing the same object
    to the batch call and to every worker is what makes the property hold.
    """

    system: str
    skills: tuple[Skill, ...] = ()


def system_for(
    context: WorkerContext, task: str, skills: Sequence[Skill]
) -> str:
    """The system message a worker on this node sends.

    ONE implementation, called both when a run freezes a node's prefix and
    when a worker builds its own. Two would be two chances for the bytes to
    diverge, and the whole mechanism is a byte comparison.
    """
    if prefix_order() == LEGACY:
        # Legacy sends everything in one user message, so the system message
        # is the bare base prompt exactly as it was before this change.
        return context.system
    run_system = context.run_system or build_run_system(
        base=context.system, task=task, criterion=context.criterion
    )
    return build_node_system(run_system, skills, task)


def build_node_prefix(
    context: WorkerContext, task: str, node: PlanNode
) -> NodePrefix:
    """Retrieve this node's skills and freeze the prefix they belong to."""
    skills = tuple(
        context.skills.retrieve(f"{task} {node.instruction}")
        if context.skills
        else ()
    )
    return NodePrefix(system=system_for(context, task, skills), skills=skills)


@dataclass
class WorkerContext:
    """Everything a worker needs, assembled once per run.

    Passed rather than looked up so a worker holds no global state and can be
    constructed in a test without a provider, a ledger, or a library.
    """

    provider: Any                        # anything with .complete(LLMRequest)
    criterion: Criterion
    economy: Economy
    account: CostAccount | None = None
    redteam: RedTeam | None = None
    skills: SkillLibrary | None = None
    sandbox: Any = None
    run_id: str = ""
    # The worker system prompt. An INPUT rather than a constant because the
    # supervisor rewrites it between runs: a prompt that cannot be replaced
    # cannot be improved, and a supervisor whose patches never reach a worker
    # is a report generator.
    system: str = WORKER_SYSTEM
    # The run-stable prefix layer (`build_run_system`): base prompt + TASK +
    # the frozen criterion, computed once by the run and shared by every agent
    # in it. Left empty by a caller that builds a context by hand -- a worker
    # then derives it from `system`, `criterion` and the task, which yields
    # the same bytes because `build_run_system` is pure.
    run_system: str = ""
    max_tokens: int = 512
    temperature: float = 0.7
    # Repair rounds inside a single node before giving up on this agent's
    # attempt. Bounded because an unbounded repair loop is a livelock that
    # spends the whole run's quota on one stubborn node.
    max_repairs: int = 2


@dataclass
class WorkerResult:
    agent_id: str
    node: str
    candidate: Candidate
    passed: bool
    attempts: int = 1
    credits_spent: float = 0.0
    contained: bool = False
    failures: tuple[str, ...] = ()
    skill_used: str = ""
    # Progress at the moment this result was produced. Handed to a replacement
    # agent when the original is killed, so completed steps are not redone.
    checkpoint: Checkpoint | None = None
    thoughts: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "node": self.node,
            "passed": self.passed,
            "attempts": self.attempts,
            "credits_spent": round(self.credits_spent, 1),
            "contained": self.contained,
            "failures": list(self.failures),
            "skill_used": self.skill_used,
            "output_preview": self.candidate.output[:280],
            "artifacts": self.candidate.artifacts,
        }

    # -- durable form -------------------------------------------------------
    #
    # Deliberately separate from `to_dict`. That one is the DISPLAY form and
    # truncates the output to a 280-character preview, which is right for a
    # report and wrong for a store: a resumed run rebuilt from previews would
    # report a different `integrity_hash` than the same run uninterrupted,
    # since the hash reads the full output. The chaos gate compares those
    # hashes, so a lossy round-trip would turn every resume into a false
    # integrity failure -- or, worse, hide a real one behind a hash that no
    # longer describes the same text.

    def to_state(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "node": self.node,
            "passed": self.passed,
            "attempts": self.attempts,
            "credits_spent": self.credits_spent,
            "contained": self.contained,
            "failures": list(self.failures),
            "skill_used": self.skill_used,
            "candidate": {
                "output": self.candidate.output,
                "artifacts": self.candidate.artifacts,
                "exit_code": self.candidate.exit_code,
                "stdout": self.candidate.stdout,
                "stderr": self.candidate.stderr,
                "source": self.candidate.source,
            },
            "checkpoint": self.checkpoint.to_dict() if self.checkpoint else None,
            "thoughts": self.thoughts,
        }

    @staticmethod
    def from_state(data: dict[str, Any]) -> WorkerResult:
        raw = data.get("candidate") or {}
        return WorkerResult(
            agent_id=str(data.get("agent_id", "")),
            node=str(data.get("node", "")),
            candidate=Candidate(
                output=str(raw.get("output", "")),
                artifacts=dict(raw.get("artifacts") or {}),
                exit_code=raw.get("exit_code"),
                stdout=str(raw.get("stdout", "")),
                stderr=str(raw.get("stderr", "")),
                source=str(raw.get("source", "")),
            ),
            passed=bool(data.get("passed", False)),
            attempts=int(data.get("attempts", 1)),
            credits_spent=float(data.get("credits_spent", 0.0)),
            contained=bool(data.get("contained", False)),
            failures=tuple(data.get("failures") or ()),
            skill_used=str(data.get("skill_used", "")),
            checkpoint=(
                Checkpoint.from_dict(data["checkpoint"])
                if data.get("checkpoint")
                else None
            ),
            thoughts=list(data.get("thoughts") or []),
        )


class GenericWorker:
    """One worker. Identical to every other worker in the population."""

    def __init__(self, agent_id: str, context: WorkerContext) -> None:
        self.agent_id = agent_id
        self.context = context
        self.thoughts: list[dict[str, Any]] = []

    # -- reasoning trace ----------------------------------------------------

    def _think(self, decision: str, reasoning: str = "") -> None:
        """Capture a chain-of-thought step for the dashboard and the trace.

        Recorded at the moment it happens with a monotonic tick, because a
        parent's post-child thought otherwise sorts before the child's -- spans
        close after their children, so span order scrambles chronology.
        """
        self.thoughts.append(
            {
                "agent_id": self.agent_id,
                "decision": decision,
                "reasoning": reasoning,
                "tick": time.monotonic_ns(),
            }
        )

    # -- prompt assembly ----------------------------------------------------

    def build_prompt(
        self, task: str, node: PlanNode, skills: list[Skill], failures: tuple[str, ...]
    ) -> str:
        """The VOLATILE half of the prompt: what changes from call to call.

        Under the default "hoisted" layout that is STEP, REQUIRED and the
        failures of the previous attempt -- and nothing else. The task, the
        criterion and this node's skills are stable for the whole run or the
        whole node, so they ride in the system message (`build_system`) where
        they form a prefix the provider can cache. Repeating them here would
        send the same bytes twice under two roles and put the divergence point
        back at the second line, which is the defect this layout removes.

        Failures are included verbatim. A repair round that does not say what
        failed is just a re-roll, and re-rolling is how a bounded repair
        budget gets spent without converging.

        `skills` is still a parameter, and the signature is deliberately
        unchanged, because the legacy layout still renders them here.
        """
        if prefix_order() == LEGACY:
            return self._legacy_prompt(task, node, skills, failures)
        parts = [f"STEP: {node.name}", f"REQUIRED: {node.instruction}"]
        tail = failures_block(failures)
        if tail:
            parts.append(tail)
        return "\n\n".join(parts)

    def _legacy_prompt(
        self, task: str, node: PlanNode, skills: Sequence[Skill],
        failures: tuple[str, ...],
    ) -> str:
        """The pre-hoist single user message, reproduced byte for byte.

        The rollback path. Kept as its own method rather than as branches
        inside `build_prompt` so that "legacy is unchanged" is something a
        test can assert against one function instead of a flag's worth of
        conditionals.
        """
        parts = [f"TASK: {task}", f"STEP: {node.name}", f"REQUIRED: {node.instruction}"]
        for block in (
            graded_block(getattr(self.context, "criterion", None)),
            skills_block(skills, task),
            failures_block(failures),
        ):
            if block:
                parts.append(block)
        return "\n\n".join(parts)

    def build_system(self, task: str, skills: Sequence[Skill]) -> str:
        """The STABLE half of this worker's prompt, paired with `build_prompt`.

        Derived from the run's precomputed `context.run_system` when there is
        one. The fallback recomputes it, which is safe only because
        `build_run_system` is pure -- a fallback that folded in anything
        per-call would silently halve the cache hit rate and nothing would
        report it.
        """
        return system_for(self.context, task, skills)

    # -- execution ----------------------------------------------------------

    async def execute(
        self,
        task: str,
        node: PlanNode,
        checkpoint: Checkpoint | None = None,
        prefix: NodePrefix | None = None,
    ) -> WorkerResult:
        """Run one plan node, checkpointing at every step boundary.

        THE GUARANTEE, and the reason this is not a plain loop: a killed
        agent's completed work is not redone. The replacement receives the dead
        agent's checkpoint and SKIPS every step already in it, so an expensive
        model call or sandbox execution that finished before the kill is reused
        rather than repeated.

        Steps are attempt-scoped (generate:1, materialise:1, generate:2)
        because a repair round is genuinely new work: a kill during attempt two
        must resume at attempt two with attempt one's result intact, not
        restart the whole node.

        Uses the kernel's Checkpoint contract -- same type, same
        skip-completed-steps semantics, same schema versioning -- so the
        recovery proven at kill-rate 0.9 by the kernel demo is the same
        mechanism operating here rather than a second implementation of it.
        """
        ctx = self.context
        cp = checkpoint or Checkpoint(task_id=node.name, agent_id=self.agent_id)
        if cp.completed_steps:
            self._think(
                "resumed",
                f"resuming from a checkpoint with {len(cp.completed_steps)} "
                f"completed step(s): {', '.join(cp.completed_steps)}",
            )

        # The node's prefix is normally resolved ONCE by the run and handed to
        # every agent in the pool -- see `NodePrefix`. A worker constructed
        # directly (a test, a seeded rogue) resolves its own, which retrieves
        # the same skills the run would have.
        node_prefix = prefix if prefix is not None else build_node_prefix(
            ctx, task, node
        )
        skills = list(node_prefix.skills)
        system = node_prefix.system
        skill_used = skills[0].skill_id if skills else ""
        if skills:
            self._think(
                "retrieved_skill",
                f"library offered {len(skills)} approach(es); starting from "
                f"{skills[0].name!r}",
            )

        failures: tuple[str, ...] = ()
        spent = 0.0
        candidate = Candidate()

        for attempt in range(1, ctx.max_repairs + 2):
            generate_step = f"generate:{attempt}"
            materialise_step = f"materialise:{attempt}"
            grade_step = f"grade:{attempt}"

            # An attempt already graded before the kill is skipped whole.
            if grade_step in cp.completed_steps:
                graded = cp.data[grade_step]
                if graded.get("passed"):
                    candidate = _candidate_from(cp.data.get(materialise_step, {}))
                    return self._result(
                        node, candidate, True, attempt, spent, (), skill_used,
                        checkpoint=cp,
                    )
                failures = tuple(graded.get("failures", ()))
                continue

            prompt = self.build_prompt(task, node, skills, failures)

            # -- step: generate --------------------------------------------
            if generate_step in cp.completed_steps:
                text = str(cp.data[generate_step])
                self._think(
                    "skipped_generate",
                    f"attempt {attempt} was generated before the kill; reusing "
                    f"it rather than paying for it twice",
                )
            else:
                cost = estimate_cost(prompt, ctx.max_tokens)
                if not ctx.economy.can_afford(self.agent_id, cost):
                    self._think(
                        "out_of_budget",
                        f"needed {cost:.0f} credits, cannot afford",
                    )
                    return self._result(
                        node, candidate, False, attempt, spent, failures,
                        skill_used, checkpoint=cp,
                    )
                try:
                    ctx.economy.spend(self.agent_id, cost, stage=node.name)
                except Bankrupt:
                    self._think("bankrupt", "agent exhausted its allowance")
                    return self._result(
                        node, candidate, False, attempt, spent, failures,
                        skill_used, checkpoint=cp,
                    )
                spent += cost

                self._think(
                    "calling_model",
                    f"attempt {attempt} of {ctx.max_repairs + 1} for step "
                    f"{node.name!r}",
                )
                text = await self._call(prompt, system)
                cp = cp.with_step(generate_step, text)

                if self._observe(
                    Action(
                        agent_id=self.agent_id, kind="llm_call", stage=node.name,
                        credits=cost, payload=prompt,
                    )
                ):
                    return self._result(
                        node, candidate, False, attempt, spent, failures,
                        skill_used, contained=True, checkpoint=cp,
                    )

            # -- step: materialise -----------------------------------------
            if materialise_step in cp.completed_steps:
                candidate = _candidate_from(cp.data[materialise_step])
            else:
                candidate = await self._materialise(text, node)
                cp = cp.with_step(materialise_step, _candidate_to(candidate))

                if self._observe(
                    Action(
                        agent_id=self.agent_id, kind="sandbox_exec",
                        stage=node.name, payload=text,
                        detail={
                            "sandbox_violation": candidate.artifacts.pop(
                                "_violation", ""
                            )
                        },
                    )
                ):
                    return self._result(
                        node, candidate, False, attempt, spent, failures,
                        skill_used, contained=True, checkpoint=cp,
                    )

            # -- step: grade -----------------------------------------------
            # The frozen criterion decides. Not the worker. Free to evaluate,
            # so it is checkpointed for resume ordering rather than for cost.
            verdict = ctx.criterion.evaluate(candidate)
            failure_list = [f"{o.kind}: {o.detail}" for o in verdict.failures]
            cp = cp.with_step(
                grade_step,
                {"passed": verdict.passed, "failures": failure_list},
            )

            if verdict.passed:
                self._think("criterion_passed", verdict.summary())
                contained = self._observe(
                    Action(
                        agent_id=self.agent_id, kind="submit", stage=node.name,
                        verified_success=True, payload=candidate.output,
                    )
                )
                ctx.economy.settle(
                    self.agent_id, verified_success=not contained, stage=node.name
                )
                if ctx.skills and skill_used:
                    ctx.skills.record_use(skill_used, success=not contained)
                return self._result(
                    node, candidate, not contained, attempt, spent, (),
                    skill_used, contained=contained, checkpoint=cp,
                )

            failures = tuple(failure_list)
            self._think("criterion_failed", verdict.summary())

        ctx.economy.settle(self.agent_id, verified_success=False, stage=node.name)
        if ctx.skills and skill_used:
            ctx.skills.record_use(skill_used, success=False)
        return self._result(
            node, candidate, False, ctx.max_repairs + 1, spent, failures,
            skill_used, checkpoint=cp,
        )

    # -- helpers ------------------------------------------------------------

    async def _call(self, prompt: str, system: str | None = None) -> str:
        from swarmd.router.providers import LLMRequest

        # `system` is the resolved node prefix, not `context.system`: the
        # difference between the two is the entire caching change. Defaulting
        # to the bare base prompt keeps a hand-rolled caller (rogues, tests)
        # working.
        request = LLMRequest(
            prompt=prompt,
            system=self.context.system if system is None else system,
            temperature=self.context.temperature,
            max_tokens=self.context.max_tokens,
            metadata={"agent_id": self.agent_id, "stage": "worker"},
        )
        try:
            response = await self.context.provider.complete(request)
            return str(response.text)
        except Exception as exc:  # noqa: BLE001 - a provider failure is data
            # A dead provider must not take the worker down: the run has other
            # agents, and this one's failure is recorded rather than raised.
            self._think("provider_error", str(exc)[:200])
            return ""

    async def _materialise(self, text: str, node: PlanNode) -> Candidate:
        """Turn a model reply into a gradeable Candidate.

        THE OUTPUT IS THE ANSWER, NOT THE REPLY. This distinction is the whole
        function, and getting it wrong made every real-provider run score 0/N.

        The worker prompt tells agents to write results to artifacts.json, and
        real models comply: they reply with a fenced ```python block. The old
        version ran that code correctly and then set `output` to the fenced
        SOURCE, so a criterion checking `json_parses` read Python and said "not
        JSON" -- for a step that had in fact succeeded.

        So, in order of what the step actually produced:

          artifacts   the contract the prompt states. Serialised as the answer.
          stdout      a program that printed its result instead of writing it.
          the reply   no code ran; the reply IS the answer, un-fenced.

        The simulated provider never surfaced this because it replied with bare
        JSON, which is the one shape where source and answer coincide.
        """
        code = _extract_code(text)
        if code and self.context.sandbox is not None:
            self._think("executing_code", f"{len(code)} chars in the sandbox")
            result = await self.context.sandbox.run_python(code)
            artifacts = dict(result.artifacts)
            if result.violation:
                artifacts["_violation"] = result.violation

            if artifacts:
                answer = json.dumps(artifacts, sort_keys=True, default=str)
            elif result.stdout.strip():
                answer = result.stdout.strip()
            else:
                # Nothing produced. Keeping the source here is deliberate: the
                # criterion should fail this, and a repair round is more useful
                # when the agent can see what it submitted.
                answer = text
            return Candidate(
                output=answer,
                source=text,
                artifacts=artifacts,
                exit_code=result.exit_code,
                stdout=result.stdout,
                stderr=result.stderr,
            )
        # No code ran. If the reply IS a JSON object, it is the artifact set:
        # `{"accuracy": 94.3}` answered directly and `artifacts.json` written
        # by a script are the same claim, and a criterion asking for the key
        # `accuracy` means the same thing in both cases.
        #
        # Without this, agents that answered correctly and directly failed
        # every `artifact_exists` check -- observed live, producing exactly
        # `{"accuracy": 94.3, "baseline": 82.1}` from the source text and being
        # graded as though they had produced nothing.
        answer = _unfence(text)
        direct: dict[str, Any] = {}
        try:
            parsed = json.loads(answer)
            if isinstance(parsed, dict):
                direct = parsed
        except (json.JSONDecodeError, TypeError):
            pass
        return Candidate(output=answer, artifacts=direct)

    def _observe(self, action: Action) -> bool:
        """Report an action to the red-team. Returns True if contained."""
        if self.context.redteam is None:
            return False
        containment = self.context.redteam.observe(action)
        if containment is not None and containment.contained:
            self._think("contained", containment.detection.detail)
            self.context.economy.kill(
                self.agent_id, reason=f"contained: {containment.detection.pattern}"
            )
            return True
        return False

    def _result(
        self,
        node: PlanNode,
        candidate: Candidate,
        passed: bool,
        attempts: int,
        spent: float,
        failures: tuple[str, ...],
        skill_used: str,
        *,
        contained: bool = False,
        checkpoint: Checkpoint | None = None,
    ) -> WorkerResult:
        return WorkerResult(
            checkpoint=checkpoint,
            agent_id=self.agent_id,
            node=node.name,
            candidate=candidate,
            passed=passed,
            attempts=attempts,
            credits_spent=spent,
            contained=contained,
            failures=failures,
            skill_used=skill_used,
            thoughts=list(self.thoughts),
        )


def _candidate_to(candidate: Candidate) -> dict[str, Any]:
    """Checkpoint payloads must be JSON-serialisable.

    A checkpoint that cannot round-trip through a durable store is one that
    only works in memory, which is the single case where it is not needed.
    """
    return {
        "output": candidate.output,
        "source": candidate.source,
        "artifacts": candidate.artifacts,
        "exit_code": candidate.exit_code,
        "stdout": candidate.stdout,
        "stderr": candidate.stderr,
    }


def _candidate_from(data: dict[str, Any]) -> Candidate:
    return Candidate(
        output=str(data.get("output", "")),
        source=str(data.get("source", "")),
        artifacts=dict(data.get("artifacts") or {}),
        exit_code=data.get("exit_code"),
        stdout=str(data.get("stdout", "")),
        stderr=str(data.get("stderr", "")),
    )


def _unfence(text: str) -> str:
    """Strip a markdown fence around a non-code answer.

    A model asked for JSON commonly returns it inside ```json fences. That is a
    presentation wrapper, not part of the answer, and leaving it on makes every
    `json_parses` check fail on output that is otherwise exactly right.

    Only strips when the whole reply is one fenced block: a fence in the middle
    of prose is content, and cutting it out would change the answer.
    """
    stripped = text.strip()
    fence = "```"
    if not stripped.startswith(fence) or stripped.count(fence) < 2:
        return text
    body = stripped[len(fence) :]
    first_newline = body.find("\n")
    if first_newline == -1:
        return text
    language = body[:first_newline].strip().lower()
    if language not in {"", "json", "text"}:
        return text
    inner = body[first_newline + 1 :]
    closing = inner.rfind(fence)
    return inner[:closing].strip() if closing != -1 else text


def _extract_code(text: str) -> str:
    """Pull a Python block out of a response.

    Fenced blocks only. Treating any response as code would execute prose, and
    a model explaining what it *would* do is not a request to do it.
    """
    fence = "```"
    if fence not in text:
        return ""
    parts = text.split(fence)
    for i in range(1, len(parts), 2):
        block = parts[i]
        first_newline = block.find("\n")
        if first_newline == -1:
            continue
        language = block[:first_newline].strip().lower()
        body = block[first_newline + 1 :]
        if language in {"", "python", "py"} and body.strip():
            return body
    return ""
