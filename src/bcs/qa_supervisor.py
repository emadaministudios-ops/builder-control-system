"""Three-pass model-assisted QA supervision against the original user objective.

This reviewer identifies requirement gaps and repair guidance. It does not execute
tests and can never certify a task as passed; executable QA evidence remains a
separate responsibility of the QA runner.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Callable
import json
import uuid

from .qa import EvidenceStore, QAReport, QAReportBuilder

from .orchestrator import WorkerResult, WorkerResultStatus, WorkerTask
from .providers import ProviderError, ProviderRegistry
from .workspace_gateway import WorkspaceToolGateway


class ReviewStatus(str, Enum):
    REPAIR_REQUIRED = "repair_required"
    READY_FOR_TESTS = "ready_for_tests"
    BLOCKED = "blocked"


@dataclass(frozen=True)
class ReviewFinding:
    requirement_id: str
    severity: str
    issue: str
    expected: str
    observed: str
    suggestion: str
    file_path: str | None = None


@dataclass(frozen=True)
class QAReview:
    task_id: str
    status: ReviewStatus
    requirements: tuple[tuple[str, str], ...]
    findings: tuple[ReviewFinding, ...]
    summary: str
    recommended_specialist: str | None
    analysis_rounds: int = 3


class QASupervisor:
    """Analyze intent, critique implementation, then audit the critique."""

    _SPECIALISTS = frozenset({
        "architect", "frontend-developer", "backend-developer",
        "integration-engineer", "builder", "research-helper",
    })
    _SEVERITIES = frozenset({"critical", "high", "medium", "low"})

    def __init__(
        self,
        *,
        providers: ProviderRegistry,
        gateway_factory: Callable[[WorkerTask], WorkspaceToolGateway],
        provider_id: str | None = None,
        max_files: int = 20,
        max_file_bytes: int = 80_000,
        max_tokens: int = 4096,
    ) -> None:
        if not callable(gateway_factory):
            raise TypeError("A trusted QA workspace gateway factory is required.")
        if not 1 <= max_files <= 50 or not 1024 <= max_file_bytes <= 500_000:
            raise ValueError("QA inspection limits are outside their allowed ranges.")
        if not 1 <= max_tokens <= 32768:
            raise ValueError("QA token limit is outside its allowed range.")
        self.providers = providers
        self.gateway_factory = gateway_factory
        self.provider_id = provider_id
        self.max_files = max_files
        self.max_file_bytes = max_file_bytes
        self.max_tokens = max_tokens

    @staticmethod
    def _json_response(content: str) -> dict:
        try:
            value = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("QA model response was not valid JSON.") from exc
        if not isinstance(value, dict):
            raise ValueError("QA model response must be a JSON object.")
        return value

    def _call(self, system: str, user: str) -> dict:
        response = self.providers.complete(
            [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            provider_id=self.provider_id,
            temperature=0.1,
            max_tokens=self.max_tokens,
        )
        return self._json_response(response.content)

    def _requirements(self, value: dict) -> tuple[tuple[str, str], ...]:
        if set(value) != {"requirements"} or not isinstance(value["requirements"], list):
            raise ValueError("QA requirement analysis schema is invalid.")
        if not 1 <= len(value["requirements"]) <= 50:
            raise ValueError("QA must identify between 1 and 50 requirements.")
        items = []
        seen = set()
        for item in value["requirements"]:
            if not isinstance(item, dict) or set(item) != {"id", "criterion"}:
                raise ValueError("QA requirement item schema is invalid.")
            identifier, criterion = item["id"], item["criterion"]
            if (
                not isinstance(identifier, str) or not identifier.strip()
                or not isinstance(criterion, str) or not criterion.strip()
                or identifier in seen
            ):
                raise ValueError("QA requirement identifiers and criteria must be unique and non-empty.")
            seen.add(identifier)
            items.append((identifier, criterion))
        return tuple(items)

    def _findings(self, value: dict, requirement_ids: set[str], changed_files: set[str]):
        if set(value) != {"summary", "findings", "recommended_specialist"}:
            raise ValueError("QA critique schema is invalid.")
        if not isinstance(value["summary"], str) or not value["summary"].strip():
            raise ValueError("QA critique summary is required.")
        if not isinstance(value["findings"], list) or len(value["findings"]) > 100:
            raise ValueError("QA findings list is invalid.")
        specialist = value["recommended_specialist"]
        if specialist is not None and specialist not in self._SPECIALISTS:
            raise ValueError("QA recommended an unknown specialist role.")
        findings = []
        for item in value["findings"]:
            fields = {
                "requirement_id", "severity", "issue", "expected",
                "observed", "suggestion", "file_path",
            }
            if not isinstance(item, dict) or set(item) != fields:
                raise ValueError("QA finding schema is invalid.")
            if item["requirement_id"] not in requirement_ids:
                raise ValueError("QA finding references an unknown requirement.")
            if item["severity"] not in self._SEVERITIES:
                raise ValueError("QA finding severity is invalid.")
            for key in ("issue", "expected", "observed", "suggestion"):
                if not isinstance(item[key], str) or not item[key].strip():
                    raise ValueError("QA finding text fields must be non-empty.")
            path = item["file_path"]
            if path is not None and path not in changed_files:
                raise ValueError("QA finding references a file outside the inspected change set.")
            findings.append(ReviewFinding(
                requirement_id=item["requirement_id"],
                severity=item["severity"],
                issue=item["issue"].strip(),
                expected=item["expected"].strip(),
                observed=item["observed"].strip(),
                suggestion=item["suggestion"].strip(),
                file_path=path,
            ))
        return tuple(findings), value["summary"].strip(), specialist

    def review(self, task: WorkerTask, result: WorkerResult) -> QAReview:
        if not isinstance(task, WorkerTask) or not isinstance(result, WorkerResult):
            raise TypeError("QA review requires validated worker task and result records.")
        if result.status != WorkerResultStatus.COMPLETED:
            return QAReview(
                task.task_id, ReviewStatus.BLOCKED, (), (),
                "Worker did not complete successfully; QA cannot review a completed implementation.",
                None,
            )
        if len(result.changed_files) > self.max_files:
            return QAReview(
                task.task_id, ReviewStatus.BLOCKED, (), (),
                "The change set exceeds QA inspection limits.",
                None,
            )
        try:
            gateway = self.gateway_factory(task)
            if not isinstance(gateway, WorkspaceToolGateway):
                raise TypeError("Unsupported workspace gateway.")
            project_files = []
            for path in result.changed_files:
                read = gateway.read_file("qa-read-" + path.replace("/", "_"), path)
                if read.status != "completed" or read.content is None:
                    return QAReview(
                        task.task_id, ReviewStatus.BLOCKED, (), (),
                        "QA could not inspect every changed file through the workspace gateway.",
                        None,
                    )
                if len(read.content.encode("utf-8")) > self.max_file_bytes:
                    return QAReview(
                        task.task_id, ReviewStatus.BLOCKED, (), (),
                        "A changed file exceeds the QA inspection size limit.",
                        None,
                    )
                project_files.append({"path": path, "content": read.content})

            objective = {
                "original_objective": task.objective,
                "acceptance_criteria": list(task.acceptance_criteria),
            }
            first = self._call(
                "You are an independent QA requirements analyst. Derive observable "
                "requirements from the original user objective. Do not inspect implementation "
                "yet. Return JSON only: {\"requirements\":[{\"id\":\"R1\","
                "\"criterion\":\"observable requirement\"}]}. Preserve all explicit "
                "constraints; do not invent new scope.",
                json.dumps(objective, ensure_ascii=False),
            )
            requirements = self._requirements(first)
            review_input = {
                **objective,
                "requirements": [{"id": key, "criterion": value} for key, value in requirements],
                "worker_summary": result.summary,
                "changed_files": project_files,
            }
            second = self._call(
                "You are an independent QA supervisor. Compare implementation evidence "
                "against every requirement, using inspected files when present and the worker summary when the task is analysis-only. Do not claim implementation evidence that was not supplied. Return JSON only with exactly these keys: "
                "{\"summary\":\"...\",\"findings\":[{\"requirement_id\":\"R1\","
                "\"severity\":\"critical|high|medium|low\",\"issue\":\"...\","
                "\"expected\":\"...\",\"observed\":\"...\","
                "\"suggestion\":\"...\",\"file_path\":null}],"
                "\"recommended_specialist\":null}. Use only supplied changed file paths. "
                "Identify concrete gaps and actionable fixes. Do not claim tests passed.",
                json.dumps(review_input, ensure_ascii=False),
            )
            second_findings, second_summary, second_specialist = self._findings(
                second, {key for key, _ in requirements}, set(result.changed_files)
            )
            audit_input = {
                **review_input,
                "initial_qa_summary": second_summary,
                "initial_findings": [
                    {
                        "requirement_id": item.requirement_id,
                        "severity": item.severity,
                        "issue": item.issue,
                        "expected": item.expected,
                        "observed": item.observed,
                        "suggestion": item.suggestion,
                        "file_path": item.file_path,
                    } for item in second_findings
                ],
            }
            third = self._call(
                "Perform a final independent completeness audit of the QA critique. "
                "Re-read the original objective and each requirement. Correct omissions, "
                "false positives and weak suggestions. Return the same exact JSON schema "
                "as the previous QA critique. An empty findings list means only that this "
                "review found no code gap; it is NOT a test pass or release approval.",
                json.dumps(audit_input, ensure_ascii=False),
            )
            findings, summary, specialist = self._findings(
                third, {key for key, _ in requirements}, set(result.changed_files)
            )
            specialist = specialist or second_specialist
        except (ProviderError, ValueError, TypeError):
            return QAReview(
                task.task_id, ReviewStatus.BLOCKED, (), (),
                "QA analysis could not complete reliably; no pass decision was made.",
                None,
            )

        return QAReview(
            task_id=task.task_id,
            status=ReviewStatus.REPAIR_REQUIRED if findings else ReviewStatus.READY_FOR_TESTS,
            requirements=requirements,
            findings=findings,
            summary=summary,
            recommended_specialist=specialist,
        )



def build_qa_report(review: QAReview, evidence_store: EvidenceStore) -> QAReport:
    """Convert a completed model review into explicitly non-executed QA checks."""
    if not isinstance(review, QAReview) or not isinstance(evidence_store, EvidenceStore):
        raise TypeError("A QAReview and EvidenceStore are required.")
    if review.status == ReviewStatus.BLOCKED:
        raise ValueError("A blocked review cannot be represented as completed QA.")
    builder = QAReportBuilder(
        task_id=review.task_id,
        qa_run_id="model-qa-" + uuid.uuid4().hex,
        evidence_store=evidence_store,
    )
    findings_by_requirement = {}
    for finding in review.findings:
        findings_by_requirement.setdefault(finding.requirement_id, []).append(finding)
    for index, (requirement_id, criterion) in enumerate(review.requirements, start=1):
        findings = findings_by_requirement.get(requirement_id, [])
        if findings:
            details = " | ".join(
                f"{item.severity}: {item.issue} Expected: {item.expected} "
                f"Observed: {item.observed} Suggested fix: {item.suggestion}"
                for item in findings
            )
        else:
            details = "No source-level gap identified by the three-pass model-assisted review."
        builder.add_static_check(
            check_id=f"model-review-{index}",
            name=f"Requirement {requirement_id}",
            passed=not findings,
            details=details,
            basis=(
                "Three-pass model-assisted source review only; this is not an executed "
                "test, runtime verification, or release approval."
            ),
        )
    return builder.build()


__all__ = [
    "ReviewStatus", "ReviewFinding", "QAReview", "QASupervisor",
    "build_qa_report",
]
