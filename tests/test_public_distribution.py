"""Exercise the public CLI against an unrelated checkout."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from sdk_tooling.distribution import HOSTS

CLI = Path(__file__).resolve().parents[1] / "ci/run_sdk_tooling.py"


class PublicDistributionTests(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(work) / "Independent Example"
        self.root.mkdir()
        self.manifest = self.root / "release.toml"
        self.host = HOSTS[sys.platform]

    def invoke(self, *arguments, use_default_manifest=False):
        override = [] if use_default_manifest else ["--manifest", self.manifest.name]
        return subprocess.run(
            [
                sys.executable,
                "-I",
                "-B",
                CLI,
                "distribution",
                *arguments,
                "--root",
                self.root,
                *override,
            ],
            cwd=self.root.parent,
            capture_output=True,
            text=True,
        )

    def test_distribution_reads_default_and_explicit_checkout_manifests(self):
        commands = [["make", "check-distribution"]]
        for name, section in (
            ("sdk-distribution.toml", self.host),
            ("sdk-tooling.toml", f"distribution.{self.host}"),
        ):
            self.manifest = self.root / name
            self.manifest.write_text(f"[{section}]\ncommands = {json.dumps(commands)}\n")
            for use_default_manifest in (True, False):
                with self.subTest(name=name, use_default_manifest=use_default_manifest):
                    plan = self.invoke("plan", use_default_manifest=use_default_manifest)
                    self.assertEqual(plan.returncode, 0, plan.stderr)
                    self.assertEqual(json.loads(plan.stdout), {self.host: commands})
            self.manifest.unlink()

    def test_external_check_preserves_literal_argument_and_disposable_output(self):
        self.manifest.write_text(
            f'[{self.host}]\ncommands = [["python", "verify.py", "{{temp}}/output name", "$literal"]]\n'
        )
        (self.root / "verify.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "result = Path(sys.argv[1])\n"
            "result.write_text(sys.argv[2])\n"
            "assert result.name == 'output name' and result.read_text() == '$literal'\n"
            "print(result)\n"
        )
        plan = self.invoke("plan")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(json.loads(plan.stdout)[self.host][0][-1], "$literal")
        checked = self.invoke("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        output = Path(checked.stdout.splitlines()[-1])
        self.assertEqual(output.name, "output name")
        self.assertFalse(output.parent.exists())

    def test_outside_packer_fails_before_any_command_runs(self):
        outside = self.root.parent / "pack.py"
        outside.write_text("raise AssertionError('must not run')\n")
        self.manifest.write_text(
            f'[{self.host}]\ncommands = [["python", "../pack.py"]]\n[nuget]\npack = "../pack.py"\n'
        )
        checked = self.invoke("check")
        self.assertNotEqual(checked.returncode, 0)
        self.assertIn("inside the checkout", checked.stderr)
        self.assertNotIn("must not run", checked.stderr)
