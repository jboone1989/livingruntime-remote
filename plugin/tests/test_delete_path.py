from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
os.environ.setdefault("LIVINGRUNTIME_REMOTE_CONFIG", str(SCRIPTS / "missing-test-config.json"))

import bridge
import filesystem_delete
import localexec


@unittest.skipUnless(os.name == "posix", "POSIX no-follow directory handles required")
class FilesystemDeletionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.work = self.root / "workspace"
        self.work.mkdir()
        self.outside = self.root / "outside"
        self.outside.mkdir()
        self.keep = self.outside / "keep"
        self.keep.write_text("valuable")

    def tearDown(self):
        self.temp.cleanup()

    def call(self, path, **kwargs):
        request = {"op": "delete_path", "path": str(path), "roots": [str(self.work)],
                   "recursive": False, "dry_run": False, "protected_paths": [],
                   "delete_worker_source": filesystem_delete.DELETE_WORKER_SOURCE, **kwargs}
        return localexec.handle(request)

    def test_file_deletion_and_truthful_count(self):
        target = self.work / "trash"
        target.write_bytes(b"abc")
        result = self.call(target)
        self.assertEqual(result["status"], "DELETED")
        self.assertEqual(result["deleted_entries"], 1)
        self.assertEqual(result["deleted_bytes"], 3)
        self.assertFalse(target.exists())

    def test_recursive_deletion_counts_children(self):
        target = self.work / "old"
        (target / "nested").mkdir(parents=True)
        (target / "first").write_bytes(b"abc")
        (target / "nested" / "second").write_bytes(b"xyzq")
        result = self.call(target, recursive=True)
        self.assertEqual(result["deleted_entries"], 4)
        self.assertEqual(result["deleted_bytes"], 7)
        self.assertFalse(target.exists())

    def test_directory_requires_explicit_recursive(self):
        target = self.work / "old"
        target.mkdir()
        with self.assertRaises(Exception):
            self.call(target)
        self.assertTrue(target.is_dir())

    def test_dry_run_preserves_contents(self):
        target = self.work / "old"
        target.mkdir()
        (target / "file").write_bytes(b"abc")
        result = self.call(target, recursive=True, dry_run=True)
        self.assertEqual(result["status"], "DRY_RUN")
        self.assertEqual(result["planned_entries"], 2)
        self.assertEqual(result["deleted_entries"], 0)
        self.assertEqual((target / "file").read_bytes(), b"abc")

    def test_matching_sha_deletes_regular_file(self):
        target = self.work / "file"
        target.write_bytes(b"abc")
        digest = hashlib.sha256(b"abc").hexdigest()
        result = self.call(target, expected_sha256=digest)
        self.assertEqual(result["sha256"], digest)
        self.assertFalse(target.exists())

    def test_stale_sha_preserves_file(self):
        target = self.work / "file"
        target.write_bytes(b"new")
        with self.assertRaises(Exception):
            self.call(target, expected_sha256=hashlib.sha256(b"old").hexdigest())
        self.assertEqual(target.read_bytes(), b"new")

    def test_bad_sha_is_rejected(self):
        target = self.work / "file"
        target.write_text("keep")
        with self.assertRaises(Exception):
            self.call(target, expected_sha256="wrong")
        self.assertTrue(target.exists())

    def test_hash_guard_cannot_be_used_for_directory_or_symlink(self):
        target = self.work / "old"
        target.mkdir()
        link = self.work / "link"
        link.symlink_to(self.keep)
        for candidate in (target, link):
            with self.assertRaises(Exception):
                self.call(candidate, recursive=True, expected_sha256="a" * 64)
        self.assertTrue(target.is_dir())
        self.assertTrue(link.is_symlink())

    def test_missing_path_is_idempotent(self):
        result = self.call(self.work / "absent")
        self.assertEqual(result["status"], "NOT_FOUND")
        self.assertFalse(result["deleted"])

    def test_missing_parent_is_idempotent(self):
        result = self.call(self.work / "missing-parent" / "file")
        self.assertEqual(result["status"], "NOT_FOUND")

    def test_hash_guard_missing_file_is_conflict(self):
        with self.assertRaises(Exception):
            self.call(self.work / "absent", expected_sha256="a" * 64)

    def test_outside_root_is_rejected(self):
        with self.assertRaises(Exception):
            self.call(self.keep)
        self.assertEqual(self.keep.read_text(), "valuable")

    def test_configured_root_and_ancestor_are_rejected(self):
        for target in (self.work, self.root, Path("/")):
            with self.assertRaises(Exception):
                self.call(target, recursive=True)
        self.assertTrue(self.work.is_dir())

    def test_project_root_is_protected_but_child_can_be_deleted(self):
        project = self.work / "project"
        project.mkdir()
        target = project / "trash"
        target.write_text("old")
        with self.assertRaises(Exception):
            self.call(project, recursive=True, protected_paths=[str(project)])
        self.assertTrue(target.exists())
        result = self.call(target, protected_paths=[str(project)])
        self.assertTrue(result["deleted"])

    def test_home_connector_and_credentials_are_protected(self):
        for name in (".ssh", ".livingruntime", ".codex", ".gnupg", ".config"):
            with self.assertRaises(Exception):
                self.call(Path.home() / name, roots=[str(Path.home())], recursive=True)

    def test_parent_traversal_is_rejected(self):
        with self.assertRaises(Exception):
            self.call(str(self.work) + "/../outside/keep")
        self.assertTrue(self.keep.exists())

    def test_parent_symlink_cannot_escape_root(self):
        parent = self.work / "alias"
        parent.symlink_to(self.outside, target_is_directory=True)
        with self.assertRaises(Exception):
            self.call(parent / "keep")
        self.assertTrue(self.keep.exists())
        self.assertTrue(parent.is_symlink())

    def test_leaf_symlink_is_unlinked_without_touching_target(self):
        target = self.work / "link"
        target.symlink_to(self.keep)
        result = self.call(target, recursive=True)
        self.assertEqual(result["deleted_entries"], 1)
        self.assertEqual(result["deleted_bytes"], 0)
        self.assertFalse(target.is_symlink())
        self.assertEqual(self.keep.read_text(), "valuable")

    def test_dangling_symlink_can_be_deleted(self):
        target = self.work / "link"
        target.symlink_to(self.outside / "missing")
        self.assertTrue(self.call(target)["deleted"])
        self.assertFalse(target.is_symlink())

    def test_recursive_deletion_does_not_follow_symlink(self):
        target = self.work / "old"
        target.mkdir()
        (target / "link").symlink_to(self.outside, target_is_directory=True)
        self.assertTrue(self.call(target, recursive=True)["deleted"])
        self.assertEqual(self.keep.read_text(), "valuable")

    def test_special_file_blocks_entire_tree_before_mutation(self):
        target = self.work / "old"
        target.mkdir()
        (target / "a").write_text("keep")
        os.mkfifo(target / "z")
        with self.assertRaises(Exception):
            self.call(target, recursive=True)
        self.assertEqual((target / "a").read_text(), "keep")


class SshWorkerDeletionTests(FilesystemDeletionTests):
    def call(self, path, **kwargs):
        request = {"op": "delete_path", "path": str(path), "roots": [str(self.work)],
                   "recursive": False, "dry_run": False, "protected_paths": [],
                   "delete_worker_source": filesystem_delete.DELETE_WORKER_SOURCE, **kwargs}
        process = subprocess.run([sys.executable, "-c", bridge._REMOTE_AGENT],
                                 input=json.dumps(request), text=True, capture_output=True, timeout=10)
        if process.returncode:
            raise RuntimeError(process.stderr)
        return json.loads(process.stdout)


@unittest.skipUnless(os.name == "posix", "POSIX directory handles required")
class DeletionFailureTests(FilesystemDeletionTests):
    def test_byte_limit_rejection_preserves_all_files(self):
        target = self.work / "file"
        target.write_bytes(b"abc")
        with patch.object(filesystem_delete, "MAX_BYTES", 2):
            with self.assertRaisesRegex(ValueError, "bounded deletion limits"):
                self.call(target)
        self.assertEqual(target.read_bytes(), b"abc")

    def test_depth_limit_rejection_preserves_tree(self):
        target = self.work / "old"
        (target / "a" / "b").mkdir(parents=True)
        with patch.object(filesystem_delete, "MAX_DEPTH", 2):
            with self.assertRaisesRegex(ValueError, "depth"):
                self.call(target, recursive=True)
        self.assertTrue((target / "a" / "b").exists())

    def test_other_filesystem_is_rejected_before_mutation(self):
        target = self.work / "file"
        target.write_text("keep")
        original = os.stat
        def mounted(name, *args, **kwargs):
            info = original(name, *args, **kwargs)
            if name == "file" and "dir_fd" in kwargs:
                fields = list(info)
                fields[2] = info.st_dev + 1
                return os.stat_result(fields)
            return info
        with patch.object(filesystem_delete.os, "stat", mounted):
            with self.assertRaisesRegex(PermissionError, "filesystem mount"):
                self.call(target)
        self.assertTrue(target.exists())

    def test_limit_rejection_precedes_all_mutation(self):
        target = self.work / "old"
        target.mkdir()
        for name in ("a", "b", "c"):
            (target / name).write_text("keep")
        with patch.object(filesystem_delete, "MAX_ENTRIES", 2):
            with self.assertRaisesRegex(ValueError, "bounded deletion limits"):
                self.call(target, recursive=True)
        self.assertEqual(len(list(target.iterdir())), 3)

    def test_partial_failure_returns_actual_progress(self):
        target = self.work / "old"
        target.mkdir()
        for name in ("a", "b"):
            (target / name).write_text("keep")
        original = os.unlink
        def fail_second(name, *args, **kwargs):
            if name == "b":
                raise PermissionError("simulated permission failure")
            return original(name, *args, **kwargs)
        with patch.object(filesystem_delete.os, "unlink", fail_second):
            result = self.call(target, recursive=True)
        self.assertEqual(result["status"], "PARTIAL_FAILURE")
        self.assertEqual(result["deleted_entries"], 1)
        self.assertEqual(result["deleted_bytes"], 4)
        self.assertFalse(result["deleted"])
        self.assertTrue((target / "b").exists())

    def test_changed_entry_is_preserved(self):
        target = self.work / "file"
        target.write_text("keep")
        original = os.stat
        count = 0
        def changed(name, *args, **kwargs):
            nonlocal count
            if name == "file":
                count += 1
                if count == 3:
                    target.write_text("new content")
            return original(name, *args, **kwargs)
        with patch.object(filesystem_delete.os, "stat", changed):
            result = self.call(target)
        self.assertEqual(result["status"], "PARTIAL_FAILURE")
        self.assertEqual(result["deleted_entries"], 0)
        self.assertEqual(target.read_text(), "new content")

    def test_unsupported_platform_fails_closed(self):
        target = self.work / "file"
        target.write_text("keep")
        with patch.object(filesystem_delete.os, "name", "nt"):
            with self.assertRaisesRegex(NotImplementedError, "POSIX no-follow"):
                filesystem_delete.delete_path(str(target), [str(self.work)])
        self.assertTrue(target.exists())


class DeletePathBridgeTests(unittest.TestCase):
    def test_audit_success_and_failure_and_host_routing(self):
        config = {"default_host": "main", "hosts": {"main": {
            "ssh_host": "test-host", "roots": ["/tmp"], "units": []}},
            "projects": {"app": {"host": "main", "path": "/tmp/app"}}}
        success = {"path": "/tmp/app/trash", "status": "DELETED",
                   "deleted": True, "deleted_entries": 1, "deleted_bytes": 3}
        with patch.object(bridge, "_config", return_value=bridge.normalize(config)), \
             patch.object(bridge, "_remote", return_value=success) as remote, \
             patch.object(bridge, "_audit") as audit:
            self.assertEqual(bridge.delete_path("trash", project="app", device="main"), success)
            self.assertEqual(remote.call_args.args[0], "delete_path")
            self.assertEqual(remote.call_args.args[1]["path"], "/tmp/app/trash")
            self.assertEqual(remote.call_args.args[1]["protected_paths"], ["/tmp/app"])
            self.assertTrue(audit.call_args.args[1])
            remote.side_effect = PermissionError("not allowed")
            with self.assertRaises(PermissionError):
                bridge.delete_path("trash", project="app", device="main")
            self.assertFalse(audit.call_args.args[1])
        with patch.object(bridge, "_config", return_value=bridge.normalize(config)), \
             patch.object(bridge, "_remote") as remote, patch.object(bridge, "_audit"):
            with self.assertRaises(RuntimeError):
                bridge.delete_path("trash", project="app", device="other")
            remote.assert_not_called()


if __name__ == "__main__":
    unittest.main()
