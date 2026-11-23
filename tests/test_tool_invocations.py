"""Inject C compiler and .NET commands and verify their exact arguments."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from sdk_tooling import c_compiler, dotnet


class ToolInvocationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-tool-invocations-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"PATH": str(self.root / "tools"), "SDK_NATIVE_LIBRARY": "accepted-native"}
        self.calls = []
        guard = mock.patch.object(
            subprocess,
            "run",
            side_effect=AssertionError("Unit tests must inject external commands"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def run_command(self, argv, **options):
        self.calls.append((argv, options))
        self.assertTrue(options["check"])
        self.assertNotIn("shell", options)
        return subprocess.CompletedProcess(argv, 0)

    def file(self, relative, content=b"owned bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def test_clang_preserves_standard_literal_paths_and_link_flags(self):
        source = self.file("source with spaces/caller $(literal).c")
        library = self.file("Framework Name.framework/Native")
        output = self.root / "work/abi"
        c_compiler.compile_c(
            source,
            output=output,
            compiler=("xcrun", "clang"),
            env=self.env,
            cwd=self.root,
            include_dirs=(source.parent,),
            libraries=(library,),
            standard="c17",
            flags=("-fsanitize=address", "-Wl,-rpath," + str(library.parent)),
            runner=self.run_command,
        )
        argv, options = self.calls[0]
        self.assertEqual(argv[:2], ["xcrun", "clang"])
        self.assertIn("-std=c17", argv)
        self.assertIn("-Werror", argv)
        self.assertIn(str(source), argv)
        self.assertIn(str(library), argv)
        self.assertIn("-fsanitize=address", argv)
        self.assertEqual(argv[argv.index("-o") + 1], str(output))
        self.assertEqual(options["env"], self.env)

    def test_msvc_uses_utf8_and_retains_c11_import_library_and_caller_flags(self):
        source = self.file("caller.c")
        library = self.file("native.dll.lib")
        output = self.root / "work/abi.exe"
        c_compiler.compile_c(
            source,
            output=output,
            compiler=("cl.exe",),
            env=self.env,
            cwd=self.root,
            include_dirs=(source.parent,),
            libraries=(library,),
            standard="c11",
            style="msvc",
            flags=("/MT", "/D_CRT_SECURE_NO_WARNINGS"),
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        for value in (
            "/utf-8",
            "/std:c11",
            "/W4",
            "/WX",
            "/MT",
            "/D_CRT_SECURE_NO_WARNINGS",
            str(library),
            "/Fe:" + str(output),
            "/Fo:" + str(output.with_suffix(".obj")),
        ):
            self.assertIn(value, argv)

    def test_relative_compiler_output_is_owned_by_command_working_directory(self):
        c_compiler.compile_c(
            "caller.c",
            output="out/abi",
            compiler=("clang",),
            env=self.env,
            cwd=self.root,
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        self.assertEqual(
            Path(argv[argv.index("-o") + 1]).resolve(), (self.root / "out/abi").resolve()
        )
        self.assertTrue((self.root / "out").is_dir())

    def test_compiler_style_and_shell_strings_are_rejected_before_execution(self):
        for compiler, style in ((("clang",), "unknown"), ("xcrun clang", "clang")):
            with self.subTest(compiler=compiler, style=style), self.assertRaises(ValueError):
                c_compiler.compile_c(
                    "owned.c",
                    output=self.root / "out/abi",
                    compiler=compiler,
                    style=style,
                    env=self.env,
                    cwd=self.root,
                    runner=self.run_command,
                )
        self.assertFalse(self.calls)
        self.assertFalse((self.root / "out").exists())

    def test_managed_explicit_restore_and_fixture_arguments(self):
        project = self.file("managed/Test.csproj")
        config = self.file("NuGet.Config")
        library = self.file("native library.dylib")
        dotnet.run_managed(
            "dotnet",
            project,
            env=self.env,
            cwd=self.root,
            nuget_config=config,
            sources=(self.root / "local feed",),
            properties={"CoreProject": "core & fixture.csproj"},
            arguments=("--native-library", library),
            runner=self.run_command,
        )
        restore, run = [call[0] for call in self.calls]
        self.assertEqual(restore[1], "restore")
        self.assertEqual(restore[restore.index("--configfile") + 1], str(config))
        self.assertEqual(restore[restore.index("--source") + 1], str(self.root / "local feed"))
        self.assertIn("-p:CoreProject=core & fixture.csproj", restore)
        self.assertIn("--no-restore", run)
        self.assertEqual(run[run.index("--") + 1 :], ["--native-library", str(library)])
        self.assertEqual(self.env["SDK_NATIVE_LIBRARY"], "accepted-native")

    def test_managed_implicit_restore_preserves_runtime_and_core_selection(self):
        dotnet.run_managed(
            "dotnet",
            "Provider.csproj",
            env=self.env,
            cwd=self.root,
            runtime="win-arm64",
            properties={"CoreProject": "Pinned.csproj"},
            arguments=("--native",),
            runner=self.run_command,
        )
        self.assertEqual(len(self.calls), 1)
        argv, _ = self.calls[0]
        self.assertNotIn("--no-restore", argv)
        self.assertIn("-p:RuntimeIdentifier=win-arm64", argv)
        self.assertIn("-p:CoreProject=Pinned.csproj", argv)
        self.assertEqual(argv[-2:], ["--", "--native"])

    def test_restore_failure_prevents_run_or_pack(self):
        def failed(argv, **options):
            self.run_command(argv, **options)
            raise subprocess.CalledProcessError(23, argv)

        for operation in (dotnet.run_managed, dotnet.pack_project):
            self.calls.clear()
            kwargs = {"output": self.root / "packages"} if operation is dotnet.pack_project else {}
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                operation(
                    "dotnet",
                    "Owned.csproj",
                    env=self.env,
                    cwd=self.root,
                    nuget_config=self.root / "NuGet.Config",
                    runner=failed,
                    **kwargs,
                )
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0][0][1], "restore")

    def test_pack_keeps_explicit_version_runtime_and_external_targets(self):
        properties = {
            "Version": "1.2.0-owned",
            "Runtime": "win-arm64",
            "CustomAfterMicrosoftCommonTargets": "core & vendor.targets",
        }
        dotnet.pack_project(
            "dotnet",
            "Owned.csproj",
            output=self.root / "feed",
            env=self.env,
            cwd=self.root,
            nuget_config=self.root / "NuGet.Config",
            properties=properties,
            runner=self.run_command,
        )
        self.assertEqual([call[0][1] for call in self.calls], ["restore", "pack"])
        argv, _ = self.calls[-1]
        self.assertIn("--no-restore", argv)
        for name, value in properties.items():
            self.assertIn("-p:" + name + "=" + value, argv)
        self.assertEqual(argv[argv.index("--output") + 1], str(self.root / "feed"))

    def test_relative_package_output_is_owned_by_command_working_directory(self):
        dotnet.pack_project(
            "dotnet",
            "Core.csproj",
            output="packages",
            env=self.env,
            cwd=self.root,
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        self.assertEqual(
            Path(argv[argv.index("--output") + 1]).resolve(), (self.root / "packages").resolve()
        )
        self.assertTrue((self.root / "packages").is_dir())

    def test_pack_can_keep_project_implicit_restore_policy(self):
        dotnet.pack_project(
            "dotnet",
            "Core.csproj",
            output=self.root / "packages",
            env=self.env,
            cwd=self.root,
            properties={"PublishRepositoryUrl": "false"},
            runner=self.run_command,
        )
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--no-restore", self.calls[0][0])
        self.assertIn("-p:PublishRepositoryUrl=false", self.calls[0][0])
