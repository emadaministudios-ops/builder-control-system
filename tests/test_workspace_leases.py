"""Tests for exclusive workspace-root leases."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from bcs.workspace_leases import WorkspaceLeaseManager


class WorkspaceLeaseManagerTests(unittest.TestCase):
    def test_same_canonical_root_shares_one_exclusive_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manager = WorkspaceLeaseManager()
            first = manager._lock_for(root)
            second = manager._lock_for(root / ".")
            self.assertIs(first, second)
            self.assertTrue(first.acquire(blocking=False))
            try:
                self.assertFalse(second.acquire(blocking=False))
            finally:
                first.release()

    def test_distinct_worktree_roots_have_independent_locks(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            first_root = base / "worktree-a"
            second_root = base / "worktree-b"
            first_root.mkdir()
            second_root.mkdir()
            manager = WorkspaceLeaseManager()
            first = manager._lock_for(first_root)
            second = manager._lock_for(second_root)
            self.assertIsNot(first, second)
            self.assertTrue(first.acquire(blocking=False))
            try:
                self.assertTrue(second.acquire(blocking=False))
                second.release()
            finally:
                first.release()

    def test_lease_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = WorkspaceLeaseManager()
            with self.assertRaisesRegex(RuntimeError, "simulated"):
                with manager.lease(directory):
                    raise RuntimeError("simulated")
            lock = manager._lock_for(directory)
            self.assertTrue(lock.acquire(blocking=False))
            lock.release()


if __name__ == "__main__":
    unittest.main()
