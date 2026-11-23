import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sdk_tooling import windows_bindings


class WindowsBindingTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.libraries = []
        self.packages = []
        self.projects = []
        for name in ("recognition", "rendering"):
            manifest = self.root / name / "Cargo.toml"
            self.packages.append(
                {"id": f"workspace:{name}", "name": name, "manifest_path": str(manifest)}
            )
            library = self.root / "custom-target" / f"{name}.dll"
            library.parent.mkdir(exist_ok=True)
            library.write_bytes(name.encode())
            self.libraries.append(library)
            project = self.root / f"{name}.Tests.csproj"
            project.touch()
            self.projects.append(project)

    def artifacts(self):
        return [
            {
                "reason": "compiler-artifact",
                "package_id": package["id"],
                "manifest_path": package["manifest_path"],
                "target": {"crate_types": ["cdylib"]},
                "filenames": [str(library), str(library.with_suffix(".dll.lib"))],
                "fresh": True,
            }
            for package, library in zip(self.packages, self.libraries, strict=True)
        ]

    def build(self, messages):
        metadata = json.dumps(
            {"workspace_members": [p["id"] for p in self.packages], "packages": self.packages}
        )
        output = "\n".join(
            map(json.dumps, [*messages, {"reason": "build-finished", "success": True}])
        ).encode()
        env = {"CARGO_TARGET_DIR": str(self.root / "custom-target")}
        with (
            patch.object(windows_bindings.subprocess, "check_output", return_value=metadata),
            patch.object(
                windows_bindings.subprocess, "run", return_value=SimpleNamespace(stdout=output)
            ) as run,
            patch.object(windows_bindings.windows, "require_pe_machine") as require_pe,
        ):
            libraries = windows_bindings._build_dlls(self.root, env, ("recognition", "rendering"))
            self.assertEqual(libraries, tuple(self.libraries))
        self.assertEqual(
            [call.args for call in require_pe.call_args_list], [(p, "x64") for p in libraries]
        )
        self.assertIn("x86_64-pc-windows-msvc", run.call_args.args[0])
        self.assertIn("--message-format=json-render-diagnostics", run.call_args.args[0])

    def test_uses_cargo_output_paths_and_ignores_other_artifacts(self):
        artifacts = self.artifacts()
        dependency = dict(artifacts[0], package_id="dependency:recognition")
        self.build([dependency, *artifacts])

    def test_rejects_missing_or_ambiguous_dlls(self):
        first, second = self.artifacts()
        with self.assertRaisesRegex(ValueError, "each Windows fixture package"):
            self.build([first])
        with self.assertRaisesRegex(ValueError, "one Cargo-reported DLL"):
            self.build([first, second, second])

    def test_each_declared_project_runs_with_both_reported_dlls(self):
        (self.root / "sdk-checks.toml").write_text(
            '[windows_bindings]\npackages = ["recognition", "rendering"]\n'
            'projects = ["recognition.Tests.csproj", "rendering.Tests.csproj"]\n'
        )
        args = SimpleNamespace(package=[], project=[], native_property=None, argument=[])
        checked = []

        def dotnet(command, **_):
            if command[1] == "build":
                return
            assembly = command[1]
            for library in self.libraries:
                self.assertEqual(
                    (assembly.parent / library.name).read_bytes(), library.read_bytes()
                )
            checked.append(assembly.name)

        with (
            patch.object(
                windows_bindings, "select_dotnet_host", return_value=("dotnet", "x64", False)
            ),
            patch.object(windows_bindings, "_build_dlls", return_value=self.libraries) as build,
            patch.object(windows_bindings.dotnet, "run_command", side_effect=dotnet),
        ):
            windows_bindings._run(args, root=self.root)
        self.assertEqual(build.call_args.args[2], ("recognition", "rendering"))
        self.assertEqual(checked, ["recognition.Tests.dll", "rendering.Tests.dll"])

    def test_legacy_action_arguments_still_select_a_native_msbuild_property(self):
        args = SimpleNamespace(
            package=["recognition"],
            project=["recognition.Tests.csproj"],
            native_property="FixtureNativeLibrary",
            argument=["--native"],
        )
        with (
            patch.object(
                windows_bindings, "select_dotnet_host", return_value=("dotnet", "x64", False)
            ),
            patch.object(windows_bindings, "_build_dlls", return_value=self.libraries[:1]),
            patch.object(windows_bindings.dotnet, "run_managed") as run,
        ):
            windows_bindings._run(args, root=self.root)
        self.assertEqual(run.call_args.args, ("dotnet", self.projects[0]))
        self.assertEqual(
            run.call_args.kwargs["properties"], {"FixtureNativeLibrary": self.libraries[0]}
        )
        self.assertEqual(run.call_args.kwargs["arguments"], ["--native"])
