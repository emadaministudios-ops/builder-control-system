"""Build a restrictive Docker command for project task execution.

This module does not launch containers. The image and Docker daemon are trusted
controller configuration; workers may supply only an argument vector after a
separate capability and approval check.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import re
from typing import Sequence


class SandboxConfigError(ValueError):
    """Raised when a sandbox configuration or command is unsafe."""


@dataclass(frozen=True)
class SandboxLimits:
    memory: str = "512m"
    cpus: str = "1.0"
    pids: int = 128
    tmpfs_size: str = "64m"
    timeout_seconds: int = 300
    output_limit_bytes: int = 1_000_000

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[1-9][0-9]*[kKmMgG]?", self.memory):
            raise SandboxConfigError("Memory limit must be a positive Docker size.")
        if not re.fullmatch(r"(?:[0-9]+(?:\.[0-9]+)?)", self.cpus):
            raise SandboxConfigError("CPU limit must be a positive number.")
        if float(self.cpus) <= 0:
            raise SandboxConfigError("CPU limit must be greater than zero.")
        if not isinstance(self.pids, int) or isinstance(self.pids, bool) or self.pids < 1:
            raise SandboxConfigError("PID limit must be a positive integer.")
        if not re.fullmatch(r"[1-9][0-9]*[kKmMgG]?", self.tmpfs_size):
            raise SandboxConfigError("Tmpfs size must be a positive Docker size.")
        if not isinstance(self.timeout_seconds, int) or isinstance(self.timeout_seconds, bool) or self.timeout_seconds < 1:
            raise SandboxConfigError("Timeout must be a positive integer.")
        if not isinstance(self.output_limit_bytes, int) or isinstance(self.output_limit_bytes, bool) or self.output_limit_bytes < 1:
            raise SandboxConfigError("Output limit must be a positive integer.")


def build_docker_command(
    *,
    docker_executable: str,
    image: str,
    workspace_root: Path,
    container_name: str,
    argv: Sequence[str],
    limits: SandboxLimits = SandboxLimits(),
) -> tuple[str, ...]:
    """Return a Docker CLI argv with conservative isolation defaults.

    Callers must policy-authorize the task before building or executing this
    command. The controller must restrict images to a trusted allowlist.
    """
    if not isinstance(docker_executable, str) or not docker_executable or "\x00" in docker_executable:
        raise SandboxConfigError("A valid Docker executable is required.")
    if not isinstance(image, str) or not image or any(ch.isspace() for ch in image) or "\x00" in image:
        raise SandboxConfigError("A single trusted container image reference is required.")
    if not isinstance(container_name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,62}", container_name):
        raise SandboxConfigError("Container name must be a safe Docker identifier.")
    if isinstance(argv, (str, bytes)) or not argv:
        raise SandboxConfigError("Command must be a non-empty argument vector.")
    if any(not isinstance(arg, str) or "\x00" in arg for arg in argv):
        raise SandboxConfigError("Command arguments must be NUL-free strings.")
    if not argv[0]:
        raise SandboxConfigError("Command executable cannot be empty.")

    try:
        root = Path(workspace_root).resolve(strict=True)
    except (OSError, RuntimeError, TypeError) as exc:
        raise SandboxConfigError("Workspace root must exist and be accessible.") from exc
    if not root.is_dir():
        raise SandboxConfigError("Workspace root must be a directory.")

    return (
        docker_executable, "run", "--rm",
        "--name", container_name,
        "--network", "none",
        "--read-only",
        "--cap-drop=ALL",
        "--security-opt=no-new-privileges",
        "--pids-limit", str(limits.pids),
        "--memory", limits.memory,
        "--cpus", limits.cpus,
        "--user", "10001:10001",
        "--tmpfs", f"/tmp:rw,noexec,nosuid,size={limits.tmpfs_size}",
        "--mount", f"type=bind,src={root},dst=/workspace,rw",
        "--workdir", "/workspace",
        "--env", "HOME=/tmp",
        "--env", "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        image,
        *tuple(argv),
    )
