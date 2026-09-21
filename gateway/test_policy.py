from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from policy import (
    CommandPolicyError,
    resolve_allowed_path,
    validate_exec_argv,
    validate_git_args,
    validate_systemd_action,
)


class PolicyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.root = self.base / "allowed"
        self.root.mkdir()
        self.outside = self.base / "outside"
        self.outside.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_read_path_inside_root(self) -> None:
        target = self.root / "x.txt"
        target.write_text("x", encoding="utf-8")
        self.assertEqual(resolve_allowed_path(str(target), (self.root,), must_exist=True), target.resolve())

    def test_read_path_outside_root_is_blocked(self) -> None:
        target = self.outside / "x.txt"
        target.write_text("x", encoding="utf-8")
        with self.assertRaises(PermissionError):
            resolve_allowed_path(str(target), (self.root,), must_exist=True)

    def test_existing_symlink_escape_is_blocked_for_write(self) -> None:
        outside_target = self.outside / "secret.txt"
        outside_target.write_text("secret", encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside_target)
        except OSError as exc:
            if getattr(exc, "winerror", None) == 1314:
                self.skipTest("Windows symlink privilege is not available")
            raise
        with self.assertRaises(PermissionError):
            resolve_allowed_path(str(link), (self.root,), must_exist=False)

    def test_new_file_inside_root_is_allowed(self) -> None:
        target = self.root / "new.txt"
        resolved = resolve_allowed_path(str(target), (self.root,), must_exist=False)
        self.assertEqual(resolved, target)

    def test_bash_command_string_is_blocked(self) -> None:
        with self.assertRaises(CommandPolicyError):
            validate_exec_argv(["bash", "-lc", "id"])

    def test_direct_python_is_allowed(self) -> None:
        validate_exec_argv(["python3", "-V"])

    def test_generic_exec_cannot_bypass_git_policy(self) -> None:
        with self.assertRaises(CommandPolicyError):
            validate_exec_argv(["git", "status"])

    def test_git_cannot_change_repository_path(self) -> None:
        with self.assertRaises(CommandPolicyError):
            validate_git_args(["-C", "/etc", "status"])

    def test_git_credential_override_is_blocked(self) -> None:
        with self.assertRaises(CommandPolicyError):
            validate_git_args(["-c", "credential.helper=!evil", "status"])

    def test_systemd_requires_exact_unit_allowlist(self) -> None:
        validate_systemd_action("restart", "content-agent.service", {"content-agent.service"})
        with self.assertRaises(PermissionError):
            validate_systemd_action("restart", "ssh.service", {"content-agent.service"})


if __name__ == "__main__":
    unittest.main()
