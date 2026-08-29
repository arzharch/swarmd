"""Durable run state, so a paused run survives the process that started it.

WHY THIS EXISTS. The pacer will park a run for hours when a session's ration is
spent -- that is the point of it. A pause measured in hours crosses laptop
sleeps, deploys, terminal closures and Ctrl-C. A run that cannot survive those
has not been paused, it has been abandoned, and every provider request it
already paid for is wasted.

The kernel has checkpointed since Phase 1 and the swarm since the recovery
work; both keep an agent's progress across a KILL inside one process. Neither
keeps a RUN across process exit, because the criterion, the plan and the batch
drafts lived only in `SwarmRun`'s attributes. Those are the expensive things:
the criterion cost N proposer calls, the plan cost N more, and each node's
batch cost one call that produced K variants. Losing them to a restart means
buying them again.

WHAT IS PERSISTED, and the test for inclusion is "would resuming without it
cost a provider call or change the result":

  criterion     N proposer calls, and it is content-addressed -- resuming under
                a DIFFERENT criterion would grade the second half of a run
                against a target the first half never saw.
  plan          N proposer calls, and node names the checkpoints key on.
  drafts        one batched call per node, K variants.
  checkpoints   per agent, the existing Checkpoint contract.
  economy       balances decide who may still spend; resuming with fresh
                allowances would hand a bankrupt population a second budget.
  contained     the red-team's kill set. Forgetting it un-contains a rogue.
  results       finished nodes, so they are not re-run.

WHAT IS NOT PERSISTED. The ration and the usage journal, deliberately: those
live in `.swarmd/usage.jsonl` and are shared by every run on the machine. A run
that carried its own copy could resume into a session whose budget another run
had already spent.

FORMAT: one JSON document per run, replaced atomically. Not append-only like
the ledger, because this is a snapshot of current state rather than a history
of what happened -- and because the reader is a resume path that wants the
latest state, not a reconstruction. The ledger remains the record of events;
this is the working set.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import tempfile
from dataclasses import dataclass, field
from typing import Any

from swarmd.task import Checkpoint

logger = logging.getLogger(__name__)

DEFAULT_ROOT = ".swarmd/runs"

# Bumped when the stored shape changes incompatibly. A resume that silently
# misreads an old document would produce a run that looks fine and is not.
STORE_SCHEMA_VERSION = 1


@dataclass
class RunState:
    """Everything a resumed run needs that it cannot cheaply recompute."""

    run_id: str
    task: str
    profile: str
    agents: int = 0
    status: str = "running"
    # Content hash of the frozen criterion, kept alongside the criterion itself
    # so a resume can refuse a document whose criterion does not match what it
    # was graded against.
    criterion_hash: str = ""
    criterion: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    # node -> the batch variants generated for it. The single most expensive
    # thing to lose: one provider call each, and they seed every agent's first
    # attempt through the checkpoint skip path.
    drafts: dict[str, list[str]] = field(default_factory=dict)
    # agent_id -> Checkpoint.to_dict()
    checkpoints: dict[str, dict[str, Any]] = field(default_factory=dict)
    # agent_id -> balance. Resuming with fresh allowances would give a
    # population that spent its budget a second one.
    balances: dict[str, float] = field(default_factory=dict)
    contained: list[str] = field(default_factory=list)
    # Finished node results, so a resume does not re-run what already passed.
    results: list[dict[str, Any]] = field(default_factory=list)
    # Set while parked, so an operator inspecting the file mid-pause sees why.
    paused_reason: str = ""
    resumes_at: float = 0.0
    schema_version: int = STORE_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "task": self.task,
            "profile": self.profile,
            "agents": self.agents,
            "status": self.status,
            "criterion_hash": self.criterion_hash,
            "criterion": self.criterion,
            "plan": self.plan,
            "drafts": self.drafts,
            "checkpoints": self.checkpoints,
            "balances": self.balances,
            "contained": self.contained,
            "results": self.results,
            "paused_reason": self.paused_reason,
            "resumes_at": self.resumes_at,
            "schema_version": self.schema_version,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> RunState:
        version = int(data.get("schema_version", 0))
        if version != STORE_SCHEMA_VERSION:
            # Refused rather than best-effort parsed. A resume that half-reads
            # an old document produces a run that looks correct and is not,
            # which is worse than starting over knowingly.
            raise IncompatibleRunState(
                f"run state schema {version}, this build writes "
                f"{STORE_SCHEMA_VERSION}; start a fresh run rather than "
                f"resuming into a shape this code does not understand"
            )
        return RunState(
            run_id=str(data["run_id"]),
            task=str(data.get("task", "")),
            profile=str(data.get("profile", "smoke")),
            agents=int(data.get("agents", 0)),
            status=str(data.get("status", "running")),
            criterion_hash=str(data.get("criterion_hash", "")),
            criterion=data.get("criterion"),
            plan=data.get("plan"),
            drafts={k: list(v) for k, v in (data.get("drafts") or {}).items()},
            checkpoints=dict(data.get("checkpoints") or {}),
            balances={k: float(v) for k, v in (data.get("balances") or {}).items()},
            contained=list(data.get("contained") or []),
            results=list(data.get("results") or []),
            paused_reason=str(data.get("paused_reason", "")),
            resumes_at=float(data.get("resumes_at", 0.0)),
        )

    # -- checkpoints ----------------------------------------------------

    def checkpoint_for(self, agent_id: str) -> Checkpoint | None:
        raw = self.checkpoints.get(agent_id)
        return Checkpoint.from_dict(raw) if raw else None

    def remember(self, checkpoint: Checkpoint | None) -> None:
        if checkpoint is not None:
            self.checkpoints[checkpoint.agent_id] = checkpoint.to_dict()

    @property
    def finished_nodes(self) -> set[str]:
        """Nodes whose results are already recorded, so a resume skips them."""
        return {str(r.get("node", "")) for r in self.results if r.get("node")}


class IncompatibleRunState(RuntimeError):
    """The stored document was written by a different build."""


class RunStore:
    """Reads and writes run documents. One file per run.

    Writes are atomic: a temp file in the same directory, then a replace. A
    torn write here is worse than in the ledger -- the ledger loses one row of
    history, this loses the whole working set -- and `os.replace` is atomic on
    both POSIX and Windows.
    """

    def __init__(self, root: str | pathlib.Path | None = None) -> None:
        self.root = pathlib.Path(
            root or os.environ.get("SWARMD_RUN_STORE", DEFAULT_ROOT)
        )

    def path_for(self, run_id: str) -> pathlib.Path:
        # Run ids are generated (`run-<hex>`), but a resumed id arrives from an
        # operator's command line, so it is not trusted to be a bare name.
        safe = "".join(c for c in run_id if c.isalnum() or c in "-_")
        return self.root / f"{safe}.json"

    def save(self, state: RunState) -> None:
        path = self.path_for(state.run_id)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            # delete=False because the file outlives the context manager:
            # it is renamed into place, not consumed. Same directory as the
            # target so the replace is a rename rather than a cross-device
            # copy, which would not be atomic.
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(state.to_dict(), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            os.replace(temp_name, path)
        except OSError as exc:
            # A run that cannot persist is less recoverable, not broken. Failing
            # the run here would trade a degraded resume for no result at all.
            logger.warning("run state not saved (%s): %s", path, exc)

    def load(self, run_id: str) -> RunState | None:
        path = self.path_for(run_id)
        if not path.exists():
            return None
        try:
            return RunState.from_dict(json.loads(path.read_text(encoding="utf-8")))
        except IncompatibleRunState:
            raise
        except (OSError, json.JSONDecodeError, KeyError) as exc:
            logger.warning("run state unreadable (%s): %s", path, exc)
            return None

    def list_runs(self) -> list[RunState]:
        """Every resumable run, newest first. Feeds `swarmd runs list`."""
        if not self.root.exists():
            return []
        states: list[tuple[float, RunState]] = []
        for path in self.root.glob("*.json"):
            try:
                state = RunState.from_dict(
                    json.loads(path.read_text(encoding="utf-8"))
                )
            except Exception:  # noqa: BLE001 - one bad file must not hide the rest
                logger.warning("skipping unreadable run document %s", path)
                continue
            states.append((path.stat().st_mtime, state))
        return [state for _, state in sorted(states, key=lambda p: -p[0])]

    # Terminal states. A run in one of these will never be resumed, so its
    # working set is dead weight; a run in any other state might be picked up
    # hours later and must survive regardless of age.
    TERMINAL = frozenset(
        {"completed", "failed_criterion", "aborted", "error", "cancelled"}
    )

    def prune(self, *, older_than_s: float = 14 * 86400.0, now: float | None = None) -> int:
        """Drop finished run documents past their useful life. Returns the count.

        Safe to lose, and this is the reason: the LEDGER is the durable record
        (ADR-007) and this is the working set -- criterion, plan, drafts and
        results kept so a paused run can be resumed. Once a run is finished
        there is nothing left to resume and the file is only taking space, and
        each one carries every batch draft the run generated.

        A run that is paused, running, or in any state this build does not
        recognise is NEVER pruned, however old. Age is not evidence that a
        parked run was abandoned -- a ration pause plus a weekend looks exactly
        like one.
        """
        import time as _time

        now = now if now is not None else _time.time()
        cutoff = now - older_than_s
        dropped = 0
        for state in self.list_runs():
            if state.status not in self.TERMINAL:
                continue
            path = self.path_for(state.run_id)
            try:
                if path.stat().st_mtime >= cutoff:
                    continue
                path.unlink()
                dropped += 1
            except OSError as exc:
                logger.warning("could not prune run state %s: %s", path, exc)
        return dropped

    def delete(self, run_id: str) -> None:
        try:
            self.path_for(run_id).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not delete run state: %s", exc)
