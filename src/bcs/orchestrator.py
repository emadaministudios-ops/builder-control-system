"""Bounded specialist worker coordinator with optional durable storage.

Worker handlers remain trusted, runtime-only callables. The optional repository
persists task lifecycle and QA escalations, but does not grant capabilities or
provide remote worker authentication.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import threading
from typing import Callable

from .qa import QAReport
from .qa_handoff import QAEscalation, create_qa_handoff


class TaskState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"


class WorkerResultStatus(str, Enum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class WorkerResult:
    status: WorkerResultStatus
    summary: str
    changed_files: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerTask:
    task_id: str
    parent_task_id: str
    role: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    attempt: int = 1


@dataclass(frozen=True)
class TaskRecord:
    task: WorkerTask
    state: TaskState
    assigned_worker_id: str | None = None
    result: WorkerResult | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


@dataclass(frozen=True)
class DispatchOutcome:
    task_id: str
    state: TaskState
    worker_id: str | None
    result: WorkerResult | None
    reason: str = ""


@dataclass(frozen=True)
class OrchestrationSnapshot:
    tasks: tuple[TaskRecord, ...]
    escalations: tuple[QAEscalation, ...]


class Orchestrator:
    def __init__(self, *, max_attempts: int = 2, store=None):
        if not isinstance(max_attempts, int) or not 1 <= max_attempts <= 5:
            raise ValueError("max_attempts must be between 1 and 5")
        if store is not None and not all(
            callable(getattr(store, name, None))
            for name in ("list_tasks", "list_escalations", "save_state")
        ):
            raise TypeError("store must implement the orchestration repository contract")
        self._max_attempts = max_attempts
        self._store = store
        self._workers: dict[str, tuple[str, Callable]] = {}
        self._tasks: dict[str, TaskRecord] = {}
        self._escalations: list[QAEscalation] = []
        self._lock = threading.RLock()

        if self._store is not None:
            self._tasks = {record.task.task_id: record for record in self._store.list_tasks()}
            self._escalations = list(self._store.list_escalations())
            # A process may have died after marking work RUNNING. Its outcome is
            # unknown, so fail closed and require inspection before any retry.
            recovered = False
            for task_id, record in tuple(self._tasks.items()):
                if record.state == TaskState.RUNNING:
                    now = datetime.now(timezone.utc)
                    result = WorkerResult(
                        WorkerResultStatus.ERROR,
                        "Worker outcome is unknown after restart; inspect workspace before retrying.",
                    )
                    self._tasks[task_id] = TaskRecord(
                        record.task, TaskState.FAILED, record.assigned_worker_id,
                        result, record.created_at, now,
                    )
                    recovered = True
            if recovered:
                self._persist_state()

    def _persist_state(self, tasks=None, escalations=None) -> None:
        if self._store is not None:
            task_values = tuple(self._tasks.values()) if tasks is None else tuple(tasks)
            escalation_values = (
                tuple(self._escalations) if escalations is None else tuple(escalations)
            )
            self._store.save_state(task_values, escalation_values)

    def register_worker(self, *, worker_id: str, role: str, handler: Callable):
        if not isinstance(worker_id, str) or not worker_id.strip():
            raise ValueError("Worker ID is required")
        if not isinstance(role, str) or not role.strip() or not callable(handler):
            raise ValueError("Worker role and callable handler are required")
        with self._lock:
            if worker_id in self._workers:
                raise ValueError("Worker already registered")
            self._workers[worker_id] = (role, handler)

    def ingest_qa_report(self, report: QAReport) -> tuple[str, ...]:
        handoff = create_qa_handoff(report)
        now = datetime.now(timezone.utc)
        created = []
        with self._lock:
            proposed_tasks = dict(self._tasks)
            for item in handoff.repair_tasks:
                if item.repair_task_id in proposed_tasks:
                    created.append(item.repair_task_id)
                    continue
                task = WorkerTask(
                    item.repair_task_id, item.parent_task_id, item.assigned_role,
                    item.objective, item.acceptance_criteria, item.evidence_refs,
                )
                proposed_tasks[task.task_id] = TaskRecord(
                    task, TaskState.QUEUED, created_at=now, updated_at=now
                )
                created.append(task.task_id)
            known = {item.escalation_id for item in self._escalations}
            proposed_escalations = list(self._escalations)
            proposed_escalations.extend(
                item for item in handoff.escalations if item.escalation_id not in known
            )
            self._persist_state(proposed_tasks.values(), proposed_escalations)
            self._tasks = proposed_tasks
            self._escalations = proposed_escalations
        return tuple(created)

    def dispatch(self, task_id: str) -> DispatchOutcome:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError("Task does not exist")
            if record.state != TaskState.QUEUED:
                return DispatchOutcome(
                    task_id, record.state, record.assigned_worker_id, record.result,
                    "Only queued tasks can be dispatched",
                )
            candidates = [
                (worker_id, handler)
                for worker_id, (role, handler) in self._workers.items()
                if role == record.task.role
            ]
            if not candidates and record.task.role != "builder":
                candidates = [
                    (worker_id, handler)
                    for worker_id, (role, handler) in self._workers.items()
                    if role == "builder"
                ]
            if not candidates:
                return DispatchOutcome(
                    task_id, TaskState.QUEUED, None, None,
                    "No registered worker is available for this role",
                )
            worker_id, handler = candidates[0]
            running = TaskRecord(
                record.task, TaskState.RUNNING, worker_id,
                created_at=record.created_at, updated_at=datetime.now(timezone.utc),
            )
            proposed = dict(self._tasks)
            proposed[task_id] = running
            self._persist_state(proposed.values())
            self._tasks = proposed

        try:
            result = handler(record.task)
            if not isinstance(result, WorkerResult) or not isinstance(
                result.status, WorkerResultStatus
            ):
                raise TypeError("Worker returned an invalid result contract")
            if not isinstance(result.summary, str) or not result.summary.strip():
                raise ValueError("Worker result summary is required")
            for path in result.changed_files:
                if not isinstance(path, str):
                    raise ValueError("Worker changed-file paths must be strings")
                normalized = path.replace("\\\\", "/")
                if (
                    not path.strip() or normalized.startswith("/")
                    or any(part in ("..", "") for part in normalized.split("/"))
                    or (len(normalized) > 1 and normalized[1] == ":")
                ):
                    raise ValueError("Worker changed-file paths must be workspace-relative")
            if any(not isinstance(ref, str) or not ref.strip() for ref in result.evidence_refs):
                raise ValueError("Invalid evidence reference")
            if any(not isinstance(blocker, str) or not blocker.strip() for blocker in result.blockers):
                raise ValueError("Invalid blocker")
        except Exception as exc:
            result = WorkerResult(
                WorkerResultStatus.ERROR,
                f"Worker execution failed: {type(exc).__name__}",
            )

        state = {
            WorkerResultStatus.COMPLETED: TaskState.COMPLETED,
            WorkerResultStatus.BLOCKED: TaskState.BLOCKED,
            WorkerResultStatus.ERROR: TaskState.FAILED,
        }[result.status]
        with self._lock:
            updated = TaskRecord(
                record.task, state, worker_id, result,
                record.created_at, datetime.now(timezone.utc),
            )
            proposed = dict(self._tasks)
            proposed[task_id] = updated
            self._persist_state(proposed.values())
            self._tasks = proposed
        return DispatchOutcome(task_id, state, worker_id, result)

    def retry(self, task_id: str) -> TaskRecord:
        with self._lock:
            record = self._tasks.get(task_id)
            if record is None:
                raise KeyError("Task does not exist")
            if record.state != TaskState.FAILED:
                raise ValueError("Only failed tasks can be retried")
            if record.task.attempt >= self._max_attempts:
                raise ValueError("Retry budget exhausted")
            old = record.task
            task = WorkerTask(
                old.task_id, old.parent_task_id, old.role, old.objective,
                old.acceptance_criteria, old.evidence_refs, old.attempt + 1,
            )
            updated = TaskRecord(
                task, TaskState.QUEUED, created_at=record.created_at,
                updated_at=datetime.now(timezone.utc),
            )
            proposed = dict(self._tasks)
            proposed[task_id] = updated
            self._persist_state(proposed.values())
            self._tasks = proposed
            return updated

    def get_task(self, task_id: str):
        with self._lock:
            return self._tasks.get(task_id)

    def snapshot(self) -> OrchestrationSnapshot:
        with self._lock:
            return OrchestrationSnapshot(
                tuple(self._tasks.values()), tuple(self._escalations)
            )


__all__ = [
    "TaskState", "WorkerResultStatus", "WorkerResult", "WorkerTask",
    "TaskRecord", "DispatchOutcome", "OrchestrationSnapshot", "Orchestrator",
]
