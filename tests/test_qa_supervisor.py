"""Tests for three-pass model-assisted QA supervision."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ApprovalService
from bcs.orchestrator import WorkerResult, WorkerResultStatus, WorkerTask
from bcs.policy import OperatingMode, PolicyContext
from bcs.providers import ProviderResponse
from bcs.qa_supervisor import QASupervisor, ReviewStatus
from bcs.workspace import Workspace
from bcs.workspace_gateway import WorkspaceToolGateway


class FakeProviders:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def complete(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        value = self.responses.pop(0)
        return ProviderResponse("fake", "fake-model", value)


class QASupervisorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "src").mkdir()
        (self.root / "src" / "modal.tsx").write_text(
            "export function Modal() { return null; }\n"
        )
        self.task = WorkerTask(
            task_id="task-qa", parent_task_id="", role="frontend-developer",
            objective="The role-selection modal must close after selection.",
            acceptance_criteria=("Selecting Listener, Artist or Both closes the modal.",),
            evidence_refs=(),
        )
        self.result = WorkerResult(
            WorkerResultStatus.COMPLETED, "Implemented modal close behavior.",
            changed_files=("src/modal.tsx",),
        )

    def tearDown(self):
        self.temp.cleanup()

    def gateway(self, task):
        grants = frozenset({"workspace.read"})
        context = PolicyContext(
            mode=OperatingMode.AUTONOMOUS, task_id=task.task_id,
            worker_id="qa-supervisor", workspace_id="qa-workspace",
            expected_workspace_id="qa-workspace", task_active=True,
            worker_active=True, task_capabilities=grants, worker_capabilities=grants,
        )
        return WorkspaceToolGateway(
            Workspace("qa-workspace", self.root), context, ApprovalService()
        )

    def responses(self, findings):
        return [
            '{"requirements":[{"id":"R1","criterion":"Modal closes after role selection."}]}',
            '{"summary":"Initial review found a gap","findings":[],"recommended_specialist":null}',
            '{"summary":"Final review found an unclosed modal path","findings":' + findings + ',"recommended_specialist":"frontend-developer"}',
        ]

    def test_three_pass_review_returns_actionable_gap_and_specialist(self):
        finding = (
            '[{"requirement_id":"R1","severity":"high",'
            '"issue":"The component has no close-state update.",'
            '"expected":"The modal closes after a role is selected.",'
            '"observed":"The component currently returns null without handling selection.",'
            '"suggestion":"Connect the selection callback to modal state and add a UI test.",'
            '"file_path":"src/modal.tsx"}]'
        )
        providers = FakeProviders(self.responses(finding))
        supervisor = QASupervisor(providers=providers, gateway_factory=self.gateway)
        review = supervisor.review(self.task, self.result)
        self.assertEqual(review.status, ReviewStatus.REPAIR_REQUIRED)
        self.assertEqual(review.analysis_rounds, 3)
        self.assertEqual(len(providers.calls), 3)
        self.assertEqual(review.findings[0].requirement_id, "R1")
        self.assertEqual(review.recommended_specialist, "frontend-developer")

    def test_no_findings_means_ready_for_tests_not_passed(self):
        providers = FakeProviders(self.responses("[]"))
        supervisor = QASupervisor(providers=providers, gateway_factory=self.gateway)
        review = supervisor.review(self.task, self.result)
        self.assertEqual(review.status, ReviewStatus.READY_FOR_TESTS)
        self.assertEqual(review.findings, ())
        self.assertNotEqual(review.status.value, "passed")

    def test_blocked_worker_is_not_reviewed_or_marked_passed(self):
        providers = FakeProviders([])
        supervisor = QASupervisor(providers=providers, gateway_factory=self.gateway)
        blocked = WorkerResult(WorkerResultStatus.BLOCKED, "Needs approval")
        review = supervisor.review(self.task, blocked)
        self.assertEqual(review.status, ReviewStatus.BLOCKED)
        self.assertEqual(providers.calls, [])


    def test_analysis_only_worker_without_changed_files_can_be_reviewed(self):
        providers = FakeProviders(self.responses("[]"))
        supervisor = QASupervisor(providers=providers, gateway_factory=self.gateway)
        analysis_task = WorkerTask(
            task_id="analysis-task", parent_task_id="", role="requirements-analyst",
            objective="Identify the requirements and edge cases.",
            acceptance_criteria=("Requirements are clear and actionable.",),
            evidence_refs=(),
        )
        analysis_result = WorkerResult(
            WorkerResultStatus.COMPLETED,
            "Identified the required user flows and edge cases.",
        )
        review = supervisor.review(analysis_task, analysis_result)
        self.assertEqual(review.status, ReviewStatus.READY_FOR_TESTS)
        self.assertEqual(len(providers.calls), 3)

    def test_model_cannot_reference_uninspected_file(self):
        findings = (
            '[{"requirement_id":"R1","severity":"high","issue":"Gap",'
            '"expected":"Close","observed":"Open","suggestion":"Fix it",'
            '"file_path":"secret.txt"}]'
        )
        providers = FakeProviders(self.responses(findings))
        supervisor = QASupervisor(providers=providers, gateway_factory=self.gateway)
        review = supervisor.review(self.task, self.result)
        self.assertEqual(review.status, ReviewStatus.BLOCKED)
        self.assertEqual(review.findings, ())


if __name__ == "__main__":
    unittest.main()
