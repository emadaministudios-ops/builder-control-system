"""Model-backed specialist worker with policy-gated file edits.

The model is an untrusted planner/editor. Its output is parsed as a strict
contract and every proposed write is passed through a controller-owned
WorkspaceToolGateway. This worker cannot run shell commands or grant itself
capabilities.
"""
from __future__ import annotations

from typing import Callable
import json
import uuid

from .orchestrator import WorkerResult, WorkerResultStatus, WorkerTask
from .providers import ProviderRegistry, ProviderError
from .workspace_gateway import WorkspaceToolGateway


_SYSTEM_PROMPT = """You are a specialist software engineer working inside Builder Control System.
Return exactly one JSON object and no Markdown fences or surrounding commentary.
The JSON schema is:
{
  "summary": "brief explanation of the implementation",
  "edits": [
    {"path": "workspace-relative/path", "content": "complete UTF-8 file content"}
  ],
  "blockers": ["specific blocker, if any"]
}
Do not include any other keys. Never use absolute paths or parent-directory traversal.
Only propose files directly relevant to the assigned objective and acceptance criteria.
Do not claim tests were run. Do not invent test evidence. If the task is ambiguous,
unsafe, or cannot be completed with the available context, return no edits and explain
the blocker. Preserve existing behavior unless the objective explicitly changes it."""


class ModelWorker:
    """Call a configured model and apply its proposed edits through a safe gateway."""

    def __init__(
        self,
        *,
        worker_id: str,
        role: str,
        providers: ProviderRegistry,
        gateway_factory: Callable[[WorkerTask], WorkspaceToolGateway],
        provider_id: str | None = None,
        max_tokens: int = 4096,
        max_edits: int = 20,
        max_file_bytes: int = 100_000,
        max_total_bytes: int = 500_000,
    ) -> None:
        if not worker_id.strip() or not role.strip():
            raise ValueError("Worker ID and role are required.")
        if not callable(gateway_factory):
            raise TypeError("A trusted gateway factory is required.")
        for name, value, minimum, maximum in (
            ("max_tokens", max_tokens, 1, 32768),
            ("max_edits", max_edits, 1, 100),
            ("max_file_bytes", max_file_bytes, 1, 1_000_000),
            ("max_total_bytes", max_total_bytes, 1, 5_000_000),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
                raise ValueError(f"{name} is outside its allowed range.")
        self.worker_id = worker_id
        self.role = role
        self.providers = providers
        self.gateway_factory = gateway_factory
        self.provider_id = provider_id
        self.max_tokens = max_tokens
        self.max_edits = max_edits
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes

    def _prompt(self, task: WorkerTask) -> str:
        payload = {
            "task_id": task.task_id,
            "role": self.role,
            "objective": task.objective,
            "acceptance_criteria": list(task.acceptance_criteria),
            "prior_evidence_refs": list(task.evidence_refs),
            "attempt": task.attempt,
            "verified_prerequisite_outputs": [json.loads(item) for item in task.dependency_context],
        }
        return (
            "Complete this assigned task. Treat the objective and acceptance criteria "
            "as authoritative. Return a complete file content for each file you change.\n"
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        )

    def _parse_response(self, content: str) -> dict:
        try:
            data = json.loads(content)
        except (TypeError, json.JSONDecodeError) as exc:
            raise ValueError("Model response was not valid JSON.") from exc
        if not isinstance(data, dict) or set(data) != {"summary", "edits", "blockers"}:
            raise ValueError("Model response did not match the worker result schema.")
        if not isinstance(data["summary"], str) or not data["summary"].strip():
            raise ValueError("Model response summary is required.")
        if not isinstance(data["edits"], list) or len(data["edits"]) > self.max_edits:
            raise ValueError("Model response contains an invalid number of file edits.")
        if not isinstance(data["blockers"], list) or any(
            not isinstance(item, str) or not item.strip() for item in data["blockers"]
        ):
            raise ValueError("Model response blockers must be non-empty strings.")
        total_bytes = 0
        seen_paths = set()
        normalized_edits = []
        for edit in data["edits"]:
            if not isinstance(edit, dict) or set(edit) != {"path", "content"}:
                raise ValueError("Each file edit must contain exactly path and content.")
            path, body = edit["path"], edit["content"]
            if not isinstance(path, str) or not path.strip():
                raise ValueError("File edit path is required.")
            if not isinstance(body, str):
                raise ValueError("File edit content must be text.")
            encoded_size = len(body.encode("utf-8"))
            total_bytes += encoded_size
            if encoded_size > self.max_file_bytes or total_bytes > self.max_total_bytes:
                raise ValueError("Model-proposed file content exceeds configured limits.")
            if path in seen_paths:
                raise ValueError("Model response contains duplicate file paths.")
            seen_paths.add(path)
            normalized_edits.append((path, body))
        return {
            "summary": data["summary"].strip(),
            "edits": tuple(normalized_edits),
            "blockers": tuple(item.strip() for item in data["blockers"]),
        }

    def __call__(self, task: WorkerTask) -> WorkerResult:
        if not isinstance(task, WorkerTask) or task.role != self.role:
            return WorkerResult(
                WorkerResultStatus.ERROR,
                "Worker received a task that does not match its assigned role.",
            )
        try:
            response = self.providers.complete(
                [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": self._prompt(task)},
                ],
                provider_id=self.provider_id,
                temperature=0.1,
                max_tokens=self.max_tokens,
            )
            proposal = self._parse_response(response.content)
        except (ProviderError, ValueError, TypeError) as exc:
            return WorkerResult(
                WorkerResultStatus.ERROR,
                f"Model worker could not produce a valid proposal: {type(exc).__name__}.",
            )

        if proposal["blockers"] and not proposal["edits"]:
            return WorkerResult(
                WorkerResultStatus.BLOCKED,
                proposal["summary"],
                blockers=proposal["blockers"],
            )
        if not proposal["edits"]:
            return WorkerResult(
                WorkerResultStatus.BLOCKED,
                proposal["summary"],
                blockers=("The model proposed no file changes; further direction or context is needed.",),
            )

        try:
            gateway = self.gateway_factory(task)
            if not isinstance(gateway, WorkspaceToolGateway):
                raise TypeError("Gateway factory returned an unsupported gateway.")
        except Exception:
            return WorkerResult(
                WorkerResultStatus.ERROR,
                "Trusted workspace gateway could not be initialized.",
            )

        changed_files = []
        for path, body in proposal["edits"]:
            result = gateway.edit_file(
                "model-edit-" + uuid.uuid4().hex,
                path,
                body,
            )
            if result.status != "completed":
                reason = result.reason or "Workspace gateway rejected the proposed edit."
                blockers = (reason,)
                if result.approval_request_id:
                    blockers += ("Approval request: " + result.approval_request_id,)
                return WorkerResult(
                    WorkerResultStatus.BLOCKED,
                    "Model proposal was not fully applied; human approval or correction is required.",
                    changed_files=tuple(changed_files),
                    blockers=blockers,
                )
            changed_files.append(path)

        return WorkerResult(
            WorkerResultStatus.COMPLETED,
            proposal["summary"],
            changed_files=tuple(changed_files),
            blockers=proposal["blockers"],
        )


__all__ = ["ModelWorker"]
