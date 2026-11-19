"""QA report construction and bounded, opaque execution evidence storage."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import threading
import uuid

from .sandbox_executor import ExecutionResult


CONTRACT_VERSION = "1.0.0-draft"


class QACheckStatus(str, Enum):
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    ERROR = "error"


@dataclass(frozen=True)
class EvidenceArtifact:
    evidence_ref: str
    kind: str
    content_sha256: str
    size_bytes: int
    stdout: str = ""
    stderr: str = ""


class EvidenceStore:
    """Thread-safe evidence cache backed by an optional durable repository."""

    def __init__(self, max_artifact_bytes: int = 200_000, *, repository=None) -> None:
        if isinstance(max_artifact_bytes, bool) or not isinstance(max_artifact_bytes, int) or not 1 <= max_artifact_bytes <= 2_000_000:
            raise ValueError("Evidence size limit must be between 1 byte and 2 MB.")
        self._max_bytes = max_artifact_bytes
        self._items: dict[str, EvidenceArtifact] = {}
        self._repository = repository
        if repository is not None and not callable(getattr(repository, "save_qa_evidence_artifact", None)):
            raise TypeError("Evidence repository does not implement durable QA evidence storage.")
        self._lock = threading.RLock()

    def put_execution(self, stdout: str, stderr: str) -> str:
        if not isinstance(stdout, str) or not isinstance(stderr, str):
            raise TypeError("Evidence output must be text.")
        raw = (stdout + "\n--- STDERR ---\n" + stderr).encode("utf-8")
        if len(raw) > self._max_bytes:
            raise ValueError("Evidence exceeds the configured storage limit.")
        digest = hashlib.sha256(raw).hexdigest()
        ref = "ev_" + uuid.uuid4().hex
        artifact = EvidenceArtifact(
            evidence_ref=ref, kind="execution_output",
            content_sha256=digest, size_bytes=len(raw),
            stdout=stdout, stderr=stderr,
        )
        with self._lock:
            if self._repository is not None:
                self._repository.save_qa_evidence_artifact({
                    "evidence_ref": artifact.evidence_ref,
                    "kind": artifact.kind,
                    "content_sha256": artifact.content_sha256,
                    "size_bytes": artifact.size_bytes,
                    "stdout": artifact.stdout,
                    "stderr": artifact.stderr,
                })
            self._items[ref] = artifact
        return ref

    def get(self, evidence_ref: str) -> EvidenceArtifact | None:
        with self._lock:
            cached = self._items.get(evidence_ref)
            if cached is not None:
                return cached
            if self._repository is None:
                return None
            record = self._repository.get_qa_evidence_artifact(evidence_ref)
            if record is None:
                return None
            artifact = EvidenceArtifact(
                evidence_ref=record["evidence_ref"],
                kind=record["kind"],
                content_sha256=record["content_sha256"],
                size_bytes=record["size_bytes"],
                stdout=record["stdout"],
                stderr=record["stderr"],
            )
            raw = (artifact.stdout + "\n--- STDERR ---\n" + artifact.stderr).encode("utf-8")
            if len(raw) != artifact.size_bytes or hashlib.sha256(raw).hexdigest() != artifact.content_sha256:
                raise ValueError("Persisted QA evidence failed integrity verification.")
            self._items[evidence_ref] = artifact
            return artifact


@dataclass(frozen=True)
class QACheck:
    check_id: str
    name: str
    status: QACheckStatus
    executed: bool
    command: tuple[str, ...] | None = None
    exit_code: int | None = None
    evidence_ref: str | None = None
    details: str = ""
    non_execution_basis: str | None = None


@dataclass(frozen=True)
class QAReport:
    contract_version: str
    task_id: str
    qa_run_id: str
    status: QACheckStatus
    checks: tuple[QACheck, ...]
    started_at: datetime
    finished_at: datetime


class QAReportBuilder:
    """Build an immutable QA report from observed results, not worker assertions."""

    def __init__(
        self,
        *,
        task_id: str,
        qa_run_id: str,
        evidence_store: EvidenceStore,
        started_at: datetime | None = None,
    ) -> None:
        if not task_id.strip() or not qa_run_id.strip():
            raise ValueError("Task ID and QA run ID are required.")
        self.task_id = task_id
        self.qa_run_id = qa_run_id
        self.evidence_store = evidence_store
        self.started_at = started_at or datetime.now(timezone.utc)
        if self.started_at.tzinfo is None:
            raise ValueError("QA timestamps must include a timezone.")
        self._checks: list[QACheck] = []

    def add_execution(
        self,
        *,
        check_id: str,
        name: str,
        command: tuple[str, ...] | list[str],
        result: ExecutionResult,
    ) -> QACheck:
        if not check_id.strip() or not name.strip():
            raise ValueError("Check ID and name are required.")
        if isinstance(command, (str, bytes)) or not command or any(
            not isinstance(part, str) or "\x00" in part for part in command
        ):
            raise ValueError("Executed command must be a valid argument vector.")

        status_map = {
            "completed": QACheckStatus.PASSED,
            "failed": QACheckStatus.FAILED,
            "timeout": QACheckStatus.FAILED,
            "blocked": QACheckStatus.BLOCKED,
            "error": QACheckStatus.ERROR,
        }
        try:
            status = status_map[result.status]
        except (AttributeError, KeyError) as exc:
            raise ValueError("Unknown execution result status.") from exc

        executed = result.status in {"completed", "failed", "timeout"}
        evidence_ref = None
        if executed:
            evidence_ref = self.evidence_store.put_execution(result.stdout, result.stderr)
        check = QACheck(
            check_id=check_id, name=name, status=status, executed=executed,
            command=tuple(command), exit_code=result.exit_code,
            evidence_ref=evidence_ref, details=result.reason,
        )
        self._append_unique(check)
        return check

    def add_static_check(
        self,
        *,
        check_id: str,
        name: str,
        passed: bool,
        details: str,
        basis: str,
    ) -> QACheck:
        if not check_id.strip() or not name.strip() or not basis.strip():
            raise ValueError("Static check identity and non-execution basis are required.")
        check = QACheck(
            check_id=check_id, name=name,
            status=QACheckStatus.PASSED if passed else QACheckStatus.FAILED,
            executed=False, details=details,
            non_execution_basis=basis,
        )
        self._append_unique(check)
        return check

    def _append_unique(self, check: QACheck) -> None:
        if any(existing.check_id == check.check_id for existing in self._checks):
            raise ValueError("QA check IDs must be unique within a report.")
        self._checks.append(check)

    def build(self, *, finished_at: datetime | None = None) -> QAReport:
        end = finished_at or datetime.now(timezone.utc)
        if end.tzinfo is None or end < self.started_at:
            raise ValueError("QA finish time must be timezone-aware and not precede start.")
        if not self._checks:
            status = QACheckStatus.BLOCKED
        elif any(c.status == QACheckStatus.ERROR for c in self._checks):
            status = QACheckStatus.ERROR
        elif any(c.status == QACheckStatus.FAILED for c in self._checks):
            status = QACheckStatus.FAILED
        elif any(c.status == QACheckStatus.BLOCKED for c in self._checks):
            status = QACheckStatus.BLOCKED
        else:
            status = QACheckStatus.PASSED
        return QAReport(
            contract_version=CONTRACT_VERSION,
            task_id=self.task_id,
            qa_run_id=self.qa_run_id,
            status=status,
            checks=tuple(self._checks),
            started_at=self.started_at,
            finished_at=end,
        )
