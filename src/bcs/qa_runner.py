"""Run QA command checks through the sandbox and build an evidence-backed report."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Sequence
import uuid

from .qa import EvidenceStore, QAReport, QAReportBuilder
from .sandbox_executor import DockerSandboxExecutor


@dataclass(frozen=True)
class QACheckSpec:
    check_id: str
    name: str
    request_id: str
    argv: tuple[str, ...]


class QAExecutionRunner:
    """Coordinates QA commands; every command still passes through policy."""

    def __init__(self, executor: DockerSandboxExecutor, evidence_store: EvidenceStore) -> None:
        self._executor = executor
        self._evidence = evidence_store

    def run(
        self,
        *,
        task_id: str,
        checks: Sequence[QACheckSpec],
        qa_run_id: str | None = None,
        now: datetime | None = None,
    ) -> QAReport:
        if not checks:
            raise ValueError("At least one QA check is required.")
        check_ids = [check.check_id for check in checks]
        request_ids = [check.request_id for check in checks]
        if len(set(check_ids)) != len(check_ids) or len(set(request_ids)) != len(request_ids):
            raise ValueError("QA check IDs and execution request IDs must be unique.")
        builder = QAReportBuilder(
            task_id=task_id,
            qa_run_id=qa_run_id or ("qa_" + uuid.uuid4().hex),
            evidence_store=self._evidence,
            started_at=now,
        )
        for check in checks:
            if not check.check_id.strip() or not check.name.strip() or not check.request_id.strip():
                raise ValueError("QA check identity fields are required.")
            if isinstance(check.argv, (str, bytes)) or not check.argv:
                raise ValueError("QA check command must be a non-empty argument vector.")
            if any(not isinstance(part, str) or not part or "\x00" in part for part in check.argv):
                raise ValueError("QA check command arguments must be non-empty NUL-free strings.")
            result = self._executor.execute(check.request_id, check.argv, now=now)
            builder.add_execution(
                check_id=check.check_id,
                name=check.name,
                command=check.argv,
                result=result,
            )
        return builder.build()


__all__ = ["QACheckSpec", "QAExecutionRunner"]
