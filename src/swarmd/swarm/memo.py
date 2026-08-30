"""The run memo: what a task's criterion and plan were, the last time it worked.

WHY THIS EXISTS. Every run opens with the same serial, always-paid head: three
proposer calls to author a criterion, three more to author a plan (one
`profile.proposers` each, see `PROFILES`). Nothing else may start until both
have frozen, so on `smoke` that is 6 of ~30 calls and 100% of the run's
time-to-first-worker. Ask the same question twice and the system pays it twice,
which is the specific thing the owner noticed: "if it learns a task, the next
time a similar one comes in it should fire instantaneously."

WHAT IS REUSED, AND WHAT IS NOT. A memo carries the CRITERION and the PLAN --
the definition of success and the decomposition. It does NOT carry candidates,
outputs, artifacts, or a status. A memo hit still runs every worker against the
real, current task and still grades the result with the frozen criterion; what
it skips is asking three models what "done" means for a question that has
already been answered, attacked and completed once. There is no stored answer
here to serve, by construction, so the worst case of a bad memo is a run graded
against a criterion it would have written itself.

WHY THE KEY IS EXACT. `normalise` strips, collapses internal whitespace and
casefolds. Nothing fuzzier -- and specifically no similarity, no embedding, no
token overlap. `router/cache.py` documents what happened the last time this
system matched machine-assembled prompts by cosine: three genuinely different
plan nodes measured 0.97 similar, above the 0.95 threshold, so one node's
answer was served to another and the run reported a high hit rate while being
wrong. Templated text is dominated by shared boilerplate, so similarity there
rises with template length rather than with sameness. An exact key on the
normalised task cannot make that mistake.

WHY A MEMO IS ONLY REUSABLE ONCE ITS RUN COMPLETED. The entry is written when
the criterion freezes, but it stays unusable until the originating run reaches
`completed` -- i.e. until that criterion actually graded real work and the run
passed its own gate. A criterion frozen by a run that then failed, aborted or
was interrupted is exactly the criterion not to inherit: it is unproven at
best, and at worst it is the reason the run failed.

THE NEAR-MATCH TIER. The exact key above only fires on a repeat of the same
question, which leaves "a SIMILAR task fires instantly" unmet -- pencils at a
different price still pay for six proposer calls. `task_fingerprint` is a
second, deliberately coarser key: `generalise.abstract_fingerprint`, the same
SHAPE-not-subject fingerprint that lets a skill transfer from pens to pencils
(see `generalise.py`'s own module docstring). `by_fingerprint` is the lookup
over it. This is NOT the similarity `router/cache.py` warns against -- it is
not a threshold on a continuous score, it is an equality test on a
deterministic, discrete shape -- and nothing here serves an answer on a
fingerprint match alone: a near hit still has to have its literals rebound
onto the NEW task and its criterion re-attacked against the NEW task text
before `swarm/run.py` may trust either. See `_criterion_from_near_memo` there
for the half of the invariant this module cannot enforce by itself.

FORMAT: one JSON document per task key, replaced atomically, under the RunStore
root. Same write discipline as `RunStore.save` (temp file in the same
directory, fsync, `os.replace`) and the same filename sanitiser. Content-hash
verified on load; a mismatch is QUARANTINED into `memos/quarantine/`, never
silently deleted, because a document that does not hash to its own contents is
evidence rather than garbage -- the same discipline `SkillLibrary._load` uses.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import pathlib
import tempfile
import time
from dataclasses import dataclass
from typing import Any

from swarmd.swarm.generalise import abstract_fingerprint
from swarmd.swarm.runstore import RunStore

logger = logging.getLogger(__name__)

# Bumped when the stored shape changes incompatibly. A memo read under the
# wrong shape would hand a run a criterion it cannot verify, which is worse
# than paying for a new one.
#
# 2: added `task_fingerprint`, the near-match tier's secondary key. A memo
# written under version 1 has no fingerprint to index by, and reading it as
# though it did would either mismatch every lookup or, worse, match one by
# coincidence (an unset field reads as "" on both sides). Bumping means those
# entries age out and are re-earned rather than silently misindexed.
MEMO_SCHEMA_VERSION = 2

# Directory under the RunStore root. Beside the run documents deliberately: a
# memo is derived from a run, ages with the same working set, and an operator
# clearing `.swarmd/runs` should clear both rather than leave one pointing at
# the other's ghosts.
DEFAULT_DIRNAME = "memos"

# ANATOMY: MEMO_MAX_AGE_S (30 days)
#   How long a frozen criterion is allowed to speak for a task. Not a
#   correctness bound -- the criterion is re-attacked against the new task text
#   on every hit, so an old memo is not a wrong one. It bounds STALENESS of
#   judgement: check kinds get added, the proposer prompt changes, and a
#   month-old definition of "done" for a task deserves to be re-asked
#   eventually. Longer than the RunStore's 14-day sweep of finished runs, so a
#   memo outlives the document of the run that produced it -- which is why the
#   provenance status is recorded ON the memo rather than only looked up.
MEMO_MAX_AGE_S = 30 * 86400.0


def normalise(task: str) -> str:
    """The memo key's normal form: strip, collapse whitespace, casefold.

    Deliberately the whole of it. Three operations that cannot change what is
    being asked -- an extra newline from a shell heredoc, a doubled space from
    a copy-paste, a capitalised first word -- and nothing that can. Punctuation
    is NOT stripped and word order is NOT touched, so `"3 pens at 1.25"` and
    `"3 pens at 12.5"` are different keys, and so are `"Compare A to B"` and
    `"compare B to A"`.

    A paraphrase MISSES, and that is the correct outcome: paying six proposer
    calls to discover that two phrasings meant the same thing is cheap next to
    grading one task against another task's definition of done.
    """
    return " ".join(task.split()).casefold()


def key_for(task: str) -> str:
    """Content address of the normalised task. The filename and the index."""
    return hashlib.sha256(normalise(task).encode("utf-8")).hexdigest()[:32]


@dataclass(slots=True)
class TaskMemo:
    """What one completed run learned about how to grade and decompose a task.

    `status` is the provenance run's terminal status, not this memo's. Only
    `"completed"` is reusable; everything else is retained so a reader can say
    WHY a memo was refused rather than reporting a miss it cannot explain.
    """

    task_key: str
    # The raw task of the provenance run. Kept for the audit trail only: the
    # lookup key is the normalised form, and nothing reads this to decide
    # anything. It is what lets an operator see which question a memo came from.
    task: str
    run_id: str
    # The near-match tier's secondary key: `generalise.abstract_fingerprint`
    # of `task`. Stored rather than recomputed on every read so a fingerprint
    # rule change is visible per-entry (an old entry keeps the fingerprint it
    # was written with until it is re-earned) instead of silently reindexing
    # the whole store the moment the code changes.
    task_fingerprint: str = ""
    criterion: dict[str, Any] | None = None
    criterion_hash: str = ""
    plan: dict[str, Any] | None = None
    plan_hash: str = ""
    created_ts: float = 0.0
    updated_ts: float = 0.0
    # Provenance run status. Starts "running"; settled when that run finishes.
    status: str = "running"
    schema_version: int = MEMO_SCHEMA_VERSION
    # sha256 over the canonical payload below, verified on load.
    entry_hash: str = ""

    def payload(self) -> dict[str, Any]:
        """Everything the hash covers. `entry_hash` itself is excluded."""
        return {
            "task_key": self.task_key,
            "task": self.task,
            "run_id": self.run_id,
            "task_fingerprint": self.task_fingerprint,
            "criterion": self.criterion,
            "criterion_hash": self.criterion_hash,
            "plan": self.plan,
            "plan_hash": self.plan_hash,
            "created_ts": self.created_ts,
            "updated_ts": self.updated_ts,
            "status": self.status,
            "schema_version": self.schema_version,
        }

    def content_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(self.payload(), sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:32]

    def to_dict(self) -> dict[str, Any]:
        return {**self.payload(), "entry_hash": self.content_hash()}

    @staticmethod
    def from_dict(data: dict[str, Any]) -> TaskMemo:
        version = int(data.get("schema_version", 0))
        if version != MEMO_SCHEMA_VERSION:
            raise IncompatibleMemo(
                f"memo schema {version}, this build writes {MEMO_SCHEMA_VERSION}"
            )
        return TaskMemo(
            task_key=str(data["task_key"]),
            task=str(data.get("task", "")),
            run_id=str(data.get("run_id", "")),
            task_fingerprint=str(data.get("task_fingerprint", "")),
            criterion=data.get("criterion"),
            criterion_hash=str(data.get("criterion_hash", "")),
            plan=data.get("plan"),
            plan_hash=str(data.get("plan_hash", "")),
            created_ts=float(data.get("created_ts", 0.0)),
            updated_ts=float(data.get("updated_ts", 0.0)),
            status=str(data.get("status", "running")),
            entry_hash=str(data.get("entry_hash", "")),
        )

    # -- admission ----------------------------------------------------------

    def reusable(self, *, now: float, max_age_s: float = MEMO_MAX_AGE_S) -> str:
        """Empty string when this memo may be reused, else the reason it may not.

        A reason string rather than a bool because a memo that is never reused
        is indistinguishable from a memo that never existed, and the difference
        is the whole of the debugging story. `memo_miss_reason` is the number
        to watch, not `memo_hit_rate`.
        """
        if self.status != "completed":
            # The gate that matters. A criterion frozen by a run that then
            # failed its own gate is precisely the criterion not to inherit.
            return f"provenance run {self.run_id} status is {self.status!r}"
        if not self.criterion or not self.criterion_hash:
            return "memo carries no criterion"
        if now - self.created_ts > max_age_s:
            return f"memo is {(now - self.created_ts) / 86400:.1f} days old"
        return ""


class IncompatibleMemo(RuntimeError):
    """The stored memo was written by a different build."""


class MemoStore:
    """Reads and writes task memos. One file per normalised task key.

    ANATOMY: the atomic write
      Identical to `RunStore.save` -- temp file in the SAME directory, fsync,
      `os.replace`. Same directory so the replace is a rename rather than a
      cross-device copy, which would not be atomic. A torn memo is worse than
      no memo: it would be quarantined on the next read, which is correct but
      costs the reuse it existed to provide.

    ANATOMY: quarantine, not delete
      A document whose contents do not hash to its own recorded hash was
      changed by something that is not this code. Deleting it destroys the only
      evidence of that; serving it hands a run a criterion nobody authored.
      Moved aside, exactly as `SkillLibrary` does with a tampered skill.
    """

    def __init__(self, root: str | pathlib.Path | None = None) -> None:
        if root is not None:
            self.root = pathlib.Path(root)
        else:
            env = os.environ.get("SWARMD_MEMO_STORE")
            # Under the RunStore root by default, resolved through RunStore so
            # `SWARMD_RUN_STORE` moves both together -- a test suite or an
            # operator redirecting one and not the other would leave memos
            # pointing at runs that are not there.
            self.root = (
                pathlib.Path(env) if env else RunStore().root / DEFAULT_DIRNAME
            )

    def path_for(self, task_key: str) -> pathlib.Path:
        # Task keys are hex digests this module computes, but `path_for` is not
        # allowed to assume that: the same sanitiser RunStore uses, because a
        # key that reached here from anywhere else is not trusted to be a bare
        # name.
        safe = "".join(c for c in task_key if c.isalnum() or c in "-_")
        return self.root / f"{safe}.json"

    # -- reading ------------------------------------------------------------

    def get(self, task_key: str) -> TaskMemo | None:
        """The memo for a key, or None. Verifies the content hash on load."""
        path = self.path_for(task_key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            memo = TaskMemo.from_dict(data)
        except IncompatibleMemo as exc:
            # Not quarantined: an old-shape document is a version skew, not
            # tampering, and it is the reader that is new.
            logger.info("memo %s ignored: %s", path.name, exc)
            return None
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            logger.warning("memo unreadable (%s): %s", path, exc)
            return None
        if memo.entry_hash != memo.content_hash():
            self.quarantine(task_key, "content hash does not match the payload")
            return None
        # The criterion and plan carry their own hashes, and those are what a
        # run reports and grades against. A payload whose criterion no longer
        # hashes to the recorded hash is the same class of problem, caught
        # before it can become a run's frozen criterion.
        if memo.criterion is not None and not _criterion_matches(memo):
            self.quarantine(task_key, "criterion does not match its recorded hash")
            return None
        return memo

    def by_fingerprint(
        self, fingerprint: str, *, exclude_key: str = ""
    ) -> list[TaskMemo]:
        """Every stored memo sharing this task SHAPE, most recently proven first.

        The near-match tier's lookup: the caller has already missed on the
        exact key and is asking "has ANY task of this shape been solved
        before". Returns candidates, not an answer -- every one of them still
        has to pass `get`'s own hash and provenance checks (folded in via
        `entries`) and then be rebound and re-attacked by the caller before it
        may be trusted; see `swarm/run.py`.

        ANATOMY: a scan over `entries()`, not a separate index file
          `entries()` already walks every document in the store to prune and
          to verify each one's content hash, so a memo store is small enough
          to read in full on every call -- the same reasoning `SkillLibrary`
          gives for staying JSON-file backed rather than a database. A second
          on-disk structure mapping fingerprint to task keys would need its
          own atomic-write discipline and could itself drift out of sync with
          the documents it claims to index (a memo deleted without its index
          row updated would serve a candidate `get` immediately quarantines
          on read). A query over `entries()` cannot drift, because it has
          nothing of its own to drift from.
        """
        if not fingerprint:
            return []
        matches = [
            m for m in self.entries()
            if m.task_fingerprint == fingerprint and m.task_key != exclude_key
        ]
        matches.sort(key=lambda m: -m.updated_ts)
        return matches

    # -- writing ------------------------------------------------------------

    def put(self, memo: TaskMemo) -> None:
        memo.entry_hash = memo.content_hash()
        path = self.path_for(memo.task_key)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(memo.to_dict(), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            os.replace(temp_name, path)
        except OSError as exc:
            # A memo that cannot be written costs a future run six calls. That
            # is a saving lost, not a run broken, so it never raises.
            logger.warning("memo not saved (%s): %s", path, exc)

    def remember(
        self,
        *,
        task: str,
        run_id: str,
        criterion: dict[str, Any] | None = None,
        criterion_hash: str = "",
        plan: dict[str, Any] | None = None,
        plan_hash: str = "",
        now: float | None = None,
    ) -> TaskMemo | None:
        """Record (or extend) this run's memo for a task. Returns what was kept.

        WHY A COMPLETED MEMO IS NEVER OVERWRITTEN by a run in flight: the entry
        on disk has already graded real work and passed; the writer here has
        frozen a criterion and proved nothing yet. Two runs of the same task
        overlapping is the ordinary case for a service, and letting the later
        one replace a proven memo with an unproven one would make the feature
        weakest exactly when it is used most.
        """
        now = now if now is not None else time.time()
        key = key_for(task)
        existing = self.get(key)
        if (
            existing is not None
            and existing.run_id != run_id
            and existing.reusable(now=now) == ""
        ):
            return existing

        memo = existing if existing is not None and existing.run_id == run_id else None
        if memo is None:
            memo = TaskMemo(
                task_key=key,
                task=task,
                run_id=run_id,
                task_fingerprint=abstract_fingerprint(task),
                created_ts=now,
            )
        if criterion is not None:
            memo.criterion = criterion
            memo.criterion_hash = criterion_hash
        if plan is not None:
            memo.plan = plan
            memo.plan_hash = plan_hash
        memo.updated_ts = now
        self.put(memo)
        return memo

    def record_outcome(self, *, task: str, run_id: str, status: str) -> None:
        """Settle the memo a run wrote with the status that run reached.

        Only the run that WROTE the entry may settle it -- a second run of the
        same task failing must not retire the first run's proven memo, and a
        second run succeeding must not certify a criterion it did not freeze.

        A non-completed outcome DELETES the entry rather than marking it. There
        is nothing to keep: the memo is unusable by `reusable()` forever after,
        and leaving it on disk means the next run of this task finds a
        permanently-refused entry instead of writing its own.
        """
        key = key_for(task)
        memo = self.get(key)
        if memo is None or memo.run_id != run_id:
            return
        if status != "completed":
            self.delete(key)
            return
        memo.status = status
        memo.updated_ts = time.time()
        self.put(memo)

    def delete(self, task_key: str) -> None:
        try:
            self.path_for(task_key).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not delete memo: %s", exc)

    def quarantine(self, task_key: str, reason: str) -> None:
        """Move a suspect document aside. Never deleted -- it is evidence."""
        path = self.path_for(task_key)
        target = self.root / "quarantine" / path.name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, target)
            logger.warning("memo %s quarantined: %s", path.name, reason)
        except OSError as exc:
            logger.warning("could not quarantine memo %s: %s", path, exc)

    # -- maintenance --------------------------------------------------------

    def entries(self) -> list[TaskMemo]:
        if not self.root.exists():
            return []
        out: list[TaskMemo] = []
        for path in self.root.glob("*.json"):
            memo = self.get(path.stem)
            if memo is not None:
                out.append(memo)
        return out

    def prune(
        self,
        *,
        older_than_s: float = MEMO_MAX_AGE_S,
        now: float | None = None,
    ) -> int:
        """Drop memos past their useful life. Returns the count.

        Safe to lose by construction: a memo is a saving, never a record. The
        ledger is the durable record (ADR-007) and a pruned memo costs the next
        run of that task six proposer calls, which is what it would have cost
        anyway before this module existed.
        """
        now = now if now is not None else time.time()
        dropped = 0
        for memo in self.entries():
            if now - memo.created_ts <= older_than_s:
                continue
            self.delete(memo.task_key)
            dropped += 1
        return dropped


def _criterion_matches(memo: TaskMemo) -> bool:
    """True when the stored criterion still hashes to its recorded hash.

    Imported lazily so this module stays importable without the criteria stack
    -- the store is also read by CLI paths that never build a run.
    """
    from swarmd.swarm.criteria import CheckError, Criterion

    if not memo.criterion_hash:
        return False
    try:
        return (
            Criterion.from_dict(memo.criterion or {}).content_hash()
            == memo.criterion_hash
        )
    except (CheckError, TypeError, ValueError):
        return False
