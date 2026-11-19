"""Deterministic QA-to-repair handoff creation.

This module creates bounded task records from observed QA findings. It does not
dispatch workers, grant capabilities, or execute repairs; the orchestrator must
validate and authorize those later steps.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Sequence

from .qa import QAReport, QACheckStatus


class HandoffKind(str, Enum):
    REPAIR = "repair"
    ESCALATION = "escalation"


@dataclass(frozen=True)
class RepairTask:
    repair_task_id: str
    parent_task_id: str
    source_qa_run_id: str
    source_check_id: str
    assigned_role: str
    objective: str
    acceptance_criteria: tuple[str, ...]
    evidence_refs: tuple[str, ...]
    original_command: tuple[str, ...] | None
    status: str = "queued"


@dataclass(frozen=True)
class QAEscalation:
    escalation_id: str
    parent_task_id: str
    source_qa_run_id: str
    source_check_id: str
    reason: str
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True)
class QAHandoff:
    repair_tasks: tuple[RepairTask, ...]
    escalations: tuple[QAEscalation, ...]


_ROLE_RULES = (
    (re.compile(r"\b(frontend|ui|css|browser|component|visual)\b", re.I), "frontend-developer"),
    (re.compile(r"\b(api|backend|database|sql|server|endpoint)\b", re.I), "backend-developer"),
    (re.compile(r"\b(integration|connector|webhook|provider)\b", re.I), "integration-engineer"),
    (re.compile(r"\b(dependency|package|build|compile|typecheck|lint)\b", re.I), "integration-engineer"),
)


def _role_for_check(name: str, details: str) -> str:
    text = f"{name} {details}"
    for pattern, role in _ROLE_RULES:
        if pattern.search(text):
            return role
    return "builder"


def _safe_id(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise ValueError(f"{field} must be a non-empty string of at most 128 characters.")
    return value.strip()


def create_qa_handoff(report: QAReport) -> QAHandoff:
    """Convert failed checks into repair tasks and non-repairable states into escalations.

    Passed checks produce no work. Failed checks produce repair tasks. Blocked and
    error checks are escalated because they may require approval, infrastructure,
    or human diagnosis rather than code changes.
    """
    if not isinstance(report, QAReport):
        raise TypeError("A validated QAReport is required.")
    task_id = _safe_id(report.task_id, "task_id")
    run_id = _safe_id(report.qa_run_id, "qa_run_id")
    repair_tasks: list[RepairTask] = []
    escalations: list[QAEscalation] = []

    for check in report.checks:
        check_id = _safe_id(check.check_id, "check_id")
        evidence = (check.evidence_ref,) if check.evidence_ref else ()
        if check.status == QACheckStatus.FAILED:
            reason = check.details.strip() or "QA check failed; inspect the recorded evidence."
            command_text = " ".join(check.command or ())
            objective = (
                f"Diagnose and repair the failure in QA check '{check.name}'. "
                f"Observed result: {reason}"
            )
            if command_text:
                objective += f" Original check command: {command_text}."
            repair_tasks.append(RepairTask(
                repair_task_id=f"repair:{run_id}:{check_id}",
                parent_task_id=task_id,
                source_qa_run_id=run_id,
                source_check_id=check_id,
                assigned_role=_role_for_check(check.name, reason),
                objective=objective,
                acceptance_criteria=(
                    "Identify the root cause and make the smallest safe correction.",
                    "Add or update automated coverage for the reported failure.",
                    "Return changed workspace-relative file paths and a concise repair summary.",
                    "Request a fresh QA run; do not claim the original failed check passed.",
                ),
                evidence_refs=evidence,
                original_command=check.command,
            ))
        elif check.status in {QACheckStatus.BLOCKED, QACheckStatus.ERROR}:
            reason = check.details.strip() or (
                "Approval or additional input is required." if check.status == QACheckStatus.BLOCKED
                else "QA execution encountered an error requiring diagnosis."
            )
            escalations.append(QAEscalation(
                escalation_id=f"escalate:{run_id}:{check_id}",
                parent_task_id=task_id,
                source_qa_run_id=run_id,
                source_check_id=check_id,
                reason=reason,
                evidence_refs=evidence,
            ))

    return QAHandoff(tuple(repair_tasks), tuple(escalations))


__all__ = [
    "HandoffKind", "RepairTask", "QAEscalation", "QAHandoff", "create_qa_handoff",
]
