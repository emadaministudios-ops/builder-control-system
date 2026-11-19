"""Policy-gated Docker execution controller with bounded output and cleanup."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import subprocess
import threading
import uuid
from typing import Callable, Sequence

from .approvals import ApprovalService, ApprovalStatus
from .policy import DecisionStatus, PolicyContext, ToolRequest, evaluate_tool_request
from .sandbox import SandboxConfigError, SandboxLimits, build_docker_command
from .workspace import Workspace


@dataclass(frozen=True)
class ExecutionResult:
    status: str
    reason: str
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    timed_out: bool = False
    output_truncated: bool = False
    approval_request_id: str | None = None


class DockerSandboxExecutor:
    """Launch approved commands in a controller-configured, networkless container.

    The worker supplies argv only. Image, Docker executable, workspace, limits,
    and container identity are controller-owned. A trusted Docker daemon remains
    a deployment prerequisite; this class is not an OS-level host isolation layer.
    """

    def __init__(
        self,
        *,
        workspace: Workspace,
        context: PolicyContext,
        approvals: ApprovalService,
        docker_executable: str,
        image: str,
        allowed_images: frozenset[str],
        limits: SandboxLimits = SandboxLimits(),
        popen_factory: Callable = subprocess.Popen,
        cleanup_runner: Callable = subprocess.run,
    ) -> None:
        if workspace.workspace_id != context.workspace_id or context.workspace_id != context.expected_workspace_id:
            raise ValueError("Workspace does not match trusted task context.")
        if not allowed_images or image not in allowed_images:
            raise ValueError("Configured image is not in the controller image allowlist.")
        self._workspace = workspace
        self._context = context
        self._approvals = approvals
        self._docker = docker_executable
        self._image = image
        self._limits = limits
        self._popen = popen_factory
        self._cleanup_runner = cleanup_runner

    def execute(
        self,
        request_id: str,
        argv: Sequence[str],
        *,
        now: datetime | None = None,
    ) -> ExecutionResult:
        """Execute a command or return blocked pending a matching human approval."""
        # Include every execution-affecting setting in the approval fingerprint.
        if isinstance(argv, (str, bytes)) or not isinstance(argv, Sequence) or not argv:
            return ExecutionResult("error", "Command must be a non-empty argument vector.")
        if any(not isinstance(arg, str) or "\x00" in arg for arg in argv) or not argv[0]:
            return ExecutionResult("error", "Command arguments must be NUL-free strings and start with an executable.")
        arguments = {
            "argv": list(argv),
            "image": self._image,
            "limits": {
                "memory": self._limits.memory, "cpus": self._limits.cpus,
                "pids": self._limits.pids, "tmpfs_size": self._limits.tmpfs_size,
                "timeout_seconds": self._limits.timeout_seconds,
                "output_limit_bytes": self._limits.output_limit_bytes,
            },
        }
        request = ToolRequest(
            request_id=request_id,
            task_id=self._context.task_id,
            worker_id=self._context.worker_id,
            workspace_id=self._workspace.workspace_id,
            capability="tests.run",
            arguments=arguments,
        )
        try:
            approval = self._approvals.policy_approval(request_id, now=now)
            decision = evaluate_tool_request(request, self._context, approval, now=now)
        except (TypeError, ValueError) as exc:
            return ExecutionResult("blocked", str(exc))

        if decision.status == DecisionStatus.DENY:
            return ExecutionResult("blocked", decision.reason)
        if decision.status == DecisionStatus.REQUIRE_APPROVAL:
            try:
                record = self._approvals.submit(request, now=now)
            except (TypeError, ValueError) as exc:
                return ExecutionResult("blocked", str(exc))
            if record.status == ApprovalStatus.REJECTED:
                reason = "Human approval was rejected."
            elif record.status == ApprovalStatus.EXPIRED:
                reason = "Approval expired; submit a new request ID."
            elif record.status == ApprovalStatus.CANCELLED:
                reason = "Approval was cancelled."
            else:
                reason = "Human approval is required before this command can run."
            return ExecutionResult("blocked", reason, approval_request_id=record.request_id)

        container_name = "bcs-" + uuid.uuid4().hex[:24]
        try:
            command = build_docker_command(
                docker_executable=self._docker,
                image=self._image,
                workspace_root=self._workspace.root,
                container_name=container_name,
                argv=argv,
                limits=self._limits,
            )
        except (SandboxConfigError, TypeError, ValueError) as exc:
            return ExecutionResult("error", str(exc))

        try:
            process = self._popen(
                command, stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                shell=False, bufsize=0,
            )
        except OSError as exc:
            return ExecutionResult("error", f"Could not start Docker: {exc}")

        output_lock = threading.Lock()
        output = {"stdout": bytearray(), "stderr": bytearray()}
        total = 0
        truncated = False
        cap = self._limits.output_limit_bytes

        def drain(stream, key: str) -> None:
            nonlocal total, truncated
            try:
                while True:
                    chunk = stream.read(4096)
                    if not chunk:
                        break
                    if isinstance(chunk, str):
                        chunk = chunk.encode("utf-8", errors="replace")
                    with output_lock:
                        remaining = max(0, cap - total)
                        keep = chunk[:remaining]
                        output[key].extend(keep)
                        total += len(keep)
                        if len(keep) < len(chunk):
                            truncated = True
            except (OSError, ValueError):
                pass

        readers = [
            threading.Thread(target=drain, args=(process.stdout, "stdout"), daemon=True),
            threading.Thread(target=drain, args=(process.stderr, "stderr"), daemon=True),
        ]
        for reader in readers:
            reader.start()

        timed_out = False
        try:
            exit_code = process.wait(timeout=self._limits.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            try:
                process.wait(timeout=5)
            except (subprocess.TimeoutExpired, OSError):
                pass
            # Killing the CLI may not kill the container. Explicitly stop and remove it.
            try:
                self._cleanup_runner(
                    (self._docker, "kill", container_name),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, timeout=5, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            try:
                self._cleanup_runner(
                    (self._docker, "rm", "-f", container_name),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, shell=False, timeout=5, check=False,
                )
            except (OSError, subprocess.SubprocessError):
                pass
            exit_code = None
        finally:
            for stream in (process.stdout, process.stderr):
                try:
                    stream.close()
                except (OSError, AttributeError):
                    pass
            for reader in readers:
                reader.join(timeout=1)

        stdout = bytes(output["stdout"]).decode("utf-8", errors="replace")
        stderr = bytes(output["stderr"]).decode("utf-8", errors="replace")
        if timed_out:
            return ExecutionResult("timeout", "Execution exceeded its time limit; container cleanup was requested.",
                                   exit_code=exit_code, stdout=stdout, stderr=stderr,
                                   timed_out=True, output_truncated=truncated)
        if exit_code == 0:
            return ExecutionResult("completed", "Container command completed successfully.",
                                   exit_code=0, stdout=stdout, stderr=stderr,
                                   output_truncated=truncated)
        return ExecutionResult("failed", "Container command returned a non-zero exit code.",
                               exit_code=exit_code, stdout=stdout, stderr=stderr,
                               output_truncated=truncated)
