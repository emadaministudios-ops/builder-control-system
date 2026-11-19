"""Tests for controller-owned workspace path confinement."""
from pathlib import Path
import tempfile
import unittest

from bcs.workspace import Workspace, WorkspaceError


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.root = self.base / "project"
        self.root.mkdir()
        (self.root / "src").mkdir()
        (self.root / "src" / "main.py").write_text("print('hello')", encoding="utf-8")
        self.workspace = Workspace("project-1", self.root)

    def test_root_is_canonical_and_controller_selected(self):
        self.assertEqual(self.workspace.root, self.root.resolve())
        self.assertEqual(self.workspace.workspace_id, "project-1")

    def test_resolves_normal_relative_path(self):
        self.assertEqual(
            self.workspace.resolve_path("src/main.py", must_exist=True),
            (self.root / "src" / "main.py").resolve(),
        )

    def test_rejects_traversal_and_absolute_paths(self):
        for path in ("../outside.txt", "src/../../outside.txt", "/etc/passwd", "C:\\Windows\\win.ini", "\\\\server\\share\\secret", "~/secret"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.resolve_path(path)

    def test_rejects_empty_malformed_and_nul_paths(self):
        for path in ("", "src//main.py", "./src/main.py", "src/./main.py", "bad\x00name"):
            with self.subTest(path=path), self.assertRaises(WorkspaceError):
                self.workspace.resolve_path(path)

    def test_rejects_symlink_escape(self):
        outside = self.base / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        link = self.root / "escape"
        try:
            link.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest("Symlink creation is not available on this platform.")
        with self.assertRaises(WorkspaceError):
            self.workspace.resolve_path("escape", must_exist=True)

    def test_reads_file_inside_workspace(self):
        self.assertEqual(self.workspace.read_text("src/main.py"), "print('hello')")

    def test_rejects_read_of_directory(self):
        with self.assertRaises(WorkspaceError):
            self.workspace.read_text("src")

    def test_lists_workspace_entries(self):
        self.assertEqual(self.workspace.list_entries(), ("src",))
        self.assertEqual(self.workspace.list_entries("src"), ("main.py",))

    def test_rejects_missing_root(self):
        with self.assertRaises(WorkspaceError):
            Workspace("missing", self.base / "does-not-exist")

    def test_rejects_file_as_root(self):
        file_path = self.base / "not-a-directory"
        file_path.write_text("x", encoding="utf-8")
        with self.assertRaises(WorkspaceError):
            Workspace("bad-root", file_path)


if __name__ == "__main__":
    unittest.main()
