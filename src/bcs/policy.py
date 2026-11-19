"""Deterministic authorization decisions for Builder Control System.

This module decides whether a registered capability may be invoked. It does not
execute tools, inspect arbitrary commands, or treat model-generated risk scores
as authoritative.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
from typing import Any, Mapping


class OperatingMode(str, Enum):
    SUPERVISED = "supervised"
    AUTONOMOUS = "autonomous"


class ActionClass(str, Enum):
    READ = "read"
    WRITE = "write"
    TEST = "test"
    DESTRUCTIVE = "destructive"
    EXTERNAL = "external"
    DEPLOY = "deploy"
    SECRET = "secret"


class DecisionStatus(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    REQUIRE_APPROVAL = "require_approval"


@dataclass(frozen=True)
class Capability:
    name: str
    action_class: ActionClass


# This is a deliberately small registry. Add capabilities only with tests and
# a documented executor that enforces the same scope.
CAPABILITIES: Mapping[str, Capability] = {
    "workspace.read": Capability("workspace.read", ActionClass.READ),
    "workspace.edit": Capability("workspace.edit", ActionClass.WRITE),
    "tests.run": Capability("tests.run", ActionClass.TEST),
    "workspace.delete": Capability("workspace.delete", ActionClass.DESTRUCTIVE),
    "network.request": Capability("network.request", ActionClass.EXTERNAL),
    "release.deploy": Capability("release.deploy", ActionClass.DEPLOY),
    "secrets.access": Capability("secrets.access", ActionClass.SECRET),
}


@dataclass(frozen=True)
class Approval:
    """Trusted approval record loaded by the control system, never by a worker."""

    request_id: str
    request_fingerprint: str
    status: str
    approver_id: str
    expires_at: datetime


@dataclass(frozen=True)
class PolicyContext:
    mode: OperatingMode
    task_id: str
    worker_id: str
    workspace_id: str
    expected_workspace_id: str
    task_active: bool
    worker_active: bool
    task_capabilities: frozenset[str]
    worker_capabilities: frozenset[str]


@dataclass(frozen=True)
class ToolRequest:
    request_id: str
    task_id: str
    worker_id: str
    workspace_id: str
    capability: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True)
class PolicyDecision:
    status: DecisionStatus
    reason: str
    request_fingerprint: str


APPROVAL_REQUIRED_CLASSES = frozenset({
    ActionClass.DESTRUCTIVE,
    ActionClass.EXTERNAL,
    ActionClass.DEPLOY,
    ActionClass.SECRET,
})


def request_fingerprint(request: ToolRequest) -> str:
    """Hash the exact action identity and arguments for approval binding."""
    payload = {
        "request_id": request.request_id,
        "task_id": request.task_id,
        "worker_id": request.worker_id,
        "workspace_id": request.workspace_id,
        "capability": request.capability,
        "arguments": request.arguments,
    }
    canonical = json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _valid_workspace_relative_path(value: Any) -> bool:
    """Reject absolute paths, traversal, empty paths, and malformed path values."""
    if not isinstance(value, str) or not value or "\x00" in value:
        return False
    normalized = value.replace("\\", "/")
    if normalized.startswith("/") or normalized.startswith("~"):
        return False
    parts = normalized.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def _approval_matches(
    approval: Approval | None,
    request: ToolRequest,
    fingerprint: str,
    now: datetime,
) -> bool:
    if approval is None:
        return False
    if approval.request_id != request.request_id:
        return False
    if approval.request_fingerprint != fingerprint:
        return False
    if approval.status != "approved" or not approval.approver_id:
        return False
    expires_at = approval.expires_at
    if expires_at.tzinfo is None:
        return False
    return expires_at > now


def evaluate_tool_request(
    request: ToolRequest,
    context: PolicyContext,
    approval: Approval | None = None,
    *,
    now: datetime | None = None,
) -> PolicyDecision:
    """Return an authorization decision; never invokes the requested capability."""
    fingerprint = request_fingerprint(request)
    current_time = now or datetime.now(timezone.utc)

    def deny(reason: str) -> PolicyDecision:
        return PolicyDecision(DecisionStatus.DENY, reason, fingerprint)

    if not request.request_id or not request.task_id or not request.worker_id:
        return deny("Request identity fields are required.")
    if request.task_id != context.task_id or request.worker_id != context.worker_id:
        return deny("Request identity does not match the active task context.")
    if not context.task_active or not context.worker_active:
        return deny("Task and worker must both be active.")
    if request.workspace_id != context.workspace_id:
        return deny("Request workspace does not match the assigned workspace.")
    if context.workspace_id != context.expected_workspace_id:
        return deny("Assigned workspace does not match the trusted task workspace.")

    capability = CAPABILITIES.get(request.capability)
    if capability is None:
        return deny("Capability is not registered.")
    if request.capability not in context.task_capabilities:
        return deny("Capability was not granted to this task.")
    if request.capability not in context.worker_capabilities:
        return deny("Capability was not granted to this worker.")

    # File-related capabilities must name a relative path. The eventual
    # executor must additionally resolve symlinks and enforce the workspace root.
    if request.capability in {"workspace.read", "workspace.edit", "workspace.delete"}:
        path = request.arguments.get("path")
        if not _valid_workspace_relative_path(path):
            return deny("A safe workspace-relative path is required.")

    approval_required = capability.action_class in APPROVAL_REQUIRED_CLASSES
    if context.mode == OperatingMode.SUPERVISED and capability.action_class in {
        ActionClass.WRITE, ActionClass.TEST
    }:
        approval_required = True

    if approval_required:
        if not _approval_matches(approval, request, fingerprint, current_time):
            return PolicyDecision(
                DecisionStatus.REQUIRE_APPROVAL,
                "This action requires a valid approval bound to its exact request.",
                fingerprint,
            )

    return PolicyDecision(
        DecisionStatus.ALLOW,
        "Capability, identity, workspace, and approval checks passed.",
        fingerprint,
    )
