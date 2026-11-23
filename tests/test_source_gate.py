"""Exercise immutable source admission for SDK-owned adapters."""

import subprocess
import tempfile
import unittest
from pathlib import Path

from sdk_tooling import source_gate


def git(root, *args):
    return subprocess.check_output(["git", "-C", root, *args], stderr=subprocess.PIPE)


class SourceGateTests(unittest.TestCase):
    def test_pinned_source_must_match_working_bytes_even_if_git_status_is_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            git(root, "init", "--quiet")
            module = root / "sdk_tooling" / "example.py"
            module.parent.mkdir()
            module.write_text("value = 1\n", encoding="utf-8")
            git(root, "add", "--", "sdk_tooling")
            git(
                root,
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Pin fixture",
            )
            pin = git(root, "rev-parse", "HEAD").decode().strip()
            _, inputs = source_gate.verify(root, pin, git)
            self.assertEqual(set(inputs), {"@sdk-tooling/sdk_tooling/example.py"})
            with self.assertRaisesRegex(ValueError, "must be a clean checkout"):
                source_gate.verify(root, "0" * 40, git)

            git(root, "update-index", "--assume-unchanged", "sdk_tooling/example.py")
            module.write_text("value = 2\n", encoding="utf-8")
            self.assertEqual(git(root, "status", "--porcelain=v1"), b"")
            with self.assertRaisesRegex(ValueError, "differs from its pinned bytes"):
                source_gate.verify(root, pin, git)
