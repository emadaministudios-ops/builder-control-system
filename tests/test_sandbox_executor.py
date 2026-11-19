"""Tests for the policy-gated Docker execution controller."""
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.approvals import ApprovalService, ActorType
from bcs.policy import OperatingMode, PolicyContext
from bcs.sandbox import SandboxLimits
from bcs.sandbox_executor import DockerSandboxExecutor
from bcs.workspace import Workspace


class FakeProcess:
    def __init__(self, code=0, stdout=b"ok", stderr=b"", timeout=False):
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.code = code
        self.timeout = timeout
        self.wait_calls = 0
        self.killed = False

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.timeout and self.wait_calls == 1:
            import subprocess
            raise subprocess.TimeoutExpired("docker", timeout)
        return self.code

    def kill(self):
        self.killed = True


class SandboxExecutorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.workspace = Workspace("ws-1", Path(self.temp.name))
        self.approvals = ApprovalService()
        self.context = PolicyContext(
            mode=OperatingMode.AUTONOMOUS,
            task_id="task-1", worker_id="qa-1",
            workspace_id="ws-1", expected_workspace_id="ws-1",
            task_active=True, worker_active=True,
            task_capabilities=frozenset({"tests.run"}),
            worker_capabilities=frozenset({"tests.run"}),
        )

    def executor(self, process, *, context=None, limits=None, cleanup=None):
        return DockerSandboxExecutor(
            workspace=self.workspace, context=context or self.context,
            approvals=self.approvals, docker_executable="docker",
            image="python:3.12-slim",
            allowed_images=frozenset({"python:3.12-slim"}),
            limits=limits or SandboxLimits(),
            popen_factory=Mock(return_value=process),
            cleanup_runner=cleanup or Mock(),
        )

    def test_autonomous_execution_launches_without_shell(self):
        process = FakeProcess(stdout=b"tests passed")
        popen = Mock(return_value=process)
        executor = DockerSandboxExecutor(
            workspace=self.workspace, context=self.context, approvals=self.approvals,
            docker_executable="docker", image="python:3.12-slim",
            allowed_images=frozenset({"python:3.12-slim"}), popen_factory=popen,
        )
        result = executor.execute("run-1", ("python", "-m", "unittest"))
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.stdout, "tests passed")
        args, kwargs = popen.call_args
        self.assertIsInstance(args[0], tuple)
        self.assertFalse(kwargs["shell"])
        self.assertIn("--network", args[0])

    def test_supervised_execution_waits_for_exact_human_approval(self):
        supervised = PolicyContext(
            mode=OperatingMode.SUPERVISED, task_id="task-1", worker_id="qa-1",
            workspace_id="ws-1", expected_workspace_id="ws-1",
            task_active=True, worker_active=True,
            task_capabilities=frozenset({"tests.run"}),
            worker_capabilities=frozenset({"tests.run"}),
        )
        popen = Mock(return_value=FakeProcess())
        executor = DockerSandboxExecutor(
            workspace=self.workspace, context=supervised, approvals=self.approvals,
            docker_executable="docker", image="python:3.12-slim",
            allowed_images=frozenset({"python:3.12-slim"}), popen_factory=popen,
        )
        first = executor.execute("run-approval", ("python", "-m", "unittest"))
        self.assertEqual(first.status, "blocked")
        self.assertEqual(first.approval_request_id, "run-approval")
        popen.assert_not_called()
        self.approvals.decide(
            "run-approval", actor_id="owner-1", actor_type=ActorType.HUMAN, approved=True
        )
        second = executor.execute("run-approval", ("python", "-m", "unittest"))
        self.assertEqual(second.status, "completed")
        popen.assert_called_once()

    def test_changed_command_cannot_reuse_approval(self):
        supervised = PolicyContext(
            mode=OperatingMode.SUPERVISED, task_id="task-1", worker_id="qa-1",
            workspace_id="ws-1", expected_workspace_id="ws-1",
            task_active=True, worker_active=True,
            task_capabilities=frozenset({"tests.run"}),
            worker_capabilities=frozenset({"tests.run"}),
        )
        popen = Mock(return_value=FakeProcess())
        executor = DockerSandboxExecutor(
            workspace=self.workspace, context=supervised, approvals=self.approvals,
            docker_executable="docker", image="python:3.12-slim",
            allowed_images=frozenset({"python:3.12-slim"}), popen_factory=popen,
        )
        executor.execute("same-id", ("python", "-m", "unittest"))
        self.approvals.decide(
            "same-id", actor_id="owner-1", actor_type=ActorType.HUMAN, approved=True
        )
        changed = executor.execute("same-id", ("python", "-c", "print('different')"))
        self.assertEqual(changed.status, "blocked")
        popen.assert_not_called()

    def test_missing_capability_blocks_before_docker(self):
        denied = PolicyContext(
            mode=OperatingMode.AUTONOMOUS, task_id="task-1", worker_id="qa-1",
            workspace_id="ws-1", expected_workspace_id="ws-1",
            task_active=True, worker_active=True,
            task_capabilities=frozenset(), worker_capabilities=frozenset(),
        )
        popen = Mock(return_value=FakeProcess())
        executor = self.executor(FakeProcess(), context=denied)
        executor._popen = popen
        result = executor.execute("denied", ("python", "-m", "unittest"))
        self.assertEqual(result.status, "blocked")
        popen.assert_not_called()

    def test_output_is_capped_across_both_streams(self):
        process = FakeProcess(stdout=b"123456", stderr=b"abcdef")
        result = self.executor(
            process, limits=SandboxLimits(output_limit_bytes=8)
        ).execute("capped", ("python", "-m", "unittest"))
        self.assertEqual(len(result.stdout.encode()) + len(result.stderr.encode()), 8)
        self.assertTrue(result.output_truncated)

    def test_nonzero_exit_is_reported_as_failed(self):
        result = self.executor(FakeProcess(code=3, stderr=b"failure")).execute(
            "failed", ("python", "-m", "unittest")
        )
        self.assertEqual(result.status, "failed")
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.stderr, "failure")

    def test_timeout_kills_cli_and_requests_container_cleanup(self):
        process = FakeProcess(timeout=True)
        cleanup = Mock()
        result = self.executor(process, cleanup=cleanup).execute(
            "timeout", ("python", "-m", "unittest")
        )
        self.assertEqual(result.status, "timeout")
        self.assertTrue(result.timed_out)
        self.assertTrue(process.killed)
        self.assertEqual(cleanup.call_count, 2)
        self.assertEqual(cleanup.call_args_list[0].args[0][1], "kill")
        self.assertEqual(cleanup.call_args_list[1].args[0][1:3], ("rm", "-f"))

    def test_untrusted_image_cannot_be_configured(self):
        with self.assertRaises(ValueError):
            DockerSandboxExecutor(
                workspace=self.workspace, context=self.context, approvals=self.approvals,
                docker_executable="docker", image="evil:latest",
                allowed_images=frozenset({"python:3.12-slim"}),
            )

    def test_invalid_worker_argv_never_launches(self):
        popen = Mock(return_value=FakeProcess())
        executor = DockerSandboxExecutor(
            workspace=self.workspace, context=self.context, approvals=self.approvals,
            docker_executable="docker", image="python:3.12-slim",
            allowed_images=frozenset({"python:3.12-slim"}), popen_factory=popen,
        )
        result = executor.execute("bad-argv", "python -m unittest")
        self.assertEqual(result.status, "error")
        popen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
