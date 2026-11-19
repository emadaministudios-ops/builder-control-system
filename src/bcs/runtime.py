"""Application composition root for the BCS core services.

This module wires durable storage, orchestration, and human approvals into one
lifecycle. It does not start an HTTP server or connect model providers yet.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import timedelta
from uuid import uuid4
import os
from pathlib import Path

from .approvals import ApprovalService
from .feedback import FeedbackController
from .qa import EvidenceStore
from .qa_supervisor import QAReview, QASupervisor, build_qa_report
from .agents import ModelWorker
from .policy import OperatingMode, PolicyContext
from .workspace import Workspace
from .workspace_gateway import WorkspaceToolGateway
from .scheduler import TaskScheduler
from .planner import TaskPlanner
from .orchestrator import Orchestrator, WorkerTask
from .providers import ProviderConfig, ProviderRegistry, provider_configs_from_environment
from .storage import SQLiteOrchestrationStore


@dataclass
class BCSRuntime:
    """Owned application services and their shared persistence connection."""

    store: SQLiteOrchestrationStore
    orchestrator: Orchestrator
    approvals: ApprovalService
    feedback: FeedbackController
    providers: ProviderRegistry
    scheduler: TaskScheduler
    evidence_store: EvidenceStore
    planner: TaskPlanner

    def plan_and_create_tasks(
        self,
        objective: str,
        acceptance_criteria: tuple[str, ...] | list[str] = (),
        *,
        provider_id: str | None = None,
    ):
        """Ask BRAIN for a dependency-aware plan and persist its specialist tasks."""
        planner = self.planner if provider_id is None else TaskPlanner(
            providers=self.providers, provider_id=provider_id
        )
        plan = planner.plan(objective, acceptance_criteria)
        plan_id = "plan_" + uuid4().hex
        task_ids = {step.step_id: f"{plan_id}_{step.step_id}" for step in plan.steps}
        records = {}
        by_step = {step.step_id: step for step in plan.steps}
        for step_id in plan.execution_order:
            step = by_step[step_id]
            records[step_id] = self.orchestrator.create_task(
                task_id=task_ids[step_id],
                objective=step.objective,
                acceptance_criteria=step.acceptance_criteria,
                role=step.role,
                dependency_ids=tuple(task_ids[item] for item in step.depends_on),
            )
        return plan, tuple(records[item] for item in plan.execution_order)

    def register_model_worker(
        self,
        *,
        worker_id: str,
        role: str,
        workspace_factory,
        mode: OperatingMode = OperatingMode.AUTONOMOUS,
        capabilities: frozenset[str] = frozenset({"workspace.read", "workspace.edit"}),
        provider_id: str | None = None,
    ) -> ModelWorker:
        """Register a model-backed specialist with a trusted workspace factory.

        The factory is application-owned and must return the isolated workspace
        assigned to the task. The worker never chooses its own workspace or grants.
        """
        if not callable(workspace_factory):
            raise TypeError("A trusted workspace factory is required.")
        if not isinstance(mode, OperatingMode):
            raise TypeError("Operating mode must be an OperatingMode value.")
        if not capabilities or any(not isinstance(item, str) for item in capabilities):
            raise ValueError("At least one explicit worker capability is required.")

        def gateway_factory(task: WorkerTask) -> WorkspaceToolGateway:
            workspace = workspace_factory(task)
            if not isinstance(workspace, Workspace):
                raise TypeError("Workspace factory must return a trusted Workspace.")
            context = PolicyContext(
                mode=mode,
                task_id=task.task_id,
                worker_id=worker_id,
                workspace_id=workspace.workspace_id,
                expected_workspace_id=workspace.workspace_id,
                task_active=True,
                worker_active=True,
                task_capabilities=frozenset(capabilities),
                worker_capabilities=frozenset(capabilities),
            )
            return WorkspaceToolGateway(workspace, context, self.approvals)

        worker = ModelWorker(
            worker_id=worker_id,
            role=role,
            providers=self.providers,
            gateway_factory=gateway_factory,
            provider_id=provider_id,
        )
        self.orchestrator.register_worker(
            worker_id=worker_id, role=role, handler=worker
        )
        return worker

    def apply_qa_review(self, review: QAReview):
        """Persist QA feedback and enqueue deterministic repair handoffs."""
        report = build_qa_report(review, self.evidence_store)
        feedback_snapshot = self.feedback.record_qa_report(report)
        repair_task_ids = self.orchestrator.ingest_qa_report(report)
        return report, feedback_snapshot, repair_task_ids

    def create_qa_supervisor(
        self,
        *,
        workspace_factory,
        worker_id: str = "qa-supervisor",
        mode: OperatingMode = OperatingMode.AUTONOMOUS,
        provider_id: str | None = None,
    ) -> QASupervisor:
        """Create an independent read-only QA reviewer for trusted task workspaces."""
        if not callable(workspace_factory):
            raise TypeError("A trusted QA workspace factory is required.")

        def gateway_factory(task: WorkerTask) -> WorkspaceToolGateway:
            workspace = workspace_factory(task)
            if not isinstance(workspace, Workspace):
                raise TypeError("Workspace factory must return a trusted Workspace.")
            grants = frozenset({"workspace.read"})
            context = PolicyContext(
                mode=mode,
                task_id=task.task_id,
                worker_id=worker_id,
                workspace_id=workspace.workspace_id,
                expected_workspace_id=workspace.workspace_id,
                task_active=True,
                worker_active=True,
                task_capabilities=grants,
                worker_capabilities=grants,
            )
            return WorkspaceToolGateway(workspace, context, self.approvals)

        return QASupervisor(
            providers=self.providers,
            gateway_factory=gateway_factory,
            provider_id=provider_id,
        )

    def review_and_handoff(self, task: WorkerTask, result, supervisor: QASupervisor):
        """Run three-pass QA, persist its report, and enqueue any repair tasks."""
        review = supervisor.review(task, result)
        if review.status.value == "blocked":
            return review, None, None, ()
        report, feedback_snapshot, repair_task_ids = self.apply_qa_review(review)
        return review, report, feedback_snapshot, repair_task_ids

    def close(self) -> None:
        self.store.close()

    def __enter__(self) -> "BCSRuntime":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()


def create_runtime(
    database_path: str | Path | None = None,
    *,
    approval_ttl_seconds: int = 900,
    max_attempts: int = 2,
    max_concurrency: int = 2,
    provider_configs: tuple[ProviderConfig, ...] | None = None,
    default_provider_id: str | None = None,
) -> BCSRuntime:
    """Create a configured runtime, sharing one durable store across services.

    BCS_DATABASE_PATH may override the default ./data/bcs.sqlite3 location.
    Explicit arguments take precedence over environment configuration.
    """
    if (
        isinstance(approval_ttl_seconds, bool)
        or not isinstance(approval_ttl_seconds, int)
        or approval_ttl_seconds <= 0
    ):
        raise ValueError("approval_ttl_seconds must be a positive integer.")

    configured_path = database_path or os.environ.get("BCS_DATABASE_PATH")
    resolved_path = Path(configured_path or "./data/bcs.sqlite3")
    store = SQLiteOrchestrationStore(resolved_path)
    try:
        configs = (
            provider_configs_from_environment()
            if provider_configs is None else tuple(provider_configs)
        )
        providers = ProviderRegistry(
            default_provider_id=(
                default_provider_id or os.environ.get("BCS_DEFAULT_PROVIDER")
            )
        )
        for config in configs:
            providers.register(
                config,
                make_default=(config.provider_id == default_provider_id),
            )
        if providers.default_provider_id is not None and (
            providers.default_provider_id not in providers.provider_ids()
        ):
            raise ValueError("Configured default model provider is not registered.")
        orchestrator = Orchestrator(max_attempts=max_attempts, store=store)
        approvals = ApprovalService(
            ttl=timedelta(seconds=approval_ttl_seconds), store=store
        )
        feedback = FeedbackController(store=store)
        scheduler = TaskScheduler(orchestrator, max_concurrency=max_concurrency)
        evidence_store = EvidenceStore()
        planner = TaskPlanner(providers=providers, provider_id=default_provider_id)
        return BCSRuntime(
            store=store,
            orchestrator=orchestrator,
            approvals=approvals,
            feedback=feedback,
            providers=providers,
            scheduler=scheduler,
            evidence_store=evidence_store,
            planner=planner,
        )
    except Exception:
        store.close()
        raise


__all__ = ["BCSRuntime", "create_runtime"]
