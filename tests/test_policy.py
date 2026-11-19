"""Unit tests for deterministic policy decisions."""
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.policy import (
    Approval,
    DecisionStatus,
    OperatingMode,
    PolicyContext,
    ToolRequest,
    evaluate_tool_request,
    request_fingerprint,
)


NOW = datetime(2026, 11, 19, 10, 0, tzinfo=timezone.utc)


def make_context(mode=OperatingMode.AUTONOMOUS):
    return PolicyContext(
        mode=mode,
        task_id="task-1",
        worker_id="worker-qa",
        workspace_id="workspace-1",
        expected_workspace_id="workspace-1",
        task_active=True,
        worker_active=True,
        task_capabilities=frozenset({
            "workspace.read", "workspace.edit", "tests.run",
            "workspace.delete", "release.deploy",
        }),
        worker_capabilities=frozenset({
            "workspace.read", "workspace.edit", "tests.run",
            "workspace.delete", "release.deploy",
        }),
    )


def make_request(capability="workspace.read", arguments=None):
    return ToolRequest(
        request_id="request-1",
        task_id="task-1",
        worker_id="worker-qa",
        workspace_id="workspace-1",
        capability=capability,
        arguments=arguments if arguments is not None else {"path": "src/app.py"},
    )


def approval_for(request, *, expires_at=None, status="approved"):
    return Approval(
        request_id=request.request_id,
        request_fingerprint=request_fingerprint(request),
        status=status,
        approver_id="human-1",
        expires_at=expires_at or NOW + timedelta(minutes=5),
    )


class PolicyTests(unittest.TestCase):
    def test_autonomous_read_is_allowed(self):
        result = evaluate_tool_request(make_request(), make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.ALLOW)

    def test_supervised_write_requires_approval(self):
        request = make_request("workspace.edit", {"path": "src/app.py"})
        result = evaluate_tool_request(
            request, make_context(OperatingMode.SUPERVISED), now=NOW
        )
        self.assertEqual(result.status, DecisionStatus.REQUIRE_APPROVAL)

    def test_supervised_write_allowed_with_exact_approval(self):
        request = make_request("workspace.edit", {"path": "src/app.py"})
        result = evaluate_tool_request(
            request,
            make_context(OperatingMode.SUPERVISED),
            approval_for(request),
            now=NOW,
        )
        self.assertEqual(result.status, DecisionStatus.ALLOW)

    def test_approval_does_not_cover_changed_arguments(self):
        original = make_request("workspace.delete", {"path": "tmp/cache"})
        changed = make_request("workspace.delete", {"path": "src/important.py"})
        result = evaluate_tool_request(
            changed, make_context(), approval_for(original), now=NOW
        )
        self.assertEqual(result.status, DecisionStatus.REQUIRE_APPROVAL)

    def test_autonomous_destructive_action_still_requires_approval(self):
        request = make_request("workspace.delete", {"path": "tmp/cache"})
        result = evaluate_tool_request(request, make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.REQUIRE_APPROVAL)

    def test_expired_approval_is_rejected(self):
        request = make_request("release.deploy", {"environment": "production"})
        approval = approval_for(request, expires_at=NOW - timedelta(seconds=1))
        result = evaluate_tool_request(request, make_context(), approval, now=NOW)
        self.assertEqual(result.status, DecisionStatus.REQUIRE_APPROVAL)

    def test_unapproved_status_is_rejected(self):
        request = make_request("release.deploy", {"environment": "production"})
        approval = approval_for(request, status="pending")
        result = evaluate_tool_request(request, make_context(), approval, now=NOW)
        self.assertEqual(result.status, DecisionStatus.REQUIRE_APPROVAL)

    def test_unregistered_capability_is_denied(self):
        request = make_request("shell.run", {"command": "echo unsafe"})
        result = evaluate_tool_request(request, make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_capability_not_granted_to_worker_is_denied(self):
        request = make_request("workspace.edit", {"path": "src/app.py"})
        context = make_context()
        context = PolicyContext(
            **{**context.__dict__, "worker_capabilities": frozenset({"workspace.read"})}
        )
        result = evaluate_tool_request(request, context, now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_inactive_task_is_denied(self):
        context = make_context()
        context = PolicyContext(**{**context.__dict__, "task_active": False})
        result = evaluate_tool_request(make_request(), context, now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_workspace_mismatch_is_denied(self):
        request = make_request()
        context = PolicyContext(
            **{**make_context().__dict__, "expected_workspace_id": "other-workspace"}
        )
        result = evaluate_tool_request(request, context, now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_path_traversal_and_absolute_paths_are_denied(self):
        for path in ("../secret.txt", "src/../../secret.txt", "/etc/passwd", ""):
            with self.subTest(path=path):
                request = make_request("workspace.read", {"path": path})
                result = evaluate_tool_request(request, make_context(), now=NOW)
                self.assertEqual(result.status, DecisionStatus.DENY)

    def test_windows_drive_path_is_denied(self):
        request = make_request("workspace.read", {"path": "C:/Windows/system.ini"})
        result = evaluate_tool_request(request, make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_non_json_arguments_are_denied_without_crashing(self):
        request = make_request("workspace.read", {"path": "src/app.py", "bad": object()})
        result = evaluate_tool_request(request, make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)

    def test_request_identity_mismatch_is_denied(self):
        request = ToolRequest(
            request_id="request-1",
            task_id="another-task",
            worker_id="worker-qa",
            workspace_id="workspace-1",
            capability="workspace.read",
            arguments={"path": "src/app.py"},
        )
        result = evaluate_tool_request(request, make_context(), now=NOW)
        self.assertEqual(result.status, DecisionStatus.DENY)


if __name__ == "__main__":
    unittest.main()
