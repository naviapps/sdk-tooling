"""Reject incorrect worker state before trusting a prepared launcher."""

import hashlib
import io
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import prepared_mise, quality


class PreparedMiseTests(unittest.TestCase):
    def setUp(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.manifest = root / "manifest.json"
        self.manifest.write_bytes(b"{}\n")
        self.launcher = root / "mise"
        self.launcher.write_bytes(b"fixture launcher")
        self.launcher.chmod(0o700)
        self.enterContext(patch.dict(os.environ, {"NAVI_CI_TOOLS_ROOT": str(root)}, clear=True))
        self.enterContext(
            patch.multiple(
                prepared_mise,
                LAUNCHER=self.launcher,
                LAUNCHER_SHA256=hashlib.sha256(self.launcher.read_bytes()).hexdigest(),
                LEGACY_MANIFEST_SHA256=hashlib.sha256(self.manifest.read_bytes()).hexdigest(),
            )
        )
        self.stdout = self.enterContext(redirect_stdout(io.StringIO()))
        self.stderr = self.enterContext(redirect_stderr(io.StringIO()))

    def test_operation_prints_only_attested_launcher_path(self):
        probe = subprocess.CompletedProcess([], 0, stdout=f"{prepared_mise.VERSION}\n".encode())
        with patch.object(prepared_mise.subprocess, "run", return_value=probe) as run:
            quality.main(["prepared-mise"])
        run.assert_called_once_with(
            [self.launcher, "--version"],
            cwd=self.manifest.parent,
            capture_output=True,
            check=True,
            timeout=15,
        )
        self.assertEqual(self.stdout.getvalue(), str(self.launcher) + "\n")
        self.assertIn("Verified prepared mise", self.stderr.getvalue())

    def test_controller_digest_can_attest_a_rebuilt_guest(self):
        self.manifest.write_bytes(b'{"schema": 2}\n')
        digest = hashlib.sha256(self.manifest.read_bytes()).hexdigest()
        probe = subprocess.CompletedProcess([], 0, stdout=f"{prepared_mise.VERSION}\n".encode())
        with (
            patch.dict(os.environ, {"NAVI_CI_PREPARED_MANIFEST_SHA256": digest}),
            patch.object(prepared_mise.subprocess, "run", return_value=probe) as run,
        ):
            quality.main(["prepared-mise"])
        run.assert_called_once()
        self.assertEqual(self.stdout.getvalue(), str(self.launcher) + "\n")

    def test_explicit_bad_digest_never_falls_back_to_legacy(self):
        legacy = prepared_mise.LEGACY_MANIFEST_SHA256
        with patch.object(prepared_mise.subprocess, "run") as run:
            for digest in ("", legacy.upper(), legacy + "\n", legacy[:-1], "0" * 64):
                with (
                    self.subTest(digest=digest),
                    patch.dict(os.environ, {"NAVI_CI_PREPARED_MANIFEST_SHA256": digest}),
                    self.assertRaisesRegex(SystemExit, "does not match the attested"),
                ):
                    quality.main(["prepared-mise"])
        run.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")

    def test_modified_manifest_or_launcher_is_rejected_before_execution(self):
        with patch.object(prepared_mise.subprocess, "run") as run:
            for path in (self.manifest, self.launcher):
                with self.subTest(path=path.name):
                    before = path.read_bytes()
                    path.write_bytes(before + b"changed")
                    with self.assertRaisesRegex(SystemExit, "does not match the attested"):
                        quality.main(["prepared-mise"])
                    path.write_bytes(before)
        run.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")

    def test_credentials_block_launcher_before_execution(self):
        with patch.object(prepared_mise.subprocess, "run") as run:
            for name in ("GH_TOKEN", "GITHUB_TOKEN", "MISE_GITHUB_TOKEN"):
                with (
                    self.subTest(name=name),
                    patch.dict(os.environ, {name: "synthetic"}),
                    self.assertRaisesRegex(SystemExit, "without GitHub credentials"),
                ):
                    quality.main(["prepared-mise"])
        run.assert_not_called()
        self.assertEqual(self.stdout.getvalue(), "")

    def test_unexpected_version_is_not_returned_as_attested(self):
        probe = subprocess.CompletedProcess([], 0, stdout=b"unexpected version\n")
        with (
            patch.object(prepared_mise.subprocess, "run", return_value=probe),
            self.assertRaisesRegex(SystemExit, "does not match the attested"),
        ):
            quality.main(["prepared-mise"])
        self.assertEqual(self.stdout.getvalue(), "")
