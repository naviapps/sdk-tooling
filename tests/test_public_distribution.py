"""Exercise the public CLI against an unrelated checkout."""

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import DEFAULT, patch

from sdk_tooling import distribution, workspaces

CLI = Path(__file__).resolve().parents[1] / "ci/run_sdk_tooling.py"


class PublicDistributionTests(unittest.TestCase):
    def setUp(self):
        work = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(work) / "Independent Example"
        self.root.mkdir()
        self.manifest = self.root / "release.toml"
        self.host = distribution.HOSTS[sys.platform]

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
        commands = [
            [
                "python",
                "verify.py",
                "{temp}/output name",
                "$literal",
                " one argument ",
                "{tooling}/sdk_tooling/adapters.py",
            ]
        ]
        self.manifest.write_text(
            f"[{self.host}]\ncommands = {json.dumps(commands)}\n", encoding="utf-8"
        )
        (self.root / "verify.py").write_text(
            "import sys\nfrom pathlib import Path\n"
            "from sdk_tooling import adapters\n"
            "result = Path(sys.argv[1])\n"
            "result.write_text(sys.argv[2])\n"
            "assert result.name == 'output name' and result.read_text() == '$literal'\n"
            "assert sys.argv[3] == ' one argument '\n"
            "assert Path(sys.argv[4]).samefile(adapters.__file__)\n"
            "print(result)\n",
            encoding="utf-8",
        )
        plan = self.invoke("plan")
        self.assertEqual(plan.returncode, 0, plan.stderr)
        self.assertEqual(json.loads(plan.stdout), {self.host: commands})
        checked = self.invoke("check")
        self.assertEqual(checked.returncode, 0, checked.stderr)
        output = Path(checked.stdout.splitlines()[-1])
        self.assertEqual(output.name, "output name")
        self.assertFalse(output.parent.exists())

    def test_disposable_command_output_is_removed_after_a_windows_file_lock(self):
        error = PermissionError("Command output is still locked")
        error.winerror = 32

        def run(command, *, cwd, check):
            path = Path(command[1])
            path.write_text("output", encoding="utf-8")

        with (
            patch.object(distribution.subprocess, "run", side_effect=run) as command,
            patch.object(
                tempfile.TemporaryDirectory,
                "_rmtree",
                wraps=tempfile.TemporaryDirectory._rmtree,
                side_effect=[error, DEFAULT],
            ) as cleanup,
            patch.object(workspaces.time, "sleep") as sleep,
        ):
            distribution.execute(
                {self.host: [["sdk-check", "{temp}/result"]]}, self.root, self.host
            )
        output = Path(command.call_args.args[0][1])
        self.assertFalse(output.parent.exists())
        self.assertEqual(cleanup.call_count, 2)
        sleep.assert_called_once_with(0.1)

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
