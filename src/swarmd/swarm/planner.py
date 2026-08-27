"""Plan synthesis: the DAG is an agent output, not human code.

Every framework lets you *draw* the graph. Generating it at runtime is what
makes "thrown at an unknown task" mean anything — a hand-drawn pipeline means
the task was one you already scoped.

The pipeline mirrors criterion synthesis deliberately:

  PROPOSE   N agents decompose the task independently.
  VALIDATE  Structurally, before anything runs: acyclic, dependencies
            resolvable, every node reachable, bounded size. An invalid plan is
            REJECTED, never executed hopefully.
  JUDGE     Pick among valid proposals on measurable properties, not on prose.
  HAND OFF  The winner becomes `swarmd.pipeline.Pipeline` stages. There is no
            second execution path: the generated plan runs on exactly the DAG
            executor that Phase 2 tested, which is the whole reason that
            executor was written to take stages as data.

WHY VALIDATION IS STRUCTURAL, not semantic. Nothing here asks whether the plan
is a *good* decomposition — that is what the frozen criterion decides, after
execution. Validation only asks whether it can run at all. Conflating the two
would put a second, weaker judge in front of the one that was adversarially
tested.
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Bounds on a generated plan. A model asked to decompose a task will sometimes
# emit forty stages of one line each; that is not a plan, it is a to-do list,
# and every stage costs scheduling overhead and at least one LLM call.
MAX_NODES = 12
MIN_NODES = 1
MAX_FANIN = 6


class PlanError(ValueError):
    """A proposed plan cannot run. Raised at validation, never at execution."""


@dataclass(frozen=True, slots=True)
class PlanNode:
    """One unit of work in a generated plan."""

    name: str
    instruction: str
    depends_on: tuple[str, ...] = ()
    pool_size: int = 4
    # Which retrieved skill, if any, this node should try first. Filled by the
    # skill library at retrieval time rather than proposed by the planner --
    # the planner does not know what the library holds.
    skill_hint: str = ""

    def canonical(self) -> str:
        return json.dumps(
            {
                "name": self.name,
                "instruction": self.instruction,
                "depends_on": sorted(self.depends_on),
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, slots=True)
class Plan:
    """A validated, executable decomposition."""

    nodes: tuple[PlanNode, ...]
    rationale: str = ""

    def node(self, name: str) -> PlanNode:
        for n in self.nodes:
            if n.name == name:
                return n
        raise KeyError(name)

    @property
    def names(self) -> list[str]:
        return [n.name for n in self.nodes]

    def levels(self) -> list[list[str]]:
        """Dependency levels; nodes within a level may run concurrently.

        Computed here as well as in the executor because the width of the plan
        is a property worth judging BEFORE execution — a plan that is one long
        chain cannot use the worker pool at all.
        """
        remaining = {n.name: set(n.depends_on) for n in self.nodes}
        levels: list[list[str]] = []
        while remaining:
            ready = sorted(name for name, deps in remaining.items() if not deps)
            if not ready:
                raise PlanError(f"cycle among {sorted(remaining)}")
            levels.append(ready)
            for name in ready:
                del remaining[name]
            for deps in remaining.values():
                deps.difference_update(ready)
        return levels

    @property
    def width(self) -> int:
        """Widest level. How much of the pool this plan can actually use."""
        return max((len(level) for level in self.levels()), default=0)

    @property
    def depth(self) -> int:
        return len(self.levels())

    def content_hash(self) -> str:
        import hashlib

        canonical = sorted(n.canonical() for n in self.nodes)
        return hashlib.sha256("|".join(canonical).encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "hash": self.content_hash(),
            "rationale": self.rationale,
            "width": self.width,
            "depth": self.depth,
            "nodes": [
                {
                    "name": n.name,
                    "instruction": n.instruction,
                    "depends_on": list(n.depends_on),
                    "pool_size": n.pool_size,
                    "skill_hint": n.skill_hint,
                }
                for n in self.nodes
            ],
        }


# --- validation ------------------------------------------------------------


def validate(nodes: Sequence[PlanNode]) -> Plan:
    """Structural validation. Every failure names what is wrong and where."""
    if not nodes:
        raise PlanError("plan has no nodes")
    if len(nodes) < MIN_NODES:
        raise PlanError(f"plan has {len(nodes)} nodes, minimum {MIN_NODES}")
    if len(nodes) > MAX_NODES:
        raise PlanError(
            f"plan has {len(nodes)} nodes, maximum {MAX_NODES}. A plan this "
            f"wide is a to-do list, and each node costs scheduling and quota."
        )

    seen: dict[str, PlanNode] = {}
    for node in nodes:
        if not node.name or not node.name.strip():
            raise PlanError("plan node has an empty name")
        if node.name in seen:
            raise PlanError(f"duplicate node name: {node.name!r}")
        if not node.instruction.strip():
            raise PlanError(f"node {node.name!r} has no instruction")
        if len(node.depends_on) > MAX_FANIN:
            raise PlanError(
                f"node {node.name!r} depends on {len(node.depends_on)} nodes, "
                f"maximum {MAX_FANIN}"
            )
        seen[node.name] = node

    for node in nodes:
        for dep in node.depends_on:
            if dep not in seen:
                raise PlanError(f"node {node.name!r} depends on unknown {dep!r}")
            if dep == node.name:
                raise PlanError(f"node {node.name!r} depends on itself")

    plan = Plan(tuple(nodes))
    plan.levels()  # raises PlanError on a cycle

    # Every node must be reachable from a root, i.e. contribute to the result.
    # An orphan subgraph runs, spends quota, and is never consumed.
    reachable: set[str] = set()
    dependents: dict[str, list[str]] = defaultdict(list)
    for node in nodes:
        for dep in node.depends_on:
            dependents[dep].append(node.name)
    roots = [n.name for n in nodes if not n.depends_on]
    if not roots:
        raise PlanError("plan has no root node")
    stack = list(roots)
    while stack:
        current = stack.pop()
        if current in reachable:
            continue
        reachable.add(current)
        stack.extend(dependents.get(current, ()))
    orphans = sorted(set(seen) - reachable)
    if orphans:
        raise PlanError(f"unreachable nodes: {orphans}")

    return plan


# --- proposals -------------------------------------------------------------

PLANNER_SYSTEM = (
    "You decompose an unfamiliar task into a small directed graph of steps. "
    "You do not solve it. Prefer 2-6 steps. Each step must be independently "
    "executable given its declared dependencies, and must state concretely "
    "what it produces."
)

PLAN_SCHEMA_HINT = json.dumps(
    {
        "rationale": "one sentence on why this decomposition",
        "nodes": [
            {
                "name": "short_snake_case_id",
                "instruction": "what this step must produce",
                "depends_on": ["name_of_earlier_node"],
            }
        ],
    },
    separators=(",", ":"),
)


@dataclass
class PlanProposal:
    plan: Plan | None
    raw: str = ""
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.plan is not None


def parse_plan(raw: str) -> PlanProposal:
    """Parse and validate in one step. An invalid plan never becomes a Plan."""
    try:
        start, end = raw.find("{"), raw.rfind("}")
        if start == -1 or end <= start:
            return PlanProposal(None, raw, "no JSON object in response")
        payload = json.loads(raw[start : end + 1])
        entries = payload.get("nodes")
        if not isinstance(entries, list):
            return PlanProposal(None, raw, "payload has no 'nodes' list")
        nodes = []
        for entry in entries:
            if not isinstance(entry, dict):
                return PlanProposal(None, raw, f"malformed node: {entry!r}")
            deps = entry.get("depends_on") or []
            if not isinstance(deps, list):
                return PlanProposal(None, raw, "depends_on must be a list")
            nodes.append(
                PlanNode(
                    name=str(entry.get("name", "")).strip(),
                    instruction=str(entry.get("instruction", "")).strip(),
                    depends_on=tuple(str(d) for d in deps),
                    pool_size=int(entry.get("pool_size", 4) or 4),
                )
            )
        plan = validate(nodes)
        return PlanProposal(
            Plan(plan.nodes, str(payload.get("rationale", ""))), raw
        )
    except (json.JSONDecodeError, PlanError, TypeError, ValueError) as exc:
        return PlanProposal(None, raw, str(exc))


# --- judging ---------------------------------------------------------------


def score_plan(plan: Plan) -> float:
    """Rank valid plans on measurable properties, not on their prose.

    Three terms, in order of weight:

      width   -- parallelism the pool can actually use. A plan that is one long
                 chain wastes every worker but one, and with a ~45 req/min
                 ceiling (docs/CAPACITY.md) wall-clock is the scarce thing.
      brevity -- fewer nodes for the same width. Each node costs at least one
                 LLM call, and quota is the binding constraint.
      concrete-- instructions that name an artifact rather than describing an
                 activity. "produce metrics.json" is checkable; "analyse the
                 data" is not, and a node whose output cannot be recognised
                 cannot be verified.

    Deliberately NOT judged by a model. A model judging plan quality is a second
    opinion with no ground truth, and it would cost a call per proposal. These
    three are computable, explainable, and free.
    """
    width_term = plan.width * 2.0
    brevity_term = -0.4 * len(plan.nodes)
    concrete_markers = (".json", ".csv", ".txt", "file", "artifact", "report",
                        "produce", "write", "output", "return")
    concrete_term = sum(
        1.0
        for node in plan.nodes
        if any(m in node.instruction.lower() for m in concrete_markers)
    )
    return width_term + brevity_term + concrete_term


@dataclass(frozen=True, slots=True)
class PlanSelection:
    plan: Plan
    score: float
    considered: int
    rejected: tuple[str, ...] = ()


class PlanSynthesisFailed(RuntimeError):
    def __init__(self, message: str, rejected: Sequence[str]) -> None:
        super().__init__(message)
        self.rejected = list(rejected)


@dataclass
class PlanSynthesizer:
    """Propose N plans, discard the invalid, pick the best of the rest.

    ANATOMY: proposers
      Why 3: the same reasoning as criterion synthesis. Two gives a tie with no
      resolution; more than five mostly restates and spends scarce quota.

    ANATOMY: max_attempts
      Why 2: a plan failing validation twice is a systematic problem (the task
      is genuinely undecomposable, or the prompt is wrong), not bad luck.
      Re-rolling a systematic problem burns quota for nothing.
    """

    proposers: int = 3
    max_attempts: int = 2
    history: list[str] = field(default_factory=list)

    async def synthesize(self, task: str, propose: Any) -> PlanSelection:
        rejected: list[str] = []
        for attempt in range(1, self.max_attempts + 1):
            raw = [await propose(task, attempt, i) for i in range(self.proposers)]
            proposals = [parse_plan(r) for r in raw]

            for proposal in proposals:
                if not proposal.ok:
                    rejected.append(proposal.error)
                    self.history.append(f"attempt {attempt}: rejected: {proposal.error}")

            valid = [p.plan for p in proposals if p.plan is not None]
            if not valid:
                continue

            best = max(valid, key=score_plan)
            self.history.append(
                f"attempt {attempt}: selected plan {best.content_hash()} "
                f"({len(best.nodes)} nodes, width {best.width}, depth {best.depth})"
            )
            return PlanSelection(best, score_plan(best), len(valid), tuple(rejected))

        raise PlanSynthesisFailed(
            f"no valid plan after {self.max_attempts} attempts for task: "
            f"{task[:120]!r}",
            rejected,
        )


def to_stages(plan: Plan, make_fn: Any) -> list[Any]:
    """Convert a generated plan into executor stages.

    `make_fn(node) -> async fn` builds the callable for one node. Kept as a
    parameter so this module never imports a worker or a provider, which is
    what lets plan synthesis be tested without either.
    """
    from swarmd.pipeline.dag import Stage

    return [
        Stage(
            name=node.name,
            fn=make_fn(node),
            pool_size=node.pool_size,
            depends_on=list(node.depends_on),
        )
        for node in plan.nodes
    ]
