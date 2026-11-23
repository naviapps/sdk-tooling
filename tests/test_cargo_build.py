import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cargo_build_fixtures import FIXTURE_PACKAGE, FIXTURE_UNIT, fixture_dependencies

from sdk_tooling.cargo_build import compile_units, verify_units

TARGET = "x86_64-pc-windows-msvc"


class CargoBuildTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.target_dir = self.root / "target"
        self.library = self.target_dir / TARGET / "release/fixture.dll"
        self.env = {"CARGO_TARGET_DIR": str(self.target_dir)}
        self.identity = self.root.as_uri() + "#fixture-ffi@0.1.0"
        self.manifest = str(self.root / "Cargo.toml")
        self.graph = SimpleNamespace(
            root=self.root,
            roots=["fixture-ffi"],
            packages=[
                {
                    **FIXTURE_PACKAGE,
                    "source": None,
                    "id": self.identity,
                    "manifest_path": self.manifest,
                }
            ],
        )

    def artifact(self, path, *, features=(), kind="cdylib", **values):
        return {
            "reason": "compiler-artifact",
            "package_id": self.identity,
            "manifest_path": self.manifest,
            "target": {"name": "fixture", "kind": [kind], "crate_types": [kind]},
            "filenames": [str(path)],
            "features": list(features),
        } | values

    def compile(self, artifacts, *, complete=True):
        messages = list(artifacts)
        if complete:
            messages.append({"reason": "build-finished", "success": True})
        command = ["cargo", "build", "--message-format=json-render-diagnostics"]
        output = "\n".join(map(json.dumps, messages)).encode("utf-8")
        with patch(
            "sdk_tooling.cargo_build.subprocess.run", return_value=SimpleNamespace(stdout=output)
        ) as run:
            units = compile_units(
                command, graph=self.graph, target=TARGET, library=self.library, env=self.env
            )
        run.assert_called_once_with(
            command, cwd=self.root, env=self.env, stdout=subprocess.PIPE, check=True
        )
        return units

    def test_records_same_package_feature_sets_on_host_and_target(self):
        host = self.target_dir / "release/deps/fixture.rlib"
        target = self.target_dir / TARGET / "release/deps/fixture.rlib"
        artifacts = [
            self.artifact(host, features=["build"], kind="lib", fresh=False),
            self.artifact(target, features=["runtime"], kind="lib", fresh=True),
            self.artifact(self.library),
        ]
        units = self.compile(artifacts)
        self.assertEqual(units, self.compile(reversed(artifacts)))
        self.assertEqual(
            [(unit["compilation"], unit["kind"], unit["features"]) for unit in units],
            [
                ("target", ["cdylib"], []),
                ("host", ["lib"], ["build"]),
                ("target", ["lib"], ["runtime"]),
            ],
        )
        self.assertNotIn(str(self.root), json.dumps(units))
        dependencies = fixture_dependencies(TARGET)
        dependencies["packages"][0]["features"][TARGET] = ["build", "runtime", "workspace-only"]
        verify_units({TARGET: units}, dependencies, [TARGET], "fixture-ffi", "fixture")

    def test_requires_exact_package_and_named_output_from_successful_build(self):
        cases = [
            (self.artifact(self.library, package_id="wrong"), "verified dependency"),
            (self.artifact(self.library, manifest_path="wrong"), "verified dependency"),
            (self.artifact(self.root / self.library.name), "compilation directory"),
            (self.artifact(Path("fixture.dll")), "absolute paths"),
            (self.artifact(self.library.with_name("different.dll")), "selected native library"),
        ]
        for artifact, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                self.compile([artifact])
        with self.assertRaisesRegex(ValueError, "selected native library"):
            self.compile([self.artifact(self.library)], complete=False)

    def test_receipt_rejects_missing_unresolved_or_inconsistent_units(self):
        cases = [
            ({}, "selected targets"),
            ({TARGET: []}, "no compiled units"),
            ({TARGET: [dict(FIXTURE_UNIT, name="unknown")]}, "dependency graph"),
            ({TARGET: [dict(FIXTURE_UNIT, features=["unknown"])]}, "dependency graph"),
            (
                {TARGET: [dict(FIXTURE_UNIT, targetName="/private/artifact")]},
                "invalid compiler artifact",
            ),
            ({TARGET: [dict(FIXTURE_UNIT, compilation="host")]}, "ordered native build"),
            ({TARGET: [FIXTURE_UNIT, FIXTURE_UNIT]}, "ordered native build"),
        ]
        for receipt, error in cases:
            with self.subTest(error=error), self.assertRaisesRegex(ValueError, error):
                verify_units(
                    receipt, fixture_dependencies(TARGET), [TARGET], "fixture-ffi", "fixture"
                )
