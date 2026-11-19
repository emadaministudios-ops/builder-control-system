"""Policy- and approval-gated access to a controller-owned workspace.

The gateway accepts only an action ID/capability/arguments from a worker. Task,
worker, and workspace identity always come from trusted controller context.
This is application-level authorization, not an OS sandbox.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from .approvals import ApprovalService, ApprovalStatus
from .policy import (
    DecisionStatus,
    PolicyContext,
    ToolRequest,
    evaluate_tool_request,
)
from .workspace import Workspace, WorkspaceError


@dataclass(frozen=True)
class GatewayResult:
    status: str
    reason: str
    approval_request_id: str | None = None
    content: str | None = None


class WorkspaceToolGateway:
    """Expose narrow read/edit operations after deterministic authorization."""

    def __init__(
        self,
        workspace: Workspace,
        context: PolicyContext,
        approvals: ApprovalService,
    ) -> None:
        if workspace.workspace_id != context.workspace_id:
            raise ValueError("Workspace does not match the trusted policy context.")
        if context.workspace_id != context.expected_workspace_id:
            raise ValueError("Workspace does not match the expected task workspace.")
        self._workspace = workspace
        self._context = context
        self._approvals = approvals

    def _authorize(
        self,
        request_id: str,
        capability: str,
        arguments: Mapping[str, Any],
        *,
        now: datetime | None,
    ) -> tuple[ToolRequest, GatewayResult | None]:
        # Never accept task/worker/workspace IDs from the worker request.
        request = ToolRequest(
            request_id=request_id,
            task_id=self._context.task_id,
            worker_id=self._context.worker_id,
            workspace_id=self._workspace.workspace_id,
            capability=capability,
            arguments=dict(arguments),
        )
        approval = self._approvals.policy_approval(request_id, now=now)
        decision = evaluate_tool_request(request, self._context, approval, now=now)

        if decision.status == DecisionStatus.DENY:
            return request, GatewayResult("blocked", decision.reason)
        if decision.status == DecisionStatus.REQUIRE_APPROVAL:
            try:
                record = self._approvals.submit(request, now=now)
            except (TypeError, ValueError) as exc:
                return request, GatewayResult("blocked", str(exc))
            if record.status == ApprovalStatus.REJECTED:
                reason = "Human approval was rejected."
            elif record.status == ApprovalStatus.EXPIRED:
                reason = "Approval request expired; submit a new request ID."
            elif record.status == ApprovalStatus.CANCELLED:
                reason = "Approval request was cancelled."
            else:
                reason = decision.reason
            return request, GatewayResult(
                "blocked", reason, approval_request_id=record.request_id
            )
        return request, None

    def read_file(
        self,
        request_id: str,
        path: str,
        *,
        now: datetime | None = None,
    ) -> GatewayResult:
        request, blocked = self._authorize(
            request_id, "workspace.read", {"path": path}, now=now
        )
        if blocked is not None:
            return blocked
        try:
            return GatewayResult(
                "completed",
                "Read access authorized.",
                content=self._workspace.read_text(path),
            )
        except (OSError, UnicodeError, WorkspaceError) as exc:
            return GatewayResult("error", str(exc))

    def edit_file(
        self,
        request_id: str,
        path: str,
        content: str,
        *,
        now: datetime | None = None,
    ) -> GatewayResult:
        request, blocked = self._authorize(
            request_id,
            "workspace.edit",
            {"path": path, "content": content},
            now=now,
        )
        if blocked is not None:
            return blocked
        try:
            target = self._workspace.resolve_path(path)
            if target.exists() and not target.is_file():
                return GatewayResult("error", "Requested workspace path is not a file.")
            if not target.parent.is_dir():
                return GatewayResult("error", "Parent directory must already exist.")
            target.write_text(content, encoding="utf-8")
            return GatewayResult("completed", "File edit authorized and written.")
        except (OSError, UnicodeError, WorkspaceError) as exc:
            return GatewayResult("error", str(exc))
