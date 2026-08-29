"""Idempotency keys for run submission: one key, one run, forever.

WHY THIS EXISTS. `POST /api/runs` answers 202 and starts a background task, so
the client learns nothing about whether its request arrived until the response
comes back. A dropped response, a proxy timeout, a double-clicked button or a
retrying CI job therefore produces TWO runs -- two populations, two criteria,
two plans, and twice the provider quota for one question. On a measured budget
of ~1,146 requests/day (docs/CAPACITY.md) a duplicated `standard` run is most
of an afternoon, spent discovering something already known.

The contract, deliberately the ordinary HTTP one so existing clients already
know it:

    no header                -> current behaviour, a new run every time
    same key, same body      -> 200 with the ORIGINAL run_id, Idempotent-Replay
    same key, different body -> 422 naming the conflict, no run started
    key that fails KEY_RE    -> 400

NO BODY-HASH FALLBACK, and no environment flag to enable one. "No header" must
always mean "a new run", because re-running an identical task on purpose -- an
A/B arm, a flake hunt, a chaos comparison -- is a legitimate and common thing
to do, and an operator who forgot the header must never silently be handed
yesterday's run id instead of the run they asked for.

DURABILITY. Records are files under the RunStore root, written with RunStore's
exact discipline (temp file in the same directory, fsync, `os.replace`). A
retry that arrives after a deploy is precisely the retry worth deduplicating,
so an in-memory dict would deduplicate only the cases that did not matter.

STATED LIMIT, not solved here: the lock and the file store are per-pod. Two
replicas behind one ingress can still both accept the same key, because a stat
and a write are not atomic across machines. `IdempotencyStore` is deliberately
a narrow surface (get/reserve/complete/release/lock) so a Redis or Postgres
implementation drops in behind it later. That is a follow-up, not this change.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Any

from swarmd.swarm.runstore import RunStore

logger = logging.getLogger(__name__)

# ANATOMY: IDEMPOTENCY_TTL_S (24 hours)
#   How long a key is honoured. Long enough to cover every retry a client
#   actually makes -- an HTTP retry budget is seconds, a CI re-run is minutes,
#   an operator re-issuing a curl is hours -- and short enough that a key
#   reused next week for a different question is treated as new rather than
#   replaying something unrelated. A record also ages out with the RUN it
#   points to (see `prune`), whichever comes first.
IDEMPOTENCY_TTL_S = 86_400.0

# How long a "pending" reservation is believed. Reservations are released on
# failure in the same request, so a pending record older than this can only
# come from a process that died mid-construction (or another pod), and holding
# a key hostage forever because of a crash is worse than accepting the small
# chance of a duplicate.
PENDING_STALE_S = 120.0

# Deliberately the charset Stripe-style keys use (UUIDs, ULIDs, hashes, dotted
# job ids), with a floor of 8 characters. The floor is not cosmetic: a one- or
# two-character key from a client that "just picks something" collides across
# unrelated requests, and a collision here means one caller is handed another
# caller's run id.
KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{8,200}$")

DEFAULT_DIRNAME = "idempotency"


class ConflictingKey(ValueError):
    """The key was used before, for a different request body."""


class KeyInFlight(RuntimeError):
    """Another request holds this key and has not finished starting its run."""


def fingerprint(endpoint: str, payload: dict[str, Any]) -> str:
    """Stable hash of what was asked for.

    The ENDPOINT is part of it, so one key used against `/api/runs` and against
    `/api/runs/{id}/resume` conflicts loudly instead of replaying a submit
    response to a resume caller.

    `sort_keys` because a JSON body's field order is not meaningful and two
    clients serialising the same request differently are making the same
    request.
    """
    blob = json.dumps(
        {"endpoint": endpoint, "payload": payload},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


@dataclass(slots=True)
class IdemRecord:
    """One key's outcome, exactly as it was first answered."""

    entry_key: str
    body_fingerprint: str
    run_id: str = ""
    # The response that was returned the first time, stored verbatim. Replaying
    # a REBUILT response would let the two answers drift -- the second caller
    # would get today's field set for yesterday's run.
    status_code: int = 202
    body: dict[str, Any] = field(default_factory=dict)
    created_ts: float = 0.0
    state: str = "pending"  # pending | done

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_key": self.entry_key,
            "body_fingerprint": self.body_fingerprint,
            "run_id": self.run_id,
            "status_code": self.status_code,
            "body": self.body,
            "created_ts": self.created_ts,
            "state": self.state,
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> IdemRecord:
        return IdemRecord(
            entry_key=str(data["entry_key"]),
            body_fingerprint=str(data["body_fingerprint"]),
            run_id=str(data.get("run_id", "")),
            status_code=int(data.get("status_code", 202)),
            body=dict(data.get("body") or {}),
            created_ts=float(data.get("created_ts", 0.0)),
            state=str(data.get("state", "pending")),
        )


class IdempotencyStore:
    """Durable key -> response records, one file per key.

    ANATOMY: the per-key lock
      Construction of a `SwarmRun` and the write of its record are not atomic,
      so two simultaneous requests with one key could both find no record and
      both start a run. The lock closes that window WITHIN a process, which is
      the window that actually opens under a double-click or a retrying client
      hitting the same pod. Per key, not global: one client's slow submission
      must not serialise every other caller's.

    ANATOMY: the digest in the filename
      The RunStore sanitiser drops `.` and `:`, which `KEY_RE` allows, so
      `a.b-key` and `ab-key` would sanitise to the same name and one caller
      would replay the other's run. The sanitised prefix is kept for a human
      reading the directory; the sha256 suffix is what makes the name unique.
    """

    def __init__(self, root: str | pathlib.Path | None = None) -> None:
        if root is not None:
            self.root = pathlib.Path(root)
        else:
            env = os.environ.get("SWARMD_IDEMPOTENCY_STORE")
            self.root = (
                pathlib.Path(env) if env else RunStore().root / DEFAULT_DIRNAME
            )
        self._locks: dict[str, asyncio.Lock] = {}

    # -- keys ---------------------------------------------------------------

    def lock(self, entry_key: str) -> asyncio.Lock:
        """The lock for one key, created on demand. Process-local by design."""
        existing = self._locks.get(entry_key)
        if existing is None:
            existing = self._locks[entry_key] = asyncio.Lock()
        return existing

    def path_for(self, entry_key: str) -> pathlib.Path:
        safe = "".join(c for c in entry_key if c.isalnum() or c in "-_")[:64]
        digest = hashlib.sha256(entry_key.encode("utf-8")).hexdigest()[:16]
        return self.root / f"{safe}-{digest}.json"

    # -- records ------------------------------------------------------------

    def get(self, entry_key: str, *, now: float | None = None) -> IdemRecord | None:
        """The record for a key, or None when unseen or expired.

        An expired record is reported as absent rather than deleted here:
        deletion is `prune`'s job, and a read path that mutates the store would
        make a GET-shaped operation fail on a read-only filesystem.
        """
        path = self.path_for(entry_key)
        if not path.exists():
            return None
        try:
            record = IdemRecord.from_dict(
                json.loads(path.read_text(encoding="utf-8"))
            )
        except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.warning("idempotency record unreadable (%s): %s", path, exc)
            return None
        now = now if now is not None else time.time()
        if now - record.created_ts > IDEMPOTENCY_TTL_S:
            return None
        return record

    def reserve(self, entry_key: str, body_fingerprint: str) -> IdemRecord:
        """Claim a key before the run exists, so a crash is visible as pending."""
        record = IdemRecord(
            entry_key=entry_key,
            body_fingerprint=body_fingerprint,
            created_ts=time.time(),
            state="pending",
        )
        self._write(record)
        return record

    def complete(
        self,
        entry_key: str,
        *,
        run_id: str,
        status_code: int,
        body: dict[str, Any],
    ) -> IdemRecord:
        """Record the response this key will replay from now on."""
        existing = self.get(entry_key)
        record = IdemRecord(
            entry_key=entry_key,
            body_fingerprint=(
                existing.body_fingerprint if existing is not None else ""
            ),
            run_id=run_id,
            status_code=status_code,
            body=dict(body),
            created_ts=(
                existing.created_ts if existing is not None else time.time()
            ),
            state="done",
        )
        self._write(record)
        return record

    def release(self, entry_key: str) -> None:
        """Drop a reservation whose run could not be constructed.

        Without this a failed submission would hold its key in `pending` until
        the TTL expired, so a client retrying after a transient 503 would be
        told its run was already being created -- forever, and for a run that
        does not exist.
        """
        try:
            self.path_for(entry_key).unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("could not release idempotency key: %s", exc)

    def _write(self, record: IdemRecord) -> None:
        path = self.path_for(record.entry_key)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w", encoding="utf-8", dir=self.root, delete=False, suffix=".tmp"
            ) as handle:
                json.dump(record.to_dict(), handle, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
                temp_name = handle.name
            os.replace(temp_name, path)
        except OSError as exc:
            # A key that cannot be persisted degrades to today's behaviour: the
            # run still starts. Failing the submission instead would turn a
            # disk problem into a refusal to work.
            logger.warning("idempotency record not saved (%s): %s", path, exc)

    # -- maintenance --------------------------------------------------------

    def records(self) -> list[IdemRecord]:
        if not self.root.exists():
            return []
        out: list[IdemRecord] = []
        for path in self.root.glob("*.json"):
            try:
                out.append(
                    IdemRecord.from_dict(
                        json.loads(path.read_text(encoding="utf-8"))
                    )
                )
            except Exception:  # noqa: BLE001 - one bad file must not hide the rest
                logger.warning("skipping unreadable idempotency record %s", path)
        return out

    def prune(
        self,
        *,
        older_than_s: float = IDEMPOTENCY_TTL_S,
        run_store: RunStore | None = None,
        now: float | None = None,
    ) -> int:
        """Drop expired keys, and keys whose run is gone. Returns the count.

        A record points at a run id and promises to keep answering with it. Once
        that run's document has been swept from the RunStore, the promise is to
        a run nothing can describe any more -- so the key ages out WITH the run
        rather than outliving it and replaying an id no endpoint can resolve.
        """
        now = now if now is not None else time.time()
        dropped = 0
        for record in self.records():
            expired = now - record.created_ts > older_than_s
            orphaned = (
                run_store is not None
                and record.state == "done"
                and bool(record.run_id)
                and not run_store.path_for(record.run_id).exists()
            )
            if not (expired or orphaned):
                continue
            self.release(record.entry_key)
            dropped += 1
        return dropped


def resolve(
    store: IdempotencyStore,
    *,
    entry_key: str,
    body_fingerprint: str,
    now: float | None = None,
) -> IdemRecord | None:
    """Decide what an incoming key means. Call while holding its lock.

    Returns the record to REPLAY, or None when the caller should go on to start
    a run (having reserved the key). Raises `ConflictingKey` when the same key
    carries a different body, and `KeyInFlight` when another process is still
    constructing this key's run.
    """
    now = now if now is not None else time.time()
    record = store.get(entry_key, now=now)
    if record is None:
        store.reserve(entry_key, body_fingerprint)
        return None

    if record.body_fingerprint != body_fingerprint:
        # Refused BEFORE anything is started, and the caller is deliberately
        # not told the other run's id: a client that reused a key by accident
        # has no claim on the run that key already names, and handing the id
        # over would let a key guess become a run disclosure.
        raise ConflictingKey(
            f"Idempotency-Key {entry_key!r} was already used for a different "
            f"request body. Retry the identical request, or use a new key."
        )

    if record.state == "done":
        return record

    if now - record.created_ts <= PENDING_STALE_S:
        raise KeyInFlight(
            f"a run for Idempotency-Key {entry_key!r} is still being created; "
            f"retry in a moment"
        )

    # Stale reservation: whoever held it died between reserve and complete.
    # Re-claimed rather than left to expire, because the alternative is a key
    # that answers "in flight" for a run that will never exist.
    store.reserve(entry_key, body_fingerprint)
    return None
