import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import adapters, hosted_windows
from sdk_tooling.__main__ import main


class HostedWindowsTests(unittest.TestCase):
    def test_runs_sdk_owned_command_with_hosted_paths(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as temporary:
            root = Path(temporary)
            sdk = root / "sdk checkout"
            sdk.mkdir()
            command = ["-m", "tools.native_host", "--architecture", "x86_64"]
            dotnet = str(root / "dotnet installation" / "dotnet.exe")
            env = {"RUNNER_TEMP": str(root)}

            with (
                patch.object(hosted_windows.sys, "platform", "win32"),
                patch.dict(
                    os.environ,
                    env | {"NAVI_CI_ARTIFACTS": "legacy stale", "VM_RUNNER_ARTIFACTS_DIR": "stale"},
                    clear=True,
                ),
                patch.object(hosted_windows.shutil, "which", return_value=dotnet),
                patch.object(hosted_windows.subprocess, "run") as run,
            ):
                hosted_windows.run(sdk, command)

            run.assert_called_once_with(
                adapters.python_command(sdk, *command),
                cwd=sdk,
                env=dict(
                    env, DOTNET_BIN=dotnet, VM_RUNNER_ARTIFACTS_DIR=str(root / "sdk-acceptance")
                ),
                check=True,
            )

    def test_rejects_a_relative_dotnet_path(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(hosted_windows.sys, "platform", "win32"),
            patch.dict(os.environ, {"RUNNER_TEMP": temporary}, clear=True),
            patch.object(hosted_windows.shutil, "which", return_value="dotnet.exe"),
            patch.object(hosted_windows.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(SystemExit, "The pinned x64 .NET SDK was not installed"):
                hosted_windows.run(Path(temporary), ["tools/windows.py"])

        run.assert_not_called()

    def test_generated_headers_use_the_sdk_pin_before_windows_acceptance(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sdk = root / "SDK"
            sdk.mkdir()
            (sdk / "mise.toml").write_text(
                '[tools]\n"cargo:cbindgen" = { version = "0.29.4", os = ["macos"] }\n',
                encoding="utf-8",
            )
            command = ["-m", "distribution.verify_windows"]
            dotnet = str(root / "dotnet.exe")
            environment = {"RUNNER_TEMP": str(root), "PATH": "original path"}
            events = []

            def run(argv, *, cwd, env, check):
                self.assertEqual(cwd, sdk)
                self.assertTrue(check)
                if argv[0] == "cargo":
                    installation = argv[argv.index("--root") + 1]
                    self.assertEqual(installation.parent, root)
                    self.assertEqual(
                        argv,
                        [
                            "cargo",
                            "install",
                            "--locked",
                            "--version",
                            "=0.29.4",
                            "--root",
                            installation,
                            "cbindgen",
                        ],
                    )
                    (installation / "bin").mkdir()
                    (installation / "bin" / "cbindgen.exe").touch()
                    events.append(("install", installation))
                else:
                    self.assertEqual(argv, adapters.python_command(sdk, *command))
                    events.append(("acceptance", env["PATH"]))

            def prepare(checkout, *, env):
                self.assertEqual(checkout, sdk)
                self.assertEqual(env["DOTNET_BIN"], dotnet)
                events.append(("prepare", env["PATH"]))

            with (
                patch.object(hosted_windows.sys, "platform", "win32"),
                patch.dict(os.environ, environment, clear=True),
                patch.object(hosted_windows.shutil, "which", return_value=dotnet),
                patch.object(hosted_windows.c_headers, "has_generated_products", return_value=True),
                patch.object(hosted_windows.c_headers, "prepare_generated", side_effect=prepare),
                patch.object(hosted_windows.subprocess, "run", side_effect=run),
            ):
                hosted_windows.run(sdk, command)

            installation = events[0][1]
            path = os.pathsep.join((str(installation / "bin"), environment["PATH"]))
            self.assertEqual(
                events, [("install", installation), ("prepare", path), ("acceptance", path)]
            )
            self.assertFalse(installation.exists())

    def test_generated_headers_require_an_exact_cbindgen_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for version in ("latest", "0.29", "~0.29.4"):
                with self.subTest(version=version):
                    (root / "mise.toml").write_text(
                        f'[tools]\n"cargo:cbindgen" = "{version}"\n', encoding="utf-8"
                    )
                    with self.assertRaisesRegex(ValueError, "exact cargo:cbindgen"):
                        hosted_windows._cbindgen_pin(root)

    def test_prepares_only_a_valid_x64_generator_from_outside_the_candidate(self):
        cases = [
            (False, "cbindgen 0.29.4", True),
            (True, "cbindgen 0.29.4", True),
            (True, "cbindgen 0.29.3", False),
        ]
        for warm, installed, accepted in cases:
            with self.subTest(warm=warm, installed=installed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp).resolve()
                tools = root / "sdk-cbindgen/0.29.4"
                binary = tools / "bin/cbindgen.exe"

                def write_binary():
                    binary.parent.mkdir(parents=True)
                    binary.touch()

                if warm:
                    write_binary()

                def run(argv, *, cwd, env, check):
                    self.assertEqual(cwd, root)
                    self.assertTrue(check)
                    toolchain = "1.99.1-x86_64-pc-windows-msvc"
                    if argv[0] == "rustup":
                        self.assertEqual(argv[:4], ["rustup", "toolchain", "install", toolchain])
                        return
                    self.assertEqual(argv[:3], ["cargo", f"+{toolchain}", "install"])
                    self.assertEqual(argv[argv.index("--root") + 1], tools)
                    self.assertEqual(argv[argv.index("--target") + 1], "x86_64-pc-windows-msvc")
                    self.assertEqual(argv[argv.index("--version") + 1], "=0.29.4")
                    write_binary()

                with (
                    patch.object(hosted_windows.sys, "platform", "win32"),
                    patch.object(hosted_windows.windows, "require_pe_machine") as require_machine,
                    patch.object(
                        hosted_windows.subprocess, "check_output", return_value=installed
                    ) as version,
                    patch.object(hosted_windows.subprocess, "run", side_effect=run) as command,
                ):
                    if accepted:
                        hosted_windows.prepare_cbindgen_cache(root, "0.29.4", "1.99.1", warm=warm)
                    else:
                        with self.assertRaisesRegex(ValueError, "differs from the SDK pin"):
                            hosted_windows.prepare_cbindgen_cache(
                                root, "0.29.4", "1.99.1", warm=warm
                            )
                    require_machine.assert_called_once_with(binary, "x64")
                    self.assertEqual(version.call_args.args[0], [binary, "--version"])
                    self.assertEqual(command.call_count, 0 if warm else 2)

    def test_driver_gets_the_cached_generator_without_preparing_headers_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            sdk = root / "SDK"
            sdk.mkdir()
            (sdk / "mise.toml").write_text('[tools]\n"cargo:cbindgen" = "0.29.4"\n')
            binary = root / "sdk-cbindgen/0.29.4/bin/cbindgen.exe"
            binary.parent.mkdir(parents=True)
            binary.touch()
            flags = {"RUNNER_TEMP": str(root), "SDK_TOOLING_HOSTED_CBINDGEN_CACHE": "true"}
            with (
                patch.object(hosted_windows.sys, "platform", "win32"),
                patch.object(Path, "cwd", return_value=sdk),
                patch.dict(os.environ, flags, clear=True),
                patch.object(hosted_windows.shutil, "which", return_value=str(root / "dotnet.exe")),
                patch.object(hosted_windows.c_headers, "has_generated_products", return_value=True),
                patch.object(hosted_windows.c_headers, "prepare_generated") as generated,
                patch.object(hosted_windows.subprocess, "run") as run,
            ):
                command = ["-m", "distribution.verify_windows"]
                main(["windows", "--driver-prepares-headers", *command])
            generated.assert_not_called()
            run.assert_called_once()
            self.assertEqual(run.call_args.args[0], adapters.python_command(sdk, *command))
            self.assertEqual(run.call_args.kwargs["env"]["PATH"], str(binary.parent))
            self.assertNotIn("SDK_TOOLING_HOSTED_CBINDGEN_CACHE", run.call_args.kwargs["env"])
