"""Check source identity and credential boundaries for SDK distribution."""

import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/check-sdk-distribution.py"
DISPATCH = runpy.run_path(str(SCRIPT))


class DistributionTests(unittest.TestCase):
    def test_sdk_child_identity_replaces_workflow_sha_without_credentials(self):
        environment = {
            "NAVI_CI_SOURCE_COMMIT": "a" * 40,
            "NAVI_CI_SOURCE_FINGERPRINT": "b" * 64,
            "NAVI_CI_SOURCES": "manifest.json",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_TOKEN": "test token",
            "GH_TOKEN": "test token",
            "ACTIONS_RUNTIME_TOKEN": "test token",
            "MISE_GITHUB_TOKEN": "test token",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "unrelated authorization header",
            "GIT_CONFIG_VALUE_1": "unrelated credential helper",
        }
        with patch.dict(os.environ, environment, clear=True):
            actual = DISPATCH["child_environment"]("c" * 40, SCRIPT.parent)
        self.assertEqual(actual["GITHUB_SHA"], "c" * 40)
        self.assertEqual(actual["NAVI_SDK_TOOLING_SOURCE"], str(SCRIPT.parent))
        self.assertEqual(actual["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(actual["GIT_CONFIG_VALUE_0"], "")
        self.assertFalse(
            (environment.keys() - {"GITHUB_SHA", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"})
            & actual.keys()
        )

    def test_pin_is_one_full_tooling_commit(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            manifest = root / "tools/tooling-sources.json"
            manifest.parent.mkdir()
            repository = DISPATCH["REPOSITORY"]
            expected = "a" * 40
            manifest.write_text(json.dumps({repository: expected}), encoding="utf-8")
            self.assertEqual(DISPATCH["tooling_revision"](root), expected)
            for invalid in (
                {repository: "main"},
                {repository: expected, "naviapps/other": expected},
            ):
                with self.subTest(invalid=invalid):
                    manifest.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "one full lowercase Git SHA"):
                        DISPATCH["tooling_revision"](root)

    def test_sdk_checkout_rejects_off_main_commit(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable).resolve()
            repository = "naviapps/preset-documents"

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Distribution Test",
                        "-c",
                        "user.email=distribution@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        f"core.hooksPath={os.devnull}",
                        *args,
                    ],
                    cwd=root,
                    text=True,
                ).strip()

            git("init", "--quiet", "--initial-branch=main")
            git("remote", "add", "origin", f"https://github.com/{repository}")
            git("commit", "--quiet", "--allow-empty", "-m", "Public main")
            stable = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", stable)
            verify = DISPATCH["verify_checkout"]
            verify(root, stable, repository, os.environ, require_main=True)
            git("commit", "--quiet", "--allow-empty", "-m", "Unreviewed change")
            with self.assertRaises(subprocess.CalledProcessError):
                verify(root, git("rev-parse", "HEAD"), repository, os.environ, require_main=True)


if __name__ == "__main__":
    unittest.main()
