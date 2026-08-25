"""Durable human-in-the-loop approvals.

Design notes:

- AWAITING_APPROVAL is a persisted pipeline state, not an in-memory callback.
  The process can die at review time; on restart, pending approvals are reloaded
  from the store and the pipeline resumes exactly where it paused.
- The audit trail is append-only: decisions are never updated or deleted, only
  superseded by new entries. This is what makes the trail trustworthy evidence.
- Storage is pluggable (in-memory dict for tests/CI, asyncpg for real runs) via
  a tiny protocol — the state machine logic is identical either way.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol


class ApprovalState(str, Enum):
    PENDING = "PENDING"  # == AWAITING_APPROVAL
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDITED = "EDITED"  # approved with modifications


@dataclass(slots=True)
class ApprovalRequest:
    request_id: str
    item: dict[str, Any]
    stage: str
    created_ts: float = field(default_factory=time.time)
    state: ApprovalState = ApprovalState.PENDING


@dataclass(frozen=True, slots=True)
class AuditEntry:
    ts: float
    request_id: str
    action: str  # approve / reject / edit / submit
    actor: str
    detail: str = ""


class ApprovalStore(Protocol):
    """Persistence boundary for approvals + audit trail."""

    async def save_request(self, req: ApprovalRequest) -> None: ...
    async def get_request(self, request_id: str) -> ApprovalRequest | None: ...
    async def list_pending(self) -> list[ApprovalRequest]: ...
    async def append_audit(self, entry: AuditEntry) -> None: ...
    async def read_audit(self) -> list[AuditEntry]: ...


class InMemoryApprovalStore:
    """Dict-backed store for tests and offline demos. Same contract as SQL."""

    def __init__(self) -> None:
        self._requests: dict[str, ApprovalRequest] = {}
        self._audit: list[AuditEntry] = []

    async def save_request(self, req: ApprovalRequest) -> None:
        self._requests[req.request_id] = req

    async def get_request(self, request_id: str) -> ApprovalRequest | None:
        return self._requests.get(request_id)

    async def list_pending(self) -> list[ApprovalRequest]:
        return [
            r for r in self._requests.values() if r.state is ApprovalState.PENDING
        ]

    async def append_audit(self, entry: AuditEntry) -> None:
        self._audit.append(entry)

    async def read_audit(self) -> list[AuditEntry]:
        return list(self._audit)


class ApprovalManager:
    """State machine over persisted approval requests."""

    def __init__(self, store: ApprovalStore) -> None:
        self.store = store

    async def submit(self, item: dict[str, Any], stage: str) -> ApprovalRequest:
        req = ApprovalRequest(
            request_id=uuid.uuid4().hex[:12], item=item, stage=stage
        )
        await self.store.save_request(req)
        await self.store.append_audit(
            AuditEntry(
                ts=time.time(),
                request_id=req.request_id,
                action="submit",
                actor="system",
                detail=f"stage={stage}",
            )
        )
        return req

    async def decide(
        self,
        request_id: str,
        action: str,
        actor: str,
        edited_item: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        req = await self.store.get_request(request_id)
        if req is None:
            raise KeyError(f"unknown request: {request_id}")
        if req.state is not ApprovalState.PENDING:
            raise ValueError(
                f"request {request_id} already decided ({req.state.value}); "
                "decisions are immutable — submit a new request instead"
            )

        if action == "approve":
            req.state = ApprovalState.APPROVED
        elif action == "reject":
            req.state = ApprovalState.REJECTED
        elif action == "edit":
            if edited_item is None:
                raise ValueError("edit requires edited_item")
            req.state = ApprovalState.EDITED
            req.item = edited_item
        else:
            raise ValueError(f"unknown action: {action!r}")

        await self.store.save_request(req)
        await self.store.append_audit(
            AuditEntry(
                ts=time.time(),
                request_id=request_id,
                action=action,
                actor=actor,
                detail="",
            )
        )
        return req

    async def pending(self) -> list[ApprovalRequest]:
        return await self.store.list_pending()

    async def audit(self) -> list[AuditEntry]:
        return await self.store.read_audit()
