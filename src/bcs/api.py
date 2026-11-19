"""Authenticated FastAPI control plane for BCS.

Run with: uvicorn bcs.api:create_app --factory
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from enum import Enum
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .approvals import ApprovalError, ApprovalStatus
from .auth import (
    AuthenticationError, Principal, TokenAuthenticator,
)
from .runtime import BCSRuntime, create_runtime


def _json_value(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_value(item) for key, item in value.items()}
    return value


def _task_view(record) -> dict[str, Any]:
    return {
        "task": _json_value(asdict(record.task)),
        "state": record.state.value,
        "assigned_worker_id": record.assigned_worker_id,
        "result": None if record.result is None else {
            "status": record.result.status.value,
            "summary": record.result.summary,
            "changed_files": list(record.result.changed_files),
            "evidence_refs": list(record.result.evidence_refs),
            "blockers": list(record.result.blockers),
        },
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


def _approval_view(record) -> dict[str, Any]:
    return {
        "request_id": record.request_id,
        "task_id": record.task_id,
        "worker_id": record.worker_id,
        "capability": record.capability,
        "status": record.status.value,
        "created_at": record.created_at.isoformat(),
        "expires_at": record.expires_at.isoformat(),
        "decided_at": None if record.decided_at is None else record.decided_at.isoformat(),
        "decided_by": record.decided_by,
        "reason": record.reason,
    }


class ApprovalDecisionInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    approved: bool
    reason: str = Field(default="", max_length=1000)


def create_app(
    *,
    runtime: BCSRuntime | None = None,
    authenticator: TokenAuthenticator | None = None,
) -> FastAPI:
    """Create the BCS API with injected or environment-configured services."""
    owns_runtime = runtime is None
    active_runtime = runtime or create_runtime()
    active_authenticator = authenticator or TokenAuthenticator.from_environment()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        if owns_runtime:
            active_runtime.close()

    app = FastAPI(
        title="Builder Control System API",
        version="0.1.0",
        description="Authenticated control plane for BCS orchestration and approvals.",
        lifespan=lifespan,
    )
    app.state.bcs_runtime = active_runtime
    app.state.bcs_authenticator = active_authenticator

    def require_principal(request: Request) -> Principal:
        try:
            return request.app.state.bcs_authenticator.authenticate(
                request.headers.get("authorization")
            )
        except AuthenticationError as exc:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Valid bearer authentication is required.",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc
        except RuntimeError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="API authentication is not configured.",
            ) from exc

    def require_scope(principal: Principal, scope: str) -> None:
        if scope not in principal.scopes:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Insufficient permission.",
            )

    @app.get("/health")
    def health():
        return {"status": "ok", "service": "bcs-api"}

    @app.get("/v1/tasks")
    def list_tasks(principal: Principal = Depends(require_principal)):
        require_scope(principal, "tasks:read")
        snapshot = active_runtime.orchestrator.snapshot()
        return {
            "tasks": [_task_view(record) for record in snapshot.tasks],
            "escalations": [_json_value(asdict(item)) for item in snapshot.escalations],
        }

    @app.get("/v1/approvals")
    def list_approvals(principal: Principal = Depends(require_principal)):
        require_scope(principal, "approvals:read")
        records = active_runtime.store.list_approvals()
        return {"approvals": [_approval_view(record) for record in records]}

    @app.post("/v1/approvals/{request_id}/decision")
    def decide_approval(
        request_id: str,
        decision: ApprovalDecisionInput,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "approvals:decide")
        if principal.actor_type.value != "human":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an authenticated human principal may decide approvals.",
            )
        try:
            record = active_runtime.approvals.decide(
                request_id,
                actor_id=principal.actor_id,
                actor_type=principal.actor_type,
                approved=decision.approved,
                reason=decision.reason,
            )
        except ApprovalError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=str(exc)
            ) from exc
        return {"approval": _approval_view(record)}

    @app.get("/v1/feedback/{task_id}")
    def get_feedback(
        task_id: str,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "tasks:read")
        snapshot = active_runtime.feedback.get_snapshot(task_id)
        if snapshot is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Feedback history not found.")
        return {
            "task_id": snapshot.task_id,
            "state": snapshot.state.value,
            "cycles_remaining": snapshot.cycles_remaining,
            "cycles": [
                {
                    "qa_run_id": cycle.qa_run_id,
                    "cycle_number": cycle.cycle_number,
                    "report_status": cycle.report_status.value,
                    "state": cycle.state.value,
                    "instruction": cycle.instruction,
                }
                for cycle in snapshot.cycles
            ],
        }

    return app


__all__ = ["create_app"]
