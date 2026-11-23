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

    def test_each_declared_project_runs_with_both_reported_dlls_and_arguments(self):
        (self.root / "sdk-checks.toml").write_text(
            '[windows_bindings]\npackages = ["recognition", "rendering"]\n'
            'projects = ["recognition.Tests.csproj", "rendering.Tests.csproj"]\n'
            'arguments = ["--native"]\n'
        )
        checked = []
        dotnet_path = self.root / "dotnet" / "dotnet.exe"

        def dotnet(command, *, env, **_):
            self.assertEqual(env["DOTNET_ROOT_X64"], str(dotnet_path.parent))
            if command[0] == dotnet_path:
                self.assertEqual(command[1], "build")
                (command[-1] / (command[2].stem + ".exe")).touch()
                return
            executable, *arguments = command
            self.assertEqual(arguments, ["--native"])
            for library in self.libraries:
                self.assertEqual(
                    (executable.parent / library.name).read_bytes(), library.read_bytes()
                )
            checked.append(executable)

        with (
            patch.object(
                windows_bindings, "select_dotnet_host", return_value=(dotnet_path, "x64", False)
            ),
            patch.object(windows_bindings, "_build_dlls", return_value=self.libraries) as build,
            patch.object(windows_bindings.dotnet, "run_command", side_effect=dotnet),
            patch.object(windows_bindings.windows, "require_pe_machine") as require_pe,
        ):
            windows_bindings._run(None, root=self.root)
        self.assertEqual(build.call_args.args[2], ("recognition", "rendering"))
        self.assertEqual(
            [executable.name for executable in checked],
            ["recognition.Tests.exe", "rendering.Tests.exe"],
        )
        self.assertEqual(
            [call.args for call in require_pe.call_args_list], [(p, "x64") for p in checked]
        )

    def test_uses_existing_binding_project_and_runs_through_dotnet_without_apphost(self):
        (self.root / "sdk-checks.toml").write_text(
            '[bindings]\ndotnet_project = "recognition.Tests.csproj"\n'
            '[windows_bindings]\npackages = ["recognition"]\n'
        )
        with (
            patch.object(
                windows_bindings, "select_dotnet_host", return_value=("dotnet", "x64", False)
            ),
            patch.object(windows_bindings, "_build_dlls", return_value=self.libraries[:1]),
            patch.object(windows_bindings.dotnet, "run_command") as run,
            patch.object(windows_bindings.windows, "require_pe_machine"),
        ):
            windows_bindings._run(None, root=self.root)
        build, launch = (call.args[0] for call in run.call_args_list)
        self.assertEqual(build[:3], ["dotnet", "build", self.projects[0]])
        self.assertEqual(len(launch), 2)
        self.assertEqual(launch[0], "dotnet")
        self.assertEqual(launch[1].name, "recognition.Tests.dll")
