"""Tests for restrictive Docker command construction."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.sandbox import SandboxConfigError, SandboxLimits, build_docker_command


class SandboxCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def build(self, **overrides):
        args = {
            "docker_executable": "docker",
            "image": "python:3.12-slim",
            "workspace_root": self.root,
            "container_name": "bcs-task-123",
            "argv": ("python", "-m", "unittest"),
        }
        args.update(overrides)
        return build_docker_command(**args)

    def test_command_has_required_isolation_flags(self):
        command = self.build()
        self.assertIn("--network", command)
        self.assertEqual(command[command.index("--network") + 1], "none")
        self.assertIn("--read-only", command)
        self.assertIn("--cap-drop=ALL", command)
        self.assertIn("--security-opt=no-new-privileges", command)
        self.assertIn("--user", command)
        self.assertEqual(command[command.index("--user") + 1], "10001:10001")
        self.assertIn("--pids-limit", command)
        self.assertIn("--memory", command)
        self.assertIn("--cpus", command)
        self.assertIn("--tmpfs", command)

    def test_workspace_is_mounted_only_at_fixed_container_path(self):
        command = self.build()
        mount = command[command.index("--mount") + 1]
        self.assertTrue(mount.startswith(f"type=bind,src={self.root},dst=/workspace,rw"))
        self.assertEqual(command[command.index("--workdir") + 1], "/workspace")

    def test_worker_argv_is_preserved_without_shell_interpolation(self):
        argv = ("python", "-c", "print('hello; echo unsafe')")
        command = self.build(argv=argv)
        self.assertEqual(command[-len(argv):], argv)
        self.assertNotIn("sh", command[:command.index("python:3.12-slim")])

    def test_rejects_string_instead_of_argv(self):
        with self.assertRaises(SandboxConfigError):
            self.build(argv="python -m unittest")

    def test_rejects_empty_or_malformed_argv(self):
        for argv in ((), ("",), ("python", "bad\x00arg"), (1,)):
            with self.subTest(argv=argv), self.assertRaises(SandboxConfigError):
                self.build(argv=argv)

    def test_rejects_unsafe_image_and_container_name(self):
        for image in ("", "python image", "python\n--privileged"):
            with self.subTest(image=image), self.assertRaises(SandboxConfigError):
                self.build(image=image)
        with self.assertRaises(SandboxConfigError):
            self.build(container_name="bad name")

    def test_rejects_missing_or_non_directory_workspace(self):
        with self.assertRaises(SandboxConfigError):
            self.build(workspace_root=self.root / "missing")
        file_path = self.root / "file.txt"
        file_path.write_text("x")
        with self.assertRaises(SandboxConfigError):
            self.build(workspace_root=file_path)

    def test_limits_validate_values(self):
        for kwargs in (
            {"memory": "0m"}, {"memory": "--privileged"},
            {"cpus": "0"}, {"cpus": "nan"}, {"pids": 0},
            {"tmpfs_size": "0m"}, {"timeout_seconds": 0},
            {"output_limit_bytes": 0},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(SandboxConfigError):
                SandboxLimits(**kwargs)


if __name__ == "__main__":
    unittest.main()
