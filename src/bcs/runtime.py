"""Application composition root for the BCS core services.

This module wires durable storage, orchestration, and human approvals into one
lifecycle. It does not start an HTTP server or connect model providers yet.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone, timedelta
from uuid import uuid4
import os
from pathlib import Path

from .approvals import ApprovalService
from .feedback import FeedbackController, FeedbackState, FeedbackSnapshot
from .qa import EvidenceStore, QAReport, QACheckStatus
from .qa_runner import QAExecutionRunner, QACheckSpec
from .sandbox_executor import DockerSandboxExecutor
from .sandbox import SandboxLimits
from .qa_supervisor import QAReview, QASupervisor, build_qa_report
from .agents import ModelWorker
from .consultation import (SpecialistConsultant, SpecialistAdvice, ConsultationRouter, ConsultationRoute, ConsultationRecord)
from .consultation_feedback import ConsultationOutcome
from .policy import OperatingMode, PolicyContext
from .workspace import Workspace
from .workspace_leases import WorkspaceLeaseManager
from .workspace_gateway import WorkspaceToolGateway
from .scheduler import TaskScheduler
from .planner import TaskPlanner
from .orchestrator import Orchestrator, WorkerTask, WorkerResult, WorkerResultStatus, DispatchOutcome, TaskState
from .git_integration import GitIntegrationCoordinator
from .providers import ProviderConfig, ProviderRegistry, provider_configs_from_environment
from .storage import SQLiteOrchestrationStore


@dataclass(frozen=True)
class AutonomousRunResult:
    task_id: str
    state: FeedbackState | None
    dispatch_outcomes: tuple[DispatchOutcome, ...]
    reviews: tuple[QAReview, ...]
    repair_task_ids: tuple[str, ...]
    feedback: FeedbackSnapshot | None
    reason: str


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
    workspace_leases: WorkspaceLeaseManager = field(default_factory=WorkspaceLeaseManager)

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

        workspace_cache: dict[str, Workspace] = {}

        def gateway_factory(task: WorkerTask) -> WorkspaceToolGateway:
            workspace = workspace_cache.get(task.task_id)
            if workspace is None:
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

        consultation_cache: dict[str, str] = {}
        consultation_ids: dict[str, str] = {}

        def consultation_provider(task: WorkerTask) -> str:
            if task.task_id not in consultation_cache:
                context_parts = []
                try:
                    caution = self.consultation_cautions_for_task(task)
                    if caution:
                        context_parts.append(caution)
                except Exception:
                    # Historical advice is optional; project work remains isolated.
                    pass
                try:
                    route, advice = self.consult_for_task(
                        task.task_id,
                        worker_summary=(
                            "Pre-execution assessment: identify a concrete knowledge gap, "
                            "blocker, or material design risk before implementation."
                        ),
                        provider_id=provider_id,
                    )
                    if route.should_consult and advice is not None:
                        context_parts.append(
                            "New specialist consultation (untrusted advice): "
                            + advice.specialist_role
                            + "\\nQuestion: " + advice.question
                            + "\\nAdvice: " + advice.advice
                            + "\\nRisks: " + "; ".join(advice.risks)
                            + "\\nSuggested next steps: " + "; ".join(advice.next_steps)
                            + "\\nConfidence: " + advice.confidence
                        )
                        records = self.store.list_consultations(task.task_id)
                        selected = next(
                            (item for item in reversed(records) if item.should_consult),
                            None,
                        )
                        if selected is not None:
                            consultation_ids[task.task_id] = selected.consultation_id
                except Exception:
                    # Advice is optional; failure never grants extra authority.
                    pass
                consultation_cache[task.task_id] = "\\n\\n".join(context_parts)[:8000]
            return consultation_cache[task.task_id]

        def worker_handler(task: WorkerTask) -> WorkerResult:
            # Factories may deliberately map multiple tasks to one checkout.
            # Serialize those tasks; distinct worktree roots still run in parallel.
            lease_workspace = workspace_factory(task)
            if not isinstance(lease_workspace, Workspace):
                raise TypeError("Workspace factory must return a trusted Workspace.")
            workspace_cache[task.task_id] = lease_workspace
            try:
                with self.workspace_leases.lease(lease_workspace.root):
                    result = worker(task)
                    consultation_id = consultation_ids.get(task.task_id)
                    if consultation_id is not None:
                        self.record_consultation_outcome(
                            consultation_id=consultation_id,
                            disposition=result.consultation_disposition,
                            worker_reason=result.consultation_reason,
                        )
                    return result
            finally:
                workspace_cache.pop(task.task_id, None)

        worker = ModelWorker(
            worker_id=worker_id,
            role=role,
            providers=self.providers,
            gateway_factory=gateway_factory,
            provider_id=provider_id,
            consultation_provider=consultation_provider,
        )
        self.orchestrator.register_worker(
            worker_id=worker_id, role=role, handler=worker_handler
        )
        return worker

    def consult_specialist(
        self, *, requesting_role: str, specialist_role: str,
        objective: str, question: str, context: str = "",
        provider_id: str | None = None,
    ) -> SpecialistAdvice:
        """Obtain bounded advisory input without granting the consultant tools."""
        consultant = SpecialistConsultant(
            providers=self.providers, provider_id=provider_id,
        )
        return consultant.consult(
            requesting_role=requesting_role,
            specialist_role=specialist_role,
            objective=objective,
            question=question,
            context=context,
        )

    def consultation_learning_context(self, task: WorkerTask, *, limit: int = 5) -> str:
        """Build bounded, project-scoped prior consultation evidence for BRAIN."""
        if not isinstance(task, WorkerTask):
            raise TypeError("A validated WorkerTask is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 8:
            raise ValueError("Consultation history limit must be between 1 and 8.")
        task_ids = [task.task_id]
        group_id = task.workspace_group_id
        parent_id = task.parent_task_id if group_id else None
        visited = {task.task_id}
        while parent_id and parent_id not in visited and len(task_ids) < 12:
            visited.add(parent_id)
            parent = self.orchestrator.get_task(parent_id)
            if parent is None or parent.task.workspace_group_id != group_id:
                break
            task_ids.append(parent_id)
            parent_id = parent.task.parent_task_id
        consultations = []
        outcomes = []
        for item_id in task_ids:
            consultations.extend(self.store.list_consultations(item_id))
            outcomes.extend(self.store.list_consultation_outcomes(item_id))
        by_id = {item.consultation_id: item for item in consultations}
        latest = {}
        for outcome in outcomes:
            previous = latest.get(outcome.consultation_id)
            if previous is None or outcome.created_at > previous.created_at:
                latest[outcome.consultation_id] = outcome
        rows = []
        for consultation in consultations:
            if not consultation.should_consult:
                rows.append((
                    consultation.created_at,
                    "Consultation=skipped; requesting_role=" + consultation.requesting_role
                    + "; reason=" + consultation.reason[:500]
                ))
                continue
            outcome = latest.get(consultation.consultation_id)
            if outcome is None:
                continue
            rows.append(
                (outcome.created_at,
                 "Specialist=" + str(consultation.specialist_role)
                 + "; adoption=" + outcome.disposition
                 + "; QA=" + outcome.qa_status
                 + "; reason=" + outcome.worker_reason[:500]
                 + "; advice=" + (consultation.advice or "")[:700]
                 + "; evidence=" + ";".join(outcome.evidence_refs[:5]))
            )
        rows.sort(key=lambda item: item[0], reverse=True)
        return "\\n".join(item[1] for item in rows[:limit])[:4000]

    def consult_for_task(
        self, task_id: str, *, worker_summary: str,
        blockers: tuple[str, ...] = (), provider_id: str | None = None,
    ) -> tuple[ConsultationRoute, SpecialistAdvice | None]:
        """Have BRAIN decide whether a task warrants one bounded specialist consult."""
        record = self.orchestrator.get_task(task_id)
        if record is None:
            raise KeyError("Task does not exist.")
        task = record.task
        router = ConsultationRouter(providers=self.providers, provider_id=provider_id)
        history_context = self.consultation_learning_context(task)
        route = router.route(
            task_role=task.role, objective=task.objective,
            acceptance_criteria=task.acceptance_criteria,
            worker_summary=worker_summary, blockers=blockers,
            collaboration_history=history_context,
        )
        if not route.should_consult:
            self.store.save_consultation(ConsultationRecord(
                consultation_id="consult_" + uuid4().hex, task_id=task_id,
                requesting_role=task.role, should_consult=False,
                specialist_role=None, question="", reason=route.reason,
                advice=None, risks=(), next_steps=(), confidence=None,
                created_at=datetime.now(timezone.utc),
            ))
            return route, None
        advice = self.consult_specialist(
            requesting_role=task.role, specialist_role=route.specialist_role,
            objective=task.objective, question=route.question,
            context=(
                "Acceptance criteria: " + "; ".join(task.acceptance_criteria)
                + "\\nWorker summary: " + worker_summary
                + ("\\nBlockers: " + "; ".join(blockers) if blockers else "")
                + "\\nBRAIN routing rationale: " + route.reason
            ),
            provider_id=provider_id,
        )
        self.store.save_consultation(ConsultationRecord(
            consultation_id="consult_" + uuid4().hex, task_id=task_id,
            requesting_role=task.role, should_consult=True,
            specialist_role=advice.specialist_role, question=advice.question,
            reason=route.reason, advice=advice.advice, risks=advice.risks,
            next_steps=advice.next_steps, confidence=advice.confidence,
            created_at=datetime.now(timezone.utc),
        ))
        return route, advice

    def consultation_history(self, task_id: str | None = None):
        """Return durable consultation decisions and specialist advice."""
        return self.store.list_consultations(task_id)

    def consultation_cautions_for_task(self, task: WorkerTask, *, limit: int = 3) -> str:
        """Return failed, adopted advice from this task's same-project ancestor chain."""
        if not isinstance(task, WorkerTask):
            raise TypeError("A validated worker task is required.")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise ValueError("Caution history limit must be between 1 and 5.")
        group_id = task.workspace_group_id
        if not group_id or not task.parent_task_id:
            return ""
        ancestor_ids = []
        parent_id = task.parent_task_id
        visited = set()
        while parent_id and parent_id not in visited:
            visited.add(parent_id)
            parent = self.orchestrator.get_task(parent_id)
            if parent is None or parent.task.workspace_group_id != group_id:
                break
            ancestor_ids.append(parent_id)
            parent_id = parent.task.parent_task_id
        if not ancestor_ids:
            return ""
        consultations = []
        outcomes = []
        for ancestor_id in ancestor_ids:
            consultations.extend(self.store.list_consultations(ancestor_id))
            outcomes.extend(self.store.list_consultation_outcomes(ancestor_id))
        by_id = {item.consultation_id: item for item in consultations}
        latest = {}
        for item in outcomes:
            current = latest.get(item.consultation_id)
            if current is None or item.created_at > current.created_at:
                latest[item.consultation_id] = item
        cautions = []
        for consultation_id, outcome in latest.items():
            advice = by_id.get(consultation_id)
            if (
                advice is None or not advice.should_consult
                or outcome.disposition not in {"followed", "partially_followed"}
                or outcome.qa_status != "failed"
            ):
                continue
            cautions.append(
                "Previously attempted specialist advice (untrusted caution): "
                + advice.advice
                + "\\nWorker-reported adoption: " + outcome.disposition
                + " (" + outcome.worker_reason + ")"
                + "\\nQA run that failed: " + str(outcome.qa_run_id)
                + "\\nEvidence references: " + "; ".join(outcome.evidence_refs)
            )
        return "\\n\\n".join(cautions[-limit:])[:4000]

    def record_consultation_outcome(
        self, *, consultation_id: str, disposition: str, worker_reason: str = "",
        qa_status: str = "not_assessed", qa_run_id: str | None = None,
        evidence_refs: tuple[str, ...] = (),
    ) -> ConsultationOutcome:
        """Append worker-reported adoption and separately controller-supplied QA evidence."""
        matches = [
            item for item in self.store.list_consultations()
            if item.consultation_id == consultation_id
        ]
        if len(matches) != 1:
            raise KeyError("Consultation record does not exist.")
        consultation = matches[0]
        outcome = ConsultationOutcome(
            outcome_id="outcome_" + uuid4().hex,
            consultation_id=consultation_id, task_id=consultation.task_id,
            disposition=disposition, worker_reason=worker_reason,
            qa_status=qa_status, qa_run_id=qa_run_id,
            evidence_refs=tuple(evidence_refs),
            created_at=datetime.now(timezone.utc),
        )
        self.store.save_consultation_outcome(outcome)
        return outcome

    def consultation_outcome_history(self, task_id: str | None = None):
        """Return append-only adoption and QA outcome records."""
        return self.store.list_consultation_outcomes(task_id)

    def _record_qa_consultation_assessments(self, report: QAReport) -> None:
        """Attach controller-generated QA status to consultation adoption records."""
        if not isinstance(report, QAReport):
            raise TypeError("A validated QAReport is required.")
        if report.status == QACheckStatus.FAILED:
            qa_status = "failed"
        elif report.status == QACheckStatus.BLOCKED:
            qa_status = "blocked"
        elif report.status == QACheckStatus.ERROR:
            qa_status = "error"
        elif any(check.executed for check in report.checks):
            qa_status = "passed"
        else:
            # Static-only review cannot establish that implementation tests passed.
            qa_status = "not_assessed"
        evidence_refs = tuple(
            check.evidence_ref for check in report.checks if check.evidence_ref
        )
        consultations = self.store.list_consultations(report.task_id)
        outcomes = self.store.list_consultation_outcomes(report.task_id)
        latest = {}
        for outcome in outcomes:
            current = latest.get(outcome.consultation_id)
            if current is None or outcome.created_at > current.created_at:
                latest[outcome.consultation_id] = outcome
        for consultation in consultations:
            if not consultation.should_consult:
                continue
            previous = latest.get(consultation.consultation_id)
            if previous is None:
                continue
            if previous.qa_run_id == report.qa_run_id and previous.qa_status == qa_status:
                continue
            self.record_consultation_outcome(
                consultation_id=consultation.consultation_id,
                disposition=previous.disposition,
                worker_reason=previous.worker_reason,
                qa_status=qa_status,
                qa_run_id=report.qa_run_id if qa_status != "not_assessed" else None,
                evidence_refs=evidence_refs if qa_status != "not_assessed" else (),
            )

    def apply_qa_review(self, review: QAReview):
        """Persist QA feedback and enqueue deterministic repair handoffs."""
        report = build_qa_report(review, self.evidence_store)
        self._record_qa_consultation_assessments(report)
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

    def create_qa_execution_runner(
        self,
        task: WorkerTask,
        *,
        workspace_factory,
        docker_executable: str,
        image: str,
        allowed_images: frozenset[str],
        mode: OperatingMode = OperatingMode.AUTONOMOUS,
        worker_id: str = "qa-runner",
        limits: SandboxLimits = SandboxLimits(),
    ) -> QAExecutionRunner:
        """Create a sandboxed executable QA runner for a trusted task workspace."""
        if not isinstance(task, WorkerTask):
            raise TypeError("A trusted WorkerTask is required.")
        if not callable(workspace_factory):
            raise TypeError("A trusted QA workspace factory is required.")
        if not isinstance(mode, OperatingMode):
            raise TypeError("Operating mode must be an OperatingMode value.")
        workspace = workspace_factory(task)
        if not isinstance(workspace, Workspace):
            raise TypeError("Workspace factory must return a trusted Workspace.")
        grants = frozenset({"tests.run"})
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
        executor = DockerSandboxExecutor(
            workspace=workspace,
            context=context,
            approvals=self.approvals,
            docker_executable=docker_executable,
            image=image,
            allowed_images=allowed_images,
            limits=limits,
        )
        return QAExecutionRunner(executor, self.evidence_store)

    def run_executable_qa(
        self,
        task: WorkerTask,
        checks,
        *,
        workspace_factory,
        docker_executable: str,
        image: str,
        allowed_images: frozenset[str],
        mode: OperatingMode = OperatingMode.AUTONOMOUS,
        worker_id: str = "qa-runner",
        limits: SandboxLimits = SandboxLimits(),
    ) -> QAReport:
        """Execute controller-defined QA commands and retain their evidence."""
        runner = self.create_qa_execution_runner(
            task,
            workspace_factory=workspace_factory,
            docker_executable=docker_executable,
            image=image,
            allowed_images=allowed_images,
            mode=mode,
            worker_id=worker_id,
            limits=limits,
        )
        report = runner.run(task_id=task.task_id, checks=checks)
        self._record_qa_consultation_assessments(report)
        return report

    def review_and_handoff(
        self, task: WorkerTask, result, supervisor: QASupervisor,
        *, execution_report: QAReport | None = None,
    ):
        """Combine independent source review and executed checks into one feedback cycle."""
        review = supervisor.review(task, result)
        if review.status.value == "blocked":
            return review, None, None, ()
        report = build_qa_report(review, self.evidence_store)
        if execution_report is not None:
            if execution_report.task_id != task.task_id:
                raise ValueError("Executable QA report belongs to a different task.")
            checks = report.checks + tuple(
                replace(check, check_id="execution:" + check.check_id)
                for check in execution_report.checks
            )
            statuses = {check.status for check in checks}
            status = (
                QACheckStatus.ERROR if QACheckStatus.ERROR in statuses else
                QACheckStatus.FAILED if QACheckStatus.FAILED in statuses else
                QACheckStatus.BLOCKED if QACheckStatus.BLOCKED in statuses else
                QACheckStatus.PASSED
            )
            report = QAReport(
                contract_version=report.contract_version,
                task_id=task.task_id,
                qa_run_id=execution_report.qa_run_id,
                status=status,
                checks=checks,
                started_at=min(report.started_at, execution_report.started_at),
                finished_at=max(report.finished_at, execution_report.finished_at),
            )
        self._record_qa_consultation_assessments(report)
        feedback_snapshot = self.feedback.record_qa_report(report)
        repair_task_ids = self.orchestrator.ingest_qa_report(report)
        return review, report, feedback_snapshot, repair_task_ids

    def execute_with_qa(
        self,
        task_id: str,
        supervisor: QASupervisor,
        *,
        max_repair_rounds: int = 3,
        executable_checks: tuple[QACheckSpec, ...] | list[QACheckSpec] | None = None,
        qa_workspace_factory=None,
        docker_executable: str | None = None,
        qa_image: str | None = None,
        allowed_qa_images: frozenset[str] | None = None,
        qa_mode: OperatingMode = OperatingMode.AUTONOMOUS,
        qa_limits: SandboxLimits = SandboxLimits(),
    ) -> AutonomousRunResult:
        """Run a task, repair QA findings, and independently review each repair.

        This method never converts model review into executed test evidence. It
        stops on blocked/failed workers, QA escalation, unavailable specialists,
        or a bounded repair-round limit.
        """
        if not isinstance(supervisor, QASupervisor) and not callable(
            getattr(supervisor, "review", None)
        ):
            raise TypeError("A trusted QA supervisor is required.")
        if (
            isinstance(max_repair_rounds, bool)
            or not isinstance(max_repair_rounds, int)
            or not 1 <= max_repair_rounds <= 5
        ):
            raise ValueError("max_repair_rounds must be between 1 and 5.")
        executable_config = (
            executable_checks is not None, qa_workspace_factory is not None,
            docker_executable is not None, qa_image is not None,
            allowed_qa_images is not None,
        )
        if any(executable_config) and not all(executable_config):
            raise ValueError(
                "Executable QA requires checks, a trusted workspace factory, "
                "Docker executable, image, and image allowlist together."
            )
        if executable_checks is not None:
            if not executable_checks or any(not isinstance(item, QACheckSpec) for item in executable_checks):
                raise ValueError("Executable QA requires controller-defined QACheckSpec entries.")
            if not isinstance(qa_mode, OperatingMode):
                raise TypeError("qa_mode must be an OperatingMode value.")
        record = self.orchestrator.get_task(task_id)
        if record is None:
            raise KeyError("Task does not exist.")
        if record.state.value != "queued":
            raise ValueError("Autonomous QA execution requires a queued task.")

        dispatches = []
        reviews = []
        repairs = []
        initial = self.orchestrator.dispatch(task_id)
        dispatches.append(initial)
        if initial.state.value != "completed" or initial.result is None:
            return AutonomousRunResult(
                task_id, None, tuple(dispatches), (), (),
                self.feedback.get_snapshot(task_id),
                initial.reason or "Initial worker did not complete successfully.",
            )

        current_result = initial.result
        for repair_round in range(max_repair_rounds + 1):
            execution_report = None
            if executable_checks is not None:
                execution_report = self.run_executable_qa(
                    record.task, executable_checks,
                    workspace_factory=qa_workspace_factory,
                    docker_executable=docker_executable,
                    image=qa_image,
                    allowed_images=allowed_qa_images,
                    mode=qa_mode,
                    limits=qa_limits,
                )
            review, report, snapshot, repair_ids = self.review_and_handoff(
                record.task, current_result, supervisor,
                execution_report=execution_report,
            )
            reviews.append(review)
            if review.status.value == "blocked" or report is None:
                return AutonomousRunResult(
                    task_id, FeedbackState.HUMAN_ATTENTION, tuple(dispatches),
                    tuple(reviews), tuple(repairs), self.feedback.get_snapshot(task_id),
                    "QA was blocked; no completion decision was made.",
                )
            repairs.extend(repair_ids)
            if snapshot.state == FeedbackState.VERIFIED:
                return AutonomousRunResult(
                    task_id, snapshot.state, tuple(dispatches), tuple(reviews),
                    tuple(repairs), snapshot,
                    "QA feedback policy completed its required independent review cycles. "
                    + ("Controller-defined sandbox tests were executed on each pass. " if executable_checks is not None else "No executable tests were configured. ")
                    + "This is not release approval.",
                )
            if snapshot.state in {
                FeedbackState.HUMAN_ATTENTION,
                FeedbackState.CYCLE_BUDGET_EXHAUSTED,
            }:
                return AutonomousRunResult(
                    task_id, snapshot.state, tuple(dispatches), tuple(reviews),
                    tuple(repairs), snapshot, snapshot.cycles[-1].instruction,
                )
            if snapshot.state == FeedbackState.VERIFY_AGAIN:
                continue
            if snapshot.state != FeedbackState.REPAIR_REQUIRED:
                return AutonomousRunResult(
                    task_id, snapshot.state, tuple(dispatches), tuple(reviews),
                    tuple(repairs), snapshot, "Feedback controller returned an unsupported state.",
                )
            if repair_round >= max_repair_rounds:
                return AutonomousRunResult(
                    task_id, snapshot.state, tuple(dispatches), tuple(reviews),
                    tuple(repairs), snapshot,
                    "Repair-round limit reached. Findings remain open and require another bounded run or human direction.",
                )
            if not repair_ids:
                return AutonomousRunResult(
                    task_id, snapshot.state, tuple(dispatches), tuple(reviews),
                    tuple(repairs), snapshot,
                    "QA requested repair but produced no dispatchable repair tasks.",
                )

            repair_results = self.scheduler.dispatch_independent(repair_ids)
            dispatches.extend(repair_results.outcomes)
            incomplete = [
                outcome for outcome in repair_results.outcomes
                if outcome.state.value != "completed" or outcome.result is None
            ]
            if incomplete:
                return AutonomousRunResult(
                    task_id, FeedbackState.HUMAN_ATTENTION, tuple(dispatches),
                    tuple(reviews), tuple(repairs), snapshot,
                    "A repair worker failed, was blocked, or had no available specialist. "
                    "The original task remains unverified.",
                )
            changed_files = list(current_result.changed_files)
            evidence_refs = list(current_result.evidence_refs)
            summaries = [current_result.summary]
            for outcome in repair_results.outcomes:
                repair_result = outcome.result
                summaries.append(repair_result.summary)
                changed_files.extend(repair_result.changed_files)
                evidence_refs.extend(repair_result.evidence_refs)
            current_result = WorkerResult(
                status=WorkerResultStatus.COMPLETED,
                summary="\n".join(summaries)[-12000:],
                changed_files=tuple(dict.fromkeys(changed_files)),
                evidence_refs=tuple(dict.fromkeys(evidence_refs)),
            )

        snapshot = self.feedback.get_snapshot(task_id)
        return AutonomousRunResult(
            task_id, None if snapshot is None else snapshot.state,
            tuple(dispatches), tuple(reviews), tuple(repairs), snapshot,
            "Autonomous QA loop stopped without a verified result.",
        )

    def create_git_integration_coordinator(self, manager, *, max_groups: int = 32) -> GitIntegrationCoordinator:
        """Create the guarded integration service for this runtime."""
        return GitIntegrationCoordinator(manager, max_groups=max_groups)

    def integrate_completed_work(
        self,
        *,
        manager,
        integration_id: str,
        workspace_group_ids: tuple[str, ...] | list[str],
    ):
        """Integrate only workspace groups whose persisted tasks are all completed.

        This creates an isolated integration branch and returns an awaiting-QA
        result. It never approves release or promotes changes to development/main.
        """
        if (
            isinstance(workspace_group_ids, (str, bytes))
            or not isinstance(workspace_group_ids, (tuple, list))
            or not workspace_group_ids
        ):
            raise ValueError("Provide one or more completed workspace group IDs.")
        if len(set(workspace_group_ids)) != len(workspace_group_ids):
            raise ValueError("Workspace group IDs must be unique.")

        records = self.orchestrator.snapshot().tasks
        for group_id in workspace_group_ids:
            group_tasks = [
                item for item in records
                if item.task.workspace_group_id == group_id
            ]
            if not group_tasks:
                raise ValueError("Workspace group has no registered orchestration tasks.")
            unfinished = [
                item.task.task_id for item in group_tasks
                if item.state is not TaskState.COMPLETED
            ]
            if unfinished:
                raise ValueError(
                    "Integration is blocked until every task in the workspace group "
                    "is completed: " + ", ".join(unfinished[:10])
                )

        coordinator = self.create_git_integration_coordinator(manager)
        result = coordinator.integrate(
            integration_id=integration_id,
            workspace_group_ids=workspace_group_ids,
        )
        self.store.save_integration_run({
            "integration_id": result.integration_id,
            "branch": result.branch,
            "path": str(result.path),
            "status": result.status,
            "included_groups": result.included_groups,
            "commits": result.commits,
            "conflicts": result.conflicts,
            "reason": result.reason,
        })
        return result

    def integration_history(self):
        """Return durable integration records, including after runtime restart."""
        return self.store.list_integration_runs()

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


__all__ = ["BCSRuntime", "AutonomousRunResult", "create_runtime"]
