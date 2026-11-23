"""Managed semantic gates with evaluated references and injected commands."""

import json
import struct
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import DEFAULT, patch

from sdk_tooling import bindings, consumers, workspaces


def write_pe(path, machine):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ" + bytes(0x3A) + struct.pack("<I4sH", 0x40, b"PE\0\0", machine))


class ManagedQualityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="managed-quality-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.root / "Entry.csproj"
        self.library = self.root / "selected core/Library.csproj"
        self.env = {"PATH": "owned-tools", "configuration": "Debug", "NATIVE_LIBRARY": "owned"}
        self.calls = []
        self.references = {self.project: [self.library, self.library]}
        self.failure = None
        self.override = None
        self.workspace_warning = False
        self.reference_metadata = {}
        guard = patch.object(
            bindings.subprocess, "run", side_effect=AssertionError("Inject external commands")
        )
        guard.start()
        self.addCleanup(guard.stop)

    def runner(self, argv, **options):
        self.calls.append((argv, options))
        self.assertTrue(options["check"])
        self.assertNotIn("shell", options)
        if self.failure and self.failure(argv):
            raise subprocess.CalledProcessError(2, argv)
        output = ""
        if argv[1] == "msbuild":
            names = next(value for value in argv if value.startswith("-getProperty:"))
            selected = {name: options["env"][name] for name in names.split(":", 1)[1].split(",")}
            if self.override:
                selected.update(self.override)
            output = json.dumps(
                {
                    "Properties": selected,
                    "Items": {
                        "ProjectReference": [
                            {"FullPath": str(path), **self.reference_metadata}
                            for path in self.references.get(Path(argv[2]), [])
                        ]
                    },
                }
            )
        elif argv[1] == "format" and self.workspace_warning:
            output = "Warnings were encountered while loading the workspace."
        return subprocess.CompletedProcess(argv, 0, stdout=output, stderr="")

    def test_run_verifies_selected_graph_after_restore_and_preserves_runtime_arguments(self):
        properties = {
            "CoreProject": str(self.library),
            "NativeRid": "win-arm64",
            "UseSharedCompilation": "false",
        }
        original = dict(properties)
        bindings.run_managed(
            "owned-dotnet",
            self.project,
            cwd=self.root,
            env=self.env,
            nuget_config=self.root / "NuGet.Config",
            sources=(self.root / "local feed",),
            runtime="win-arm64",
            configuration="Checked",
            properties=properties,
            arguments=("--fixture", "literal & fixture"),
            verify_style=True,
            runner=self.runner,
        )
        self.assertEqual(
            [argv[1] for argv, _ in self.calls],
            ["restore", "msbuild", "format", "msbuild", "format", "run"],
        )
        self.assertEqual(
            [argv[3] for argv, _ in self.calls if argv[1] == "format"],
            [str(self.project), str(self.library)],
        )
        restore, run = self.calls[0][0], self.calls[-1][0]
        self.assertIn(str(self.root / "local feed"), restore)
        self.assertIn("-p:Configuration=Checked", restore)
        self.assertIn("--no-restore", run)
        self.assertEqual(run[-3:], ["--", "--fixture", "literal & fixture"])
        for name, value in {**properties, "RuntimeIdentifier": "win-arm64"}.items():
            self.assertIn(f"-p:{name}={value}", restore)
            self.assertIn(f"-p:{name}={value}", run)
        for argv, options in self.calls[1:-1]:
            self.assertEqual(options["cwd"], self.root)
            self.assertEqual(options["env"]["Configuration"], "Checked")
            self.assertNotIn("configuration", options["env"])
            self.assertEqual(options["env"]["RuntimeIdentifier"], "win-arm64")
            self.assertEqual(options["env"]["CoreProject"], str(self.library))
            self.assertEqual(options["env"]["NativeRid"], "win-arm64")
            if argv[1] == "format":
                self.assertEqual(argv[2], "style")
                self.assertIn("--no-restore", argv)
                self.assertIn("--verify-no-changes", argv)
                self.assertEqual(argv[argv.index("--severity") + 1], "info")
        self.assertEqual(properties, original)
        self.assertEqual(self.env["configuration"], "Debug")
        self.assertEqual(self.calls[-1][1]["env"], self.env)

    def test_referenced_library_diagnostic_prevents_entry_execution_or_build(self):
        for operation in (bindings.run_managed, bindings.build_managed):
            self.calls.clear()
            self.failure = lambda argv: argv[1:4] == ["format", "style", str(self.library)]
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                operation(
                    "dotnet",
                    self.project,
                    env=self.env,
                    cwd=self.root,
                    verify_style=True,
                    runner=self.runner,
                )
            self.assertEqual(self.calls[0][0][1], "restore")
            self.assertEqual(self.calls[-1][0][1:4], ["format", "style", str(self.library)])
            self.assertFalse(any(argv[1] in {"run", "build"} for argv, _ in self.calls))

    def test_restore_failure_prevents_semantic_evaluation(self):
        self.failure = lambda argv: argv[1] == "restore"
        with self.assertRaises(subprocess.CalledProcessError):
            bindings.run_managed(
                "dotnet",
                self.project,
                env=self.env,
                cwd=self.root,
                verify_style=True,
                runner=self.runner,
            )
        self.assertEqual(len(self.calls), 1)

    def test_already_restored_build_checks_graph_without_any_restore(self):
        bindings.build_managed(
            "dotnet",
            self.project,
            env=self.env,
            cwd=self.root,
            no_restore=True,
            properties={"NativeRid": "win-x64"},
            verify_style=True,
            runner=self.runner,
        )
        self.assertEqual(
            [argv[1] for argv, _ in self.calls], ["msbuild", "format", "msbuild", "format", "build"]
        )
        self.assertIn("--no-restore", self.calls[-1][0])
        self.assertIn("-p:NativeRid=win-x64", self.calls[-1][0])

    def test_overridden_property_fails_before_format_or_runtime(self):
        self.override = {"Configuration": "Debug"}
        with self.assertRaisesRegex(ValueError, "cannot retain Configuration=Release"):
            bindings.run_managed(
                "dotnet",
                self.project,
                env=self.env,
                cwd=self.root,
                no_restore=True,
                verify_style=True,
                runner=self.runner,
            )
        self.assertEqual([argv[1] for argv, _ in self.calls], ["msbuild"])

    def test_incomplete_workspace_cannot_pass_on_format_zero_exit(self):
        self.workspace_warning = True
        with self.assertRaisesRegex(ValueError, "workspace did not load cleanly"):
            bindings.run_managed(
                "dotnet",
                self.project,
                env=self.env,
                cwd=self.root,
                no_restore=True,
                verify_style=True,
                runner=self.runner,
            )
        self.assertEqual([argv[1] for argv, _ in self.calls], ["msbuild", "format"])

    def test_reference_property_overrides_require_explicit_support(self):
        self.reference_metadata = {"AdditionalProperties": "Configuration=Other"}
        with self.assertRaisesRegex(ValueError, "cannot retain reference AdditionalProperties"):
            bindings.run_managed(
                "dotnet",
                self.project,
                env=self.env,
                cwd=self.root,
                no_restore=True,
                verify_style=True,
                runner=self.runner,
            )
        self.assertEqual([argv[1] for argv, _ in self.calls], ["msbuild"])

    def test_native_consumer_enables_verification_before_staging_or_execution(self):
        failure = subprocess.CalledProcessError(2, ["dotnet", "format"])
        with (
            patch.object(consumers.sys, "platform", "darwin"),
            patch.object(
                consumers, "subprocess_environment", return_value={"DOTNET_BIN": "dotnet"}
            ),
            patch.object(bindings, "build_managed", side_effect=failure) as build,
            patch.object(bindings, "stage_native") as stage,
            self.assertRaises(subprocess.CalledProcessError),
        ):
            consumers.check_dotnet(
                project=self.project,
                nuget_config=self.root / "NuGet.Config",
                framework=self.root / "Native.framework/Native",
                library_name="native",
                windows_root=self.root / "windows",
                work_parent=self.root / "work",
                cwd=self.root,
                argv=["--package-source", str(self.root / "feed")],
            )
        self.assertTrue(build.call_args.kwargs["verify_style"])
        self.assertEqual(build.call_args.kwargs["sources"], [str(self.root / "feed")])
        stage.assert_not_called()

    def test_windows_package_consumer_requires_target_native_dotnet_before_restore(self):
        package = self.root / "Fixture.1.0.0.nupkg"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "Fixture.nuspec",
                "<package><metadata><id>Fixture</id><version>1.0.0</version></metadata></package>",
            )
        fixture = self.root / "Program.cs"
        fixture.write_text("Console.WriteLine(42);\n", encoding="utf-8")
        env = {"DOTNET_BIN": "selected-dotnet", "DOTNET_CLI_UI_LANGUAGE": "en"}
        arguments = dict(
            package_id="Fixture", fixture=fixture, work_parent=self.root, argv=[str(package)]
        )
        for machine, architecture, version, accepted in (
            ("arm64", "x64", "10.0.403", False),
            ("arm64", "arm64", "9.0.100", False),
            ("arm64", "arm64", "10.0.403", True),
            ("x64", "x64", "10.0.403", True),
        ):
            with (
                self.subTest(machine=machine, architecture=architecture, version=version),
                patch.object(consumers.sys, "platform", "win32"),
                patch.object(consumers.windows, "host_architecture", return_value=machine),
                patch.object(consumers, "subprocess_environment", return_value=env),
                patch.object(
                    consumers.subprocess,
                    "check_output",
                    side_effect=[f"Host:\n  Architecture: {architecture}\n", version],
                ) as probe,
                patch.object(bindings, "run_isolated_consumer") as run,
            ):
                if accepted:
                    consumers.check_nuget(**arguments)
                    run.assert_called_once()
                    self.assertEqual(run.call_args.args, ("selected-dotnet",))
                    self.assertEqual(run.call_args.kwargs["packages"], [package])
                else:
                    with self.assertRaisesRegex(ValueError, "target-native .NET 10 SDK"):
                        consumers.check_nuget(**arguments)
                    run.assert_not_called()
                self.assertEqual(
                    [call.args[0] for call in probe.call_args_list],
                    [["selected-dotnet", "--info"], ["selected-dotnet", "--version"]],
                )

        with (
            patch.object(consumers.sys, "platform", "win32"),
            patch.object(
                consumers.windows,
                "host_architecture",
                side_effect=ValueError("Target-native Windows Python is required"),
            ),
            patch.object(consumers.subprocess, "check_output") as probe,
            patch.object(bindings, "run_isolated_consumer") as run,
            self.assertRaisesRegex(ValueError, "Target-native Windows Python"),
        ):
            consumers.check_nuget(**arguments)
        probe.assert_not_called()
        run.assert_not_called()

    def test_windows_consumer_preserves_existing_build_dll_on_fixture_failure(self):
        output = self.root / "bin/Release/net10.0"
        output.mkdir(parents=True)
        assembly = output / "Entry.dll"
        assembly.write_bytes(b"managed fixture")
        existing = output / "native.dll"
        existing.write_bytes(b"existing build dependency")
        accepted = self.root / "windows/win-arm64/native.dll"
        accepted.parent.mkdir(parents=True)
        accepted.write_bytes(b"accepted native dependency")
        runtime = None

        def run(argv, **options):
            nonlocal runtime
            launched = Path(argv[1])
            runtime = launched.parent
            self.assertEqual(launched.read_bytes(), b"managed fixture")
            self.assertEqual((runtime / "native.dll").read_bytes(), accepted.read_bytes())
            raise subprocess.CalledProcessError(2, argv)

        with (
            patch.object(consumers.sys, "platform", "win32"),
            patch.object(consumers.windows, "host_architecture", return_value="arm64"),
            patch.object(
                consumers.subprocess,
                "check_output",
                side_effect=["Host:\n  Architecture: arm64\n", "10.0.403"],
            ),
            patch.object(
                consumers, "subprocess_environment", return_value={"DOTNET_BIN": "dotnet"}
            ),
            patch.object(bindings, "build_managed"),
            patch.object(consumers.subprocess, "run", side_effect=run),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            consumers.check_dotnet(
                project=self.project,
                nuget_config=self.root / "NuGet.Config",
                framework=self.root / "Native.framework/Native",
                library_name="native",
                windows_root=self.root / "windows",
                work_parent=self.root / "work",
                cwd=self.root,
                argv=[],
            )

        self.assertEqual(existing.read_bytes(), b"existing build dependency")
        self.assertFalse(runtime.exists())

    def test_windows_managed_consumer_rejects_emulated_dotnet_before_build(self):
        work = self.root / "work"
        with (
            patch.object(consumers.sys, "platform", "win32"),
            patch.object(consumers.windows, "host_architecture", return_value="arm64"),
            patch.object(
                consumers, "subprocess_environment", return_value={"DOTNET_BIN": "dotnet-x64"}
            ),
            patch.object(
                consumers.subprocess,
                "check_output",
                side_effect=["Host:\n  Architecture: x64\n", "10.0.403"],
            ) as probe,
            patch.object(
                bindings, "build_managed", side_effect=AssertionError("Managed build started")
            ) as build,
            patch.object(bindings, "stage_native") as stage,
            patch.object(consumers.subprocess, "run") as run,
            self.assertRaisesRegex(ValueError, "target-native .NET 10 SDK"),
        ):
            consumers.check_dotnet(
                project=self.project,
                nuget_config=self.root / "NuGet.Config",
                framework=self.root / "Native.framework/Native",
                library_name="native",
                windows_root=self.root / "windows",
                work_parent=work,
                cwd=self.root,
                argv=[],
            )
        self.assertEqual(
            [call.args[0] for call in probe.call_args_list],
            [["dotnet-x64", "--info"], ["dotnet-x64", "--version"]],
        )
        self.assertFalse(work.exists())
        build.assert_not_called()
        stage.assert_not_called()
        run.assert_not_called()

    def test_explicit_windows_x64_managed_consumer_uses_x64_sdk_and_checked_dll(self):
        dotnet = self.root / "tools/dotnet-x64/dotnet.exe"
        write_pe(dotnet, 0x8664)
        library = self.root / "windows/win-x64/native.dll"
        write_pe(library, 0x8664)
        accepted = library.read_bytes()
        output = self.root / "bin/Release/net10.0/win-x64"
        output.mkdir(parents=True)
        (output / "Entry.dll").write_bytes(b"managed x64 fixture")
        (output / library.name).write_bytes(b"developer build dependency")
        environment = {
            "DOTNET_BIN": str(dotnet),
            "DOTNET_ROOT": str(self.root / "tools/dotnet"),
            "DOTNET_HOST_PATH": str(self.root / "tools/dotnet/dotnet.exe"),
        }
        launched = None

        def run(argv, **options):
            nonlocal launched
            self.assertEqual(argv[0], str(dotnet))
            launched = Path(argv[1])
            self.assertEqual(launched.read_bytes(), b"managed x64 fixture")
            self.assertEqual((launched.parent / library.name).read_bytes(), accepted)
            self.assertEqual(options["env"]["DOTNET_ROOT"], str(dotnet.parent))
            self.assertEqual(options["env"]["DOTNET_ROOT_X64"], str(dotnet.parent))
            self.assertNotIn("DOTNET_HOST_PATH", options["env"])
            error = PermissionError("The staged managed fixture DLL is still locked")
            error.winerror = 32
            cleanup.side_effect = [error, DEFAULT]

        def managed():
            consumers.check_dotnet(
                project=self.project,
                nuget_config=self.root / "NuGet.Config",
                framework=self.root / "Native.framework/Native",
                library_name="native",
                windows_root=self.root / "windows",
                work_parent=self.root / "work",
                cwd=self.root,
                argv=["--windows-architecture", "x64"],
            )

        with (
            patch.object(consumers.sys, "platform", "win32"),
            patch.object(consumers.windows, "host_architecture", return_value="arm64"),
            patch.object(consumers, "subprocess_environment", side_effect=environment.copy),
            patch.object(
                consumers.subprocess,
                "check_output",
                side_effect=["Host:\n  Architecture: x64\n", "10.0.403"] * 2,
            ),
            patch.object(
                bindings, "build_managed", side_effect=lambda *_, **__: write_pe(library, 0xAA64)
            ) as build,
            patch.object(consumers.subprocess, "run", side_effect=run) as execute,
            patch.object(
                tempfile.TemporaryDirectory, "_rmtree", wraps=tempfile.TemporaryDirectory._rmtree
            ) as cleanup,
            patch.object(workspaces.time, "sleep") as sleep,
            patch("builtins.print") as report,
        ):
            managed()
            build.assert_called_once()
            self.assertEqual(
                build.call_args.kwargs["properties"],
                {
                    "UseSharedCompilation": "false",
                    "RuntimeIdentifier": "win-x64",
                    "UseAppHost": "false",
                },
            )
            execute.assert_called_once()
            sleep.assert_called_once()
            report.assert_called_once_with(
                "Windows x64 managed fixture passed under emulation on ARM64."
            )
            build.reset_mock()
            execute.reset_mock()
            cleanup.side_effect = None
            with self.assertRaisesRegex(ValueError, "Expected target-native x64 PE image"):
                managed()
            build.assert_not_called()
            execute.assert_not_called()
        self.assertFalse(launched.exists())
        self.assertEqual((output / library.name).read_bytes(), b"developer build dependency")

    def test_explicit_windows_x64_package_consumer_pins_rid_and_rejects_wrong_dotnet_pe(self):
        package = self.root / "Fixture.1.0.0.nupkg"
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                "Fixture.nuspec",
                "<package><metadata><id>Fixture</id><version>1.0.0</version></metadata></package>",
            )
        dotnet = self.root / "tools/dotnet-x64/dotnet.exe"
        write_pe(dotnet, 0x8664)
        fixture = self.root / "Program.cs"
        fixture.write_text("Console.WriteLine(42);\n", encoding="utf-8")
        arguments = dict(
            package_id="Fixture",
            fixture=fixture,
            work_parent=self.root,
            argv=[str(package), "--windows-architecture", "x64"],
        )
        with (
            patch.object(consumers.sys, "platform", "win32"),
            patch.object(consumers.windows, "host_architecture", return_value="arm64"),
            patch.object(
                consumers, "subprocess_environment", side_effect=lambda: {"DOTNET_BIN": str(dotnet)}
            ),
            patch.object(
                consumers.subprocess,
                "check_output",
                side_effect=["Host:\n  Architecture: x64\n", "10.0.403"],
            ) as probe,
            patch.object(bindings, "run_isolated_consumer") as run,
            patch("builtins.print") as report,
        ):
            consumers.check_nuget(**arguments)
            self.assertEqual(run.call_args.args, (str(dotnet),))
            self.assertEqual(run.call_args.kwargs["runtime"], "win-x64")
            self.assertEqual(run.call_args.kwargs["packages"], [package])
            report.assert_called_once_with(
                "Windows x64 NuGet fixture passed under emulation on ARM64."
            )
            run.reset_mock()
            probe.reset_mock()
            write_pe(dotnet, 0xAA64)
            with self.assertRaisesRegex(ValueError, "Expected target-native x64 PE image"):
                consumers.check_nuget(**arguments)
            probe.assert_not_called()
            run.assert_not_called()

    def test_explicit_windows_target_cannot_use_macos_or_reverse_emulation(self):
        for platform, architecture in (("darwin", None), ("win32", "x64")):
            with (
                self.subTest(platform=platform),
                patch.object(consumers.sys, "platform", platform),
                patch.object(consumers.windows, "host_architecture", return_value=architecture),
                patch.object(consumers.windows, "native_dotnet") as dotnet,
                patch.object(bindings, "build_managed") as build,
                self.assertRaises(ValueError),
            ):
                consumers.check_dotnet(
                    project=self.project,
                    nuget_config=self.root / "NuGet.Config",
                    framework=self.root / "Native.framework/Native",
                    library_name="native",
                    windows_root=self.root / "windows",
                    work_parent=self.root / "work",
                    cwd=self.root,
                    argv=["--windows-architecture", "arm64"],
                )
            dotnet.assert_not_called()
            build.assert_not_called()
            self.assertFalse((self.root / "work").exists())
