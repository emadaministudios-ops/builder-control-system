"""Authenticated FastAPI control plane for BCS.

Run with: uvicorn bcs.api:create_app --factory
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import datetime
from enum import Enum
import os
from typing import Any
from uuid import uuid4

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from .approvals import ApprovalError, ApprovalStatus
from .auth import (
    AuthenticationError, Principal, TokenAuthenticator,
)
from .runtime import BCSRuntime, create_runtime
from .execution_jobs import ExecutionJobService
from .policy import OperatingMode


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


class TaskCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)


class TaskDispatchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_ids: list[str] = Field(min_length=1, max_length=100)


class PlanCreateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    objective: str = Field(min_length=1, max_length=20_000)
    acceptance_criteria: list[str] = Field(default_factory=list, max_length=50)


def _git_manager_from_environment():
    """Build the trusted Git manager only from operator-controlled environment."""
    repository_root = os.environ.get("BCS_GIT_REPOSITORY_ROOT")
    worktree_root = os.environ.get("BCS_GIT_WORKTREE_ROOT")
    if not repository_root and not worktree_root:
        return None
    if not repository_root or not worktree_root:
        raise RuntimeError(
            "Both BCS_GIT_REPOSITORY_ROOT and BCS_GIT_WORKTREE_ROOT must be configured."
        )
    from .git_worktrees import GitWorktreeError, GitWorktreeManager
    try:
        return GitWorktreeManager(
            repository_root=repository_root,
            worktree_root=worktree_root,
            base_ref="development",
        )
    except (GitWorktreeError, OSError, ValueError) as exc:
        raise RuntimeError("Trusted BCS Git integration could not be initialized.") from exc


def _register_default_workers(runtime: BCSRuntime, git_manager) -> tuple[str, ...]:
    """Register standard specialists against controller-owned isolated worktrees."""
    if git_manager is None or not runtime.providers.provider_ids():
        return ()
    mode_name = os.environ.get("BCS_WORKER_MODE", "autonomous").strip().lower()
    try:
        mode = OperatingMode(mode_name)
    except ValueError as exc:
        raise RuntimeError("BCS_WORKER_MODE must be supervised or autonomous.") from exc
    roles = (
        "requirements-analyst", "architect", "frontend-developer",
        "backend-developer", "integration-engineer", "qa-engineer",
        "documentation-agent", "builder", "research-helper",
    )
    registered = []
    for role in roles:
        worker_id = "worker-" + role
        runtime.register_model_worker(
            worker_id=worker_id,
            role=role,
            workspace_factory=git_manager.workspace_for_task,
            mode=mode,
            capabilities=frozenset({"workspace.read", "workspace.edit"}),
        )
        registered.append(worker_id)
    return tuple(registered)


def _dispatch_view(outcome) -> dict[str, Any]:
    result = outcome.result
    return {
        "task_id": outcome.task_id,
        "state": outcome.state.value,
        "worker_id": outcome.worker_id,
        "reason": outcome.reason,
        "result": None if result is None else {
            "status": result.status.value,
            "summary": result.summary,
            "changed_files": list(result.changed_files),
            "evidence_refs": list(result.evidence_refs),
            "blockers": list(result.blockers),
        },
    }


def create_app(
    *,
    runtime: BCSRuntime | None = None,
    authenticator: TokenAuthenticator | None = None,
    git_manager=None,
    execution_job_service: ExecutionJobService | None = None,
) -> FastAPI:
    """Create the BCS API with injected or environment-configured services."""
    owns_runtime = runtime is None
    active_runtime = runtime or create_runtime()
    active_authenticator = authenticator or TokenAuthenticator.from_environment()
    active_git_manager = git_manager if git_manager is not None else _git_manager_from_environment()
    registered_workers = _register_default_workers(active_runtime, active_git_manager)
    execution_handler = None
    if active_git_manager is not None and active_runtime.providers.provider_ids():
        mode_name = os.environ.get("BCS_WORKER_MODE", "autonomous").strip().lower()
        try:
            qa_mode = OperatingMode(mode_name)
        except ValueError as exc:
            raise RuntimeError("BCS_WORKER_MODE must be supervised or autonomous.") from exc
        qa_supervisor = active_runtime.create_qa_supervisor(
            workspace_factory=active_git_manager.workspace_for_task,
            mode=qa_mode,
        )
        execution_handler = lambda job: active_runtime.execute_plan_with_qa(
            job["task_ids"], qa_supervisor
        )
    active_job_service = execution_job_service or ExecutionJobService(
        active_runtime.store, active_runtime.scheduler,
        execution_handler=execution_handler,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        active_job_service.start()
        try:
            yield
        finally:
            runner_stopped = active_job_service.stop(timeout=10)
            if owns_runtime and runner_stopped:
                active_runtime.close()

    app = FastAPI(
        title="Builder Control System API",
        version="0.1.0",
        description="Authenticated control plane for BCS orchestration and approvals.",
        lifespan=lifespan,
    )
    app.state.bcs_runtime = active_runtime
    app.state.bcs_authenticator = active_authenticator
    # The Git manager is trusted server configuration; clients cannot supply paths or repositories.
    app.state.bcs_git_manager = active_git_manager
    app.state.bcs_registered_workers = registered_workers
    app.state.bcs_execution_jobs = active_job_service

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

    @app.post("/v1/tasks", status_code=status.HTTP_201_CREATED)
    def create_task(
        payload: TaskCreateInput,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "tasks:create")
        if principal.actor_type.value != "human":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an authenticated human principal may create top-level tasks.",
            )
        criteria = payload.acceptance_criteria or [
            "Satisfy the original user objective without weakening its explicit constraints.",
            "Provide QA evidence and report unresolved limitations honestly.",
        ]
        try:
            record = active_runtime.orchestrator.create_task(
                task_id="task_" + uuid4().hex,
                objective=payload.objective,
                acceptance_criteria=criteria,
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        return {"task": _task_view(record)}

    @app.post("/v1/plans", status_code=status.HTTP_201_CREATED)
    def create_plan(
        payload: PlanCreateInput,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "tasks:create")
        if principal.actor_type.value != "human":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an authenticated human principal may create top-level plans.",
            )
        criteria = payload.acceptance_criteria or [
            "Satisfy the original user objective without weakening its explicit constraints.",
            "Provide QA evidence and report unresolved limitations honestly.",
        ]
        try:
            plan, records = active_runtime.plan_and_create_tasks(
                payload.objective, criteria
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="BCS could not create a valid task plan.",
            ) from exc
        return {
            "plan": {
                "objective": plan.objective,
                "acceptance_criteria": list(plan.acceptance_criteria),
                "execution_order": list(plan.execution_order),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "role": step.role,
                        "objective": step.objective,
                        "acceptance_criteria": list(step.acceptance_criteria),
                        "depends_on": list(step.depends_on),
                    }
                    for step in plan.steps
                ],
            },
            "tasks": [_task_view(record) for record in records],
        }

    @app.post("/v1/builds", status_code=status.HTTP_202_ACCEPTED)
    def create_and_run_build(
        payload: PlanCreateInput,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "tasks:create")
        require_scope(principal, "tasks:execute")
        if principal.actor_type.value != "human":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Only an authenticated human may start a complete BCS build.",
            )
        criteria = payload.acceptance_criteria or [
            "Satisfy the original user objective without weakening explicit constraints.",
            "Complete specialist work, QA review and bounded repairs where needed.",
            "Report unexecuted tests, blockers and unresolved limitations honestly.",
        ]
        try:
            plan, records = active_runtime.plan_and_create_tasks(
                payload.objective, criteria
            )
            job = active_job_service.enqueue(
                [record.task.task_id for record in records], principal.actor_id
            )
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="BCS could not plan or enqueue this build.",
            ) from exc
        return {
            "plan": {
                "objective": plan.objective,
                "acceptance_criteria": list(plan.acceptance_criteria),
                "execution_order": list(plan.execution_order),
                "steps": [
                    {
                        "step_id": step.step_id,
                        "role": step.role,
                        "objective": step.objective,
                        "acceptance_criteria": list(step.acceptance_criteria),
                        "depends_on": list(step.depends_on),
                    }
                    for step in plan.steps
                ],
            },
            "task_ids": [record.task.task_id for record in records],
            "job": _json_value(job),
            "status_url": f"/v1/execution-jobs/{job['job_id']}",
        }

    @app.post("/v1/tasks/dispatch", status_code=status.HTTP_202_ACCEPTED)
    def dispatch_tasks(
        payload: TaskDispatchInput,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "tasks:execute")
        if principal.actor_type.value != "human":
            raise HTTPException(status_code=403, detail="Only an authenticated human may enqueue task execution.")
        if len(set(payload.task_ids)) != len(payload.task_ids):
            raise HTTPException(status_code=422, detail="A dispatch request may contain each task only once.")
        try:
            records = []
            for task_id in payload.task_ids:
                record = active_runtime.orchestrator.get_task(task_id)
                if record is None:
                    raise KeyError(task_id)
                records.append(record)
            selected = set(payload.task_ids)
            if any(set(record.task.dependency_ids) - selected for record in records):
                raise ValueError("Dispatch request must include every task prerequisite.")
            job = active_job_service.enqueue(payload.task_ids, principal.actor_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="A submitted task does not exist.") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"job": _json_value(job), "status_url": f"/v1/execution-jobs/{job['job_id']}"}

    @app.get("/v1/execution-jobs")
    def list_execution_jobs(principal: Principal = Depends(require_principal)):
        require_scope(principal, "tasks:read")
        jobs = active_runtime.store.list_execution_jobs(requested_by=principal.actor_id)
        return {"jobs": _json_value(jobs)}

    @app.get("/v1/execution-jobs/{job_id}")
    def get_execution_job(job_id: str, principal: Principal = Depends(require_principal)):
        require_scope(principal, "tasks:read")
        job = active_runtime.store.get_execution_job(job_id)
        if job is None or job["requested_by"] != principal.actor_id:
            raise HTTPException(status_code=404, detail="Execution job not found.")
        return {"job": _json_value(job)}

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

    @app.get("/v1/integrations")
    def list_integrations(principal: Principal = Depends(require_principal)):
        require_scope(principal, "integrations:read")
        return {"integrations": _json_value(active_runtime.integration_history())}

    @app.get("/v1/integrations/{integration_id}/qa")
    def list_integration_qa(
        integration_id: str,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "integrations:read")
        if not any(item["integration_id"] == integration_id for item in active_runtime.integration_history()):
            raise HTTPException(status_code=404, detail="Integration not found.")
        return {"assessments": _json_value(active_runtime.integration_qa_history(integration_id))}

    @app.get("/v1/integration-promotions")
    def list_integration_promotions(principal: Principal = Depends(require_principal)):
        require_scope(principal, "integrations:read")
        return {"promotion_requests": _json_value(active_runtime.integration_promotion_history())}

    @app.post("/v1/integrations/{integration_id}/promotion-requests", status_code=status.HTTP_201_CREATED)
    def request_integration_promotion(
        integration_id: str,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "integrations:promote")
        if principal.actor_type.value != "human":
            raise HTTPException(status_code=403, detail="Only an authenticated human may request promotion.")
        manager = app.state.bcs_git_manager
        if manager is None:
            raise HTTPException(status_code=503, detail="Git promotion is not configured on this API instance.")
        try:
            approval = active_runtime.request_integration_promotion(
                manager=manager, integration_id=integration_id
            )
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Integration not found.") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"approval": _approval_view(approval)}

    @app.post("/v1/integration-promotions/{request_id}/execute")
    def execute_integration_promotion(
        request_id: str,
        principal: Principal = Depends(require_principal),
    ):
        require_scope(principal, "integrations:promote")
        if principal.actor_type.value != "human":
            raise HTTPException(status_code=403, detail="Only an authenticated human may execute promotion.")
        manager = app.state.bcs_git_manager
        if manager is None:
            raise HTTPException(status_code=503, detail="Git promotion is not configured on this API instance.")
        try:
            record = active_runtime.promote_integration(manager=manager, request_id=request_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="Promotion request not found.") from exc
        except (ValueError, RuntimeError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"promotion": _json_value(record)}

    return app


__all__ = ["create_app"]
