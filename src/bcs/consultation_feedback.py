"""Evidence-linked outcomes for specialist consultation advice.

Worker disposition is self-reported; QA status is recorded separately by the
trusted controller and is never inferred from the worker's claim.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class ConsultationOutcome:
    outcome_id: str
    consultation_id: str
    task_id: str
    disposition: str
    worker_reason: str
    qa_status: str
    qa_run_id: str | None
    evidence_refs: tuple[str, ...]
    created_at: datetime

    def __post_init__(self):
        for label, value, maximum in (
            ("outcome_id", self.outcome_id, 128),
            ("consultation_id", self.consultation_id, 128),
            ("task_id", self.task_id, 128),
        ):
            if not isinstance(value, str) or not value.strip() or len(value) > maximum:
                raise ValueError(f"{label} is invalid.")
        if self.disposition not in {"followed", "partially_followed", "not_followed", "unknown"}:
            raise ValueError("Consultation disposition is invalid.")
        if not isinstance(self.worker_reason, str) or len(self.worker_reason) > 2000:
            raise ValueError("Worker reason must be at most 2000 characters.")
        if self.qa_status not in {"not_assessed", "passed", "failed", "blocked", "error"}:
            raise ValueError("QA status is invalid.")
        if self.qa_status == "not_assessed":
            if self.qa_run_id is not None or self.evidence_refs:
                raise ValueError("Unassessed outcomes cannot claim QA evidence.")
        elif not isinstance(self.qa_run_id, str) or not self.qa_run_id.strip() or len(self.qa_run_id) > 128:
            raise ValueError("An assessed QA outcome requires a QA run ID.")
        if not isinstance(self.evidence_refs, tuple) or len(self.evidence_refs) > 20 or any(
            not isinstance(item, str) or not item.strip() or len(item) > 256
            for item in self.evidence_refs
        ):
            raise ValueError("Evidence references must be a bounded tuple of strings.")
        if not isinstance(self.created_at, datetime) or self.created_at.tzinfo is None:
            raise ValueError("Outcome timestamp must be timezone-aware.")


__all__ = ["ConsultationOutcome"]
