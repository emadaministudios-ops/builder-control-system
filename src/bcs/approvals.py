"""Human approval lifecycle for exact, fingerprinted tool requests.

ApprovalService can use an injected durable repository. The SQLite adapter is
the current local/development implementation; production should use PostgreSQL.
Actor identity/type must come from authenticated server-side identity, never
request payload fields supplied by a worker.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum
from threading import RLock

from .policy import Approval, ToolRequest, request_fingerprint


class ApprovalStatus(str, Enum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class ActorType(str, Enum):
    HUMAN = "human"
    WORKER = "worker"
    SYSTEM = "system"


@dataclass(frozen=True)
class ApprovalRequest:
    request_id: str
    task_id: str
    worker_id: str
    capability: str
    request_fingerprint: str
    status: ApprovalStatus
    created_at: datetime
    expires_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    reason: str | None = None


class ApprovalError(ValueError):
    """Raised when an approval lifecycle transition is invalid."""


class ApprovalService:
    """Thread-safe approval state machine; identity must be authenticated upstream."""

    def __init__(self, ttl: timedelta = timedelta(minutes=15), *, store=None) -> None:
        if ttl <= timedelta(0):
            raise ValueError("Approval TTL must be positive.")
        if store is not None and not all(
            callable(getattr(store, name, None))
            for name in ("list_approvals", "save_approval")
        ):
            raise TypeError("store must implement the approval repository contract.")
        self._ttl = ttl
        self._store = store
        self._records: dict[str, ApprovalRequest] = {}
        self._lock = RLock()
        if self._store is not None:
            self._records = {
                record.request_id: record for record in self._store.list_approvals()
            }

    def submit(self, request: ToolRequest, *, now: datetime | None = None) -> ApprovalRequest:
        if not request.request_id or not request.task_id or not request.worker_id:
            raise ApprovalError("Request identity fields are required.")
        fingerprint = request_fingerprint(request)
        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            raise ApprovalError("Approval timestamps must include a timezone.")

        with self._lock:
            existing = self._records.get(request.request_id)
            if existing is not None:
                if existing.request_fingerprint != fingerprint:
                    raise ApprovalError("Request ID is already bound to different arguments.")
                return existing

            record = ApprovalRequest(
                request_id=request.request_id,
                task_id=request.task_id,
                worker_id=request.worker_id,
                capability=request.capability,
                request_fingerprint=fingerprint,
                status=ApprovalStatus.PENDING,
                created_at=created_at,
                expires_at=created_at + self._ttl,
            )
            if self._store is not None:
                self._store.save_approval(record)
            self._records[request.request_id] = record
            return record

    def decide(
        self,
        request_id: str,
        *,
        actor_id: str,
        actor_type: ActorType,
        approved: bool,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApprovalRequest:
        if actor_type != ActorType.HUMAN:
            raise ApprovalError("Only an authenticated human may decide an approval.")
        if not actor_id.strip():
            raise ApprovalError("Approver identity is required.")
        decided_at = now or datetime.now(timezone.utc)
        if decided_at.tzinfo is None:
            raise ApprovalError("Approval timestamps must include a timezone.")

        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise ApprovalError("Approval request does not exist.")
            if record.status != ApprovalStatus.PENDING:
                raise ApprovalError("Only pending approval requests can be decided.")
            if record.expires_at <= decided_at:
                expired = ApprovalRequest(
                    **{**record.__dict__, "status": ApprovalStatus.EXPIRED}
                )
                if self._store is not None:
                    self._store.save_approval(expired)
                self._records[request_id] = expired
                raise ApprovalError("Approval request has expired.")

            updated = ApprovalRequest(
                **{
                    **record.__dict__,
                    "status": ApprovalStatus.APPROVED if approved else ApprovalStatus.REJECTED,
                    "decided_at": decided_at,
                    "decided_by": actor_id,
                    "reason": reason.strip() or None,
                }
            )
            if self._store is not None:
                self._store.save_approval(updated)
            self._records[request_id] = updated
            return updated

    def cancel(
        self,
        request_id: str,
        *,
        actor_id: str,
        actor_type: ActorType,
        reason: str = "",
        now: datetime | None = None,
    ) -> ApprovalRequest:
        """Cancel a pending request; only an authenticated human may cancel it."""
        if actor_type != ActorType.HUMAN:
            raise ApprovalError("Only an authenticated human may cancel an approval.")
        if not isinstance(actor_id, str) or not actor_id.strip():
            raise ApprovalError("Cancelling actor identity is required.")
        cancelled_at = now or datetime.now(timezone.utc)
        if cancelled_at.tzinfo is None:
            raise ApprovalError("Approval timestamps must include a timezone.")
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                raise ApprovalError("Approval request does not exist.")
            if record.status != ApprovalStatus.PENDING:
                raise ApprovalError("Only pending approval requests can be cancelled.")
            if record.expires_at <= cancelled_at:
                expired = ApprovalRequest(
                    **{**record.__dict__, "status": ApprovalStatus.EXPIRED}
                )
                if self._store is not None:
                    self._store.save_approval(expired)
                self._records[request_id] = expired
                raise ApprovalError("Approval request has expired.")
            cancelled = ApprovalRequest(
                **{
                    **record.__dict__,
                    "status": ApprovalStatus.CANCELLED,
                    "decided_at": cancelled_at,
                    "decided_by": actor_id,
                    "reason": reason.strip() or "Cancelled by human approver.",
                }
            )
            if self._store is not None:
                self._store.save_approval(cancelled)
            self._records[request_id] = cancelled
            return cancelled

    def get(self, request_id: str, *, now: datetime | None = None) -> ApprovalRequest | None:
        current_time = now or datetime.now(timezone.utc)
        if current_time.tzinfo is None:
            raise ApprovalError("Lookup timestamps must include a timezone.")
        with self._lock:
            record = self._records.get(request_id)
            if record is None:
                return None
            if record.status == ApprovalStatus.PENDING and record.expires_at <= current_time:
                record = ApprovalRequest(
                    **{**record.__dict__, "status": ApprovalStatus.EXPIRED}
                )
                if self._store is not None:
                    self._store.save_approval(record)
                self._records[request_id] = record
            return record

    def policy_approval(
        self, request_id: str, *, now: datetime | None = None
    ) -> Approval | None:
        """Return the policy engine's trusted approval only while still valid."""
        record = self.get(request_id, now=now)
        if record is None or record.status != ApprovalStatus.APPROVED:
            return None
        assert record.decided_by is not None
        return Approval(
            request_id=record.request_id,
            request_fingerprint=record.request_fingerprint,
            status=record.status.value,
            approver_id=record.decided_by,
            expires_at=record.expires_at,
        )
