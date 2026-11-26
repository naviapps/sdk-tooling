import ctypes
import json
import struct
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import DEFAULT, Mock, patch

from cargo_build_fixtures import FIXTURE_UNIT, fixture_dependencies

from sdk_tooling import binding_checks, c_compiler, workspaces
from sdk_tooling.artifacts import files, hashes
from sdk_tooling.windows import (
    MSVC_TARGETS,
    build_library,
    host_architecture,
    native_environment,
    native_msvc_environment,
    probe_abi,
    tool_inputs,
    verify_artifact,
)


def write_pe(path, machine):
    path.write_bytes(b"MZ" + bytes(0x3A) + struct.pack("<I4sH", 0x40, b"PE\0\0", machine))


def x64_msvc_fixture(root):
    tools = root / "bin/Hostarm64/x64"
    tools.mkdir(parents=True)
    for name in ("cl.exe", "link.exe"):
        write_pe(tools / name, 0xAA64)
    sdk = root / "Windows SDK"
    version = "10.0.26100.0"
    msvc_lib = root / "lib/x64"
    ucrt = sdk / "Lib" / version / "ucrt/x64"
    kernel = sdk / "Lib" / version / "um/x64"
    for folder, names in (
        (msvc_lib, ("libcmt.lib", "libvcruntime.lib")),
        (ucrt, ("libucrt.lib",)),
        (kernel, ("kernel32.lib",)),
    ):
        folder.mkdir(parents=True)
        for name in names:
            (folder / name).touch()
    return {
        "VSCMD_ARG_HOST_ARCH": "arm64",
        "VSCMD_ARG_TGT_ARCH": "x64",
        "VCTOOLSINSTALLDIR": str(root),
        "WINDOWSSDKDIR": str(sdk),
        "WINDOWSSDKVERSION": version + "\\",
        "LIB": ";".join(map(str, (msvc_lib, ucrt, kernel))),
    }, tools


class WindowsToolsTests(unittest.TestCase):
    def test_portable_artifact_check_rejects_changed_bytes_sources_and_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            sources = {"runtime.rs": "verified hash"}
            builder = SimpleNamespace(
                root=root,
                compiler="1.99.1",
                package="fixture-ffi",
                library="fixture",
                build_sources=lambda: sources,
                abi_versions=lambda windows: {"fixture_abi_version": 1},
            )
            destination = root / ".native-build/windows/win-arm64"
            destination.mkdir(parents=True)
            library = destination / "fixture.dll"
            library.write_bytes(b"retained DLL")
            receipt = dict(
                schema=2,
                target="aarch64-pc-windows-msvc",
                compiler="rustc 1.99.1 fixture",
                inputs=dict(sources),
                toolInputs=tool_inputs(),
                outputs=hashes(files(destination), destination),
                dependencies=fixture_dependencies("aarch64-pc-windows-msvc"),
                cargoBuild={"aarch64-pc-windows-msvc": [FIXTURE_UNIT]},
                abiVersions={"fixture_abi_version": 1},
                abiProbeArchitecture="arm64",
            )
            (destination / "native-build.json").write_text(json.dumps(receipt))
            self.assertEqual(verify_artifact(builder)[0], destination)
            abi_probe = dict(receipt, abiProbeArchitecture="x64")
            (destination / "native-build.json").write_text(json.dumps(abi_probe))
            with self.assertRaisesRegex(ValueError, "ABI receipt"):
                verify_artifact(builder)
            (destination / "native-build.json").write_text(json.dumps(receipt))
            sources["runtime.rs"] = "changed source"
            with self.assertRaisesRegex(ValueError, "sources or build contract"):
                verify_artifact(builder)
            sources.update(receipt["inputs"])
            library.write_bytes(b"changed DLL")
            with self.assertRaisesRegex(ValueError, "differs from its receipt"):
                verify_artifact(builder)
            with self.assertRaisesRegex(ValueError, "explicitly"):
                verify_artifact(builder, "unknown")

    def test_rejects_non_native_worker_before_any_commands(self):
        with (
            patch("sdk_tooling.windows.sys.platform", "darwin"),
            patch("sdk_tooling.windows.subprocess.check_output") as command,
        ):
            with self.assertRaisesRegex(ValueError, "target-native"):
                native_environment()
            command.assert_not_called()

    def test_native_host_checks_windows_machines_and_python_coff_header(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "python.exe"
            for process, native, image, expected in (
                (0, 0xAA64, 0xAA64, "arm64"),
                (0, 0x8664, 0x8664, "x64"),
                (0x8664, 0xAA64, 0x8664, None),
                (0, 0xAA64, 0x8664, None),
                (0, 0xAA64, 0xA641, None),
                (0, 0x014C, 0x014C, None),
            ):
                write_pe(executable, image)

                def query(handle, process_out, native_out):
                    self.assertEqual(handle, -1)
                    ctypes.cast(process_out, ctypes.POINTER(ctypes.c_ushort))[0] = process
                    ctypes.cast(native_out, ctypes.POINTER(ctypes.c_ushort))[0] = native
                    return 1

                kernel = Mock()
                kernel.GetCurrentProcess.return_value = -1
                kernel.IsWow64Process2.side_effect = query
                with (
                    self.subTest(process=process, native=native, image=image),
                    patch("sdk_tooling.windows.sys.platform", "win32"),
                    patch("sdk_tooling.windows.sys.executable", str(executable)),
                    patch.object(ctypes, "WinDLL", return_value=kernel, create=True) as load,
                ):
                    if expected is None:
                        with self.assertRaisesRegex(ValueError, "target-native Windows worker"):
                            host_architecture()
                    else:
                        self.assertEqual(host_architecture(), expected)
                    kernel.IsWow64Process2.assert_called_once()
                    load.assert_called_once_with("kernel32", use_last_error=True)

    def environment(
        self, *, host="arm64", target="arm64", dotnet_arch="arm64", msvc_root="", extra=""
    ):
        def output(command, **options):
            if options.get("shell"):
                return (
                    "VSCMD_ARG_TGT_ARCH="
                    + target
                    + "\nVSCMD_ARG_HOST_ARCH="
                    + host
                    + f"\nVCTOOLSINSTALLDIR={msvc_root}\nPATH=C:/tools"
                    + extra
                )
            if command[-1] == "--info":
                return "Architecture: " + dotnet_arch
            if command[-1] == "--version":
                return "10.0.403"
            return "C:/Visual Studio"

        return output

    def msvc_tools(self, architecture, target=None):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        tools = root / f"bin/Host{architecture}/{target or architecture}"
        tools.mkdir(parents=True)
        for name in ("cl.exe", "link.exe"):
            write_pe(tools / name, 0xAA64 if architecture == "arm64" else 0x8664)
        return root, tools

    def test_checks_selected_msvc_executables_before_dotnet(self):
        for failure in (
            "missing compiler",
            "wrong linker machine",
            "compiler shadowed",
            "malformed compiler",
        ):
            root, tools = self.msvc_tools("arm64")
            selected = {name: str(tools / name) for name in ("cl.exe", "link.exe")}
            if failure == "missing compiler":
                selected["cl.exe"] = None
            elif failure == "wrong linker machine":
                write_pe(tools / "link.exe", 0x8664)
            elif failure == "compiler shadowed":
                substitute = root / "cl.exe"
                write_pe(substitute, 0xAA64)
                selected["cl.exe"] = str(substitute)
            else:
                (tools / "cl.exe").write_bytes(b"MZ")
            with (
                self.subTest(failure=failure),
                patch("sdk_tooling.windows.host_architecture", return_value="arm64"),
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=self.environment(msvc_root=root),
                ) as command,
                patch(
                    "sdk_tooling.windows.shutil.which",
                    side_effect=lambda name, *, path: selected.get(name),
                ),
            ):
                with self.assertRaisesRegex(ValueError, r"cl\.exe|link\.exe"):
                    native_environment(
                        environment={"PROGRAMFILES(X86)": "P", "DOTNET_BIN": "dotnet"}
                    )
                self.assertEqual(command.call_count, 2)

    def test_selects_both_native_linkers_without_dotnet_or_artifact_build_flags(self):
        for arch, target in (
            ("arm64", "AARCH64_PC_WINDOWS_MSVC"),
            ("x64", "X86_64_PC_WINDOWS_MSVC"),
        ):
            root, expected = self.msvc_tools(arch)
            with (
                self.subTest(arch=arch),
                patch("sdk_tooling.windows.sys.platform", "win32"),
                patch("sdk_tooling.windows.host_architecture", return_value=arch),
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=self.environment(host=arch, target=arch, msvc_root=root),
                ) as command,
                patch(
                    "sdk_tooling.windows.shutil.which",
                    side_effect=lambda name, *, path: str(expected / name),
                ) as lookup,
            ):
                env, tools = native_msvc_environment(arch, {"PROGRAMFILES(X86)": "P"})
            self.assertEqual(tools, expected)
            self.assertEqual(env[f"CARGO_TARGET_{target}_LINKER"], str(tools / "link.exe"))
            self.assertNotIn("CARGO_ENCODED_RUSTFLAGS", env)
            self.assertEqual(command.call_count, 2)
            self.assertEqual(
                [call.args[0] for call in lookup.call_args_list], ["cl.exe", "link.exe"]
            )

    def test_arm64_host_selects_x64_tools_and_first_libraries(self):
        for failure in (None, "wrong linker machine", "shadowed library"):
            root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
            env, expected = x64_msvc_fixture(root)
            shadow = root / "lib/arm64"
            shadow.mkdir()
            if failure == "wrong linker machine":
                write_pe(expected / "link.exe", 0x8664)
            elif failure == "shadowed library":
                (shadow / "libcmt.lib").touch()
            env["LIB"] = str(shadow) + ";" + env["LIB"]
            initial = {
                "PROGRAMFILES(X86)": "P",
                "Cc_x86_64-pc-windows-msvc": "other-cl.exe",
                "CC_X86_64_PC_WINDOWS_MSVC": "other-cl.exe",
            }
            extra = "".join(f"\n{key}={value}" for key, value in env.items())
            with (
                self.subTest(failure=failure),
                patch("sdk_tooling.windows.host_architecture", return_value="arm64"),
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=self.environment(target="x64", msvc_root=root, extra=extra),
                ) as command,
                patch(
                    "sdk_tooling.windows.shutil.which",
                    side_effect=lambda name, *, path: str(expected / name),
                ),
            ):
                if failure:
                    with self.assertRaisesRegex(ValueError, r"link\.exe|libcmt\.lib"):
                        native_msvc_environment("arm64", initial, target_architecture="x64")
                else:
                    env, tools = native_msvc_environment(
                        "arm64", initial, target_architecture="x64"
                    )
                    self.assertEqual(tools, expected)
                    self.assertEqual(
                        env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"],
                        str(expected / "link.exe"),
                    )
                    self.assertNotIn("CARGO_TARGET_AARCH64_PC_WINDOWS_MSVC_LINKER", env)
                    self.assertEqual(env["CC_x86_64_pc_windows_msvc"], str(expected / "cl.exe"))
                    self.assertNotIn("CC_X86_64-PC-WINDOWS-MSVC", env)
                    self.assertNotIn("CC_X86_64_PC_WINDOWS_MSVC", env)
                self.assertEqual(command.call_count, 2)
                discover = command.call_args_list[0].args[0]
                first_component = discover.index("-requires") + 1
                self.assertEqual(
                    discover[first_component : first_component + 2],
                    [
                        "Microsoft.VisualStudio.Component.VC.Tools.ARM64",
                        "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                    ],
                )
                self.assertIn("-arch=x64 -host_arch=arm64", command.call_args_list[1].args[0])

    def test_rejects_unknown_msvc_target_before_discovery(self):
        with patch("sdk_tooling.windows.subprocess.check_output") as command:
            with self.assertRaisesRegex(ValueError, "MSVC target"):
                native_msvc_environment("arm64", target_architecture="i686")
            command.assert_not_called()

    def test_environment_is_local_and_case_insensitive(self):
        initial = {
            "PROGRAMFILES(X86)": "C:/Program Files (x86)",
            "DOTNET_BIN": "dotnet",
            "Path": "old",
            "RUSTFLAGS": "--cfg fixture",
        }
        root, expected = self.msvc_tools("arm64")
        with (
            patch("sdk_tooling.windows.sys.platform", "win32"),
            patch("sdk_tooling.windows.host_architecture", return_value="arm64"),
            patch(
                "sdk_tooling.windows.subprocess.check_output",
                side_effect=self.environment(msvc_root=root),
            ),
            patch(
                "sdk_tooling.windows.shutil.which",
                side_effect=lambda name, *, path: str(expected / name),
            ),
        ):
            env, tools, dotnet = native_environment(environment=initial)
        self.assertEqual(initial["Path"], "old")
        self.assertNotIn("Path", env)
        self.assertEqual(env["PATH"], "C:/tools")
        self.assertIn("-Ctarget-feature=+crt-static", env["CARGO_ENCODED_RUSTFLAGS"])
        self.assertIn("--cfg\x1ffixture", env["CARGO_ENCODED_RUSTFLAGS"])

    def test_wrong_msvc_or_managed_architecture_is_not_native_acceptance(self):
        root, expected = self.msvc_tools("arm64")
        for host, dotnet in (("x64", "arm64"), ("arm64", "x64")):
            with (
                self.subTest(host=host, dotnet=dotnet),
                patch("sdk_tooling.windows.sys.platform", "win32"),
                patch("sdk_tooling.windows.host_architecture", return_value="arm64"),
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=self.environment(host=host, dotnet_arch=dotnet, msvc_root=root),
                ),
                patch(
                    "sdk_tooling.windows.shutil.which",
                    side_effect=lambda name, *, path: str(expected / name),
                ),
            ):
                with self.assertRaises(ValueError):
                    native_environment(
                        environment={"PROGRAMFILES(X86)": "P", "DOTNET_BIN": "dotnet"}
                    )

    def test_arm64_host_uses_only_an_explicit_x64_dotnet_10(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        dotnet = root / "x64-dotnet.exe"
        write_pe(dotnet, 0x8664)
        arm_dotnet = root / "arm64-dotnet.exe"
        write_pe(arm_dotnet, 0xAA64)
        tools = root / "bin/Hostarm64/x64"
        for selection, architecture, version, failure in (
            (dotnet, "x64", "10.0.403", None),
            (None, "x64", "10.0.403", "absolute path"),
            (Path("dotnet.exe"), "x64", "10.0.403", "absolute path"),
            (root / "missing.exe", "x64", "10.0.403", "absolute path"),
            (arm_dotnet, "x64", "10.0.403", "x64 PE image"),
            (dotnet, "arm64", "10.0.403", "target-native .NET 10"),
            (dotnet, "x64", "9.0.400", "target-native .NET 10"),
        ):
            env = {"DOTNET_ROOT": str(root / "arm64")}
            if selection:
                env["DOTNET_BIN"] = str(selection)
            with (
                self.subTest(selection=selection, architecture=architecture, version=version),
                patch(
                    "sdk_tooling.windows.native_msvc_environment", return_value=(env, tools)
                ) as select,
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=[f"Host:\n  Architecture: {architecture}\n", version],
                ) as probe,
            ):
                if failure:
                    with self.assertRaisesRegex(ValueError, failure):
                        native_environment(target_architecture="x64")
                    if failure in ("absolute path", "x64 PE image"):
                        probe.assert_not_called()
                else:
                    selected, actual_tools, executable = native_environment(
                        target_architecture="x64"
                    )
                    self.assertEqual(actual_tools, tools)
                    self.assertEqual(executable, str(dotnet))
                    self.assertEqual(selected["DOTNET_BIN"], str(dotnet))
                    self.assertEqual(selected["DOTNET_ROOT"], str(root))
                    self.assertEqual(selected["DOTNET_ROOT_X64"], str(root))
                    self.assertEqual(
                        [call.args[0][0] for call in probe.call_args_list], [str(dotnet)] * 2
                    )
                select.assert_called_once_with("arm64", None, target_architecture="x64")


class WindowsCConsumerTests(unittest.TestCase):
    def test_standalone_c_checks_host_and_both_images_before_execution(self):
        for host, target, dll, executable, expected, should_build in (
            (None, None, 0x8664, 0x8664, "requires native Python", False),
            ("arm64", None, 0x8664, 0x8664, "target-native arm64", False),
            ("arm64", None, 0xAA64, 0x8664, "target-native arm64", True),
            ("arm64", None, 0xAA64, 0xAA64, None, True),
            ("x64", None, 0x8664, 0x8664, None, True),
            ("x64", "x64", 0x8664, 0x8664, None, True),
            ("arm64", "x64", 0x8664, 0x8664, None, True),
            ("arm64", "x64", 0xAA64, 0x8664, "target-native x64", False),
            ("arm64", "x64", 0x8664, 0xAA64, "target-native x64", True),
            ("x64", "arm64", 0xAA64, 0xAA64, "matching native Windows host", False),
        ):
            with (
                self.subTest(host=host, target=target, dll=dll, executable=executable),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary).resolve()
                library = root / "sdk.dll"
                imports = root / "sdk.dll.lib"
                work_parent = root / "work"
                tools = root / "verified ms tools"
                write_pe(library, dll)
                imports.write_bytes(b"SDK import library")

                def compile_c(source, *, output, **options):
                    self.assertEqual(options["compiler"], (tools / "cl.exe",))
                    self.assertEqual(options["libraries"], (imports,))
                    self.assertEqual(options["env"], {"PATH": "verified-path"})
                    self.assertEqual(
                        (output.parent / library.name).read_bytes(), library.read_bytes()
                    )
                    write_pe(output, executable)

                def run(command, *, cwd, env, check):
                    self.assertEqual(command, [str(cwd / "abi.exe")])
                    self.assertEqual((cwd / library.name).read_bytes(), library.read_bytes())
                    self.assertTrue(check)
                    if host == "arm64" and target == "x64":
                        error = PermissionError("abi.exe is still locked")
                        error.winerror = 5
                        cleanup.side_effect = [error, DEFAULT]

                def check_c():
                    binding_checks.check_c(
                        source=root / "main.c",
                        include_dirs=(),
                        library=library,
                        work_parent=work_parent,
                        argv=["--windows-architecture", target] if target else [],
                    )

                with (
                    patch.object(binding_checks.sys, "platform", "win32"),
                    patch.object(
                        binding_checks.windows,
                        "host_architecture",
                        return_value=host,
                        side_effect=None
                        if host
                        else ValueError("C acceptance requires native Python"),
                    ) as architecture,
                    patch.object(
                        binding_checks.windows,
                        "native_msvc_environment",
                        return_value=({"PATH": "verified-path"}, tools),
                    ) as native_environment,
                    patch.object(c_compiler, "compile_c", side_effect=compile_c) as build,
                    patch.object(binding_checks.subprocess, "run", side_effect=run) as launch,
                    patch.object(
                        tempfile.TemporaryDirectory,
                        "_rmtree",
                        wraps=tempfile.TemporaryDirectory._rmtree,
                    ) as cleanup,
                    patch.object(workspaces.time, "sleep") as sleep,
                    patch("builtins.print") as print_result,
                ):
                    if expected:
                        with self.assertRaisesRegex(ValueError, expected):
                            check_c()
                        launch.assert_not_called()
                    else:
                        check_c()
                        launch.assert_called_once()
                    architecture.assert_called_once()
                    if should_build:
                        native_environment.assert_called_once_with(host, target_architecture=target)
                        build.assert_called_once()
                    else:
                        native_environment.assert_not_called()
                        build.assert_not_called()
                    if expected is None and host == "arm64" and target == "x64":
                        sleep.assert_called_once()
                        print_result.assert_called_once_with(
                            "Windows x64 C fixture passed under emulation on ARM64."
                        )
                    else:
                        sleep.assert_not_called()
                        print_result.assert_not_called()
                self.assertEqual(list(work_parent.glob("c-consumer-*")), [])

    def test_explicit_windows_c_target_requires_a_windows_worker(self):
        with (
            patch.object(binding_checks.sys, "platform", "darwin"),
            patch.object(binding_checks.windows, "host_architecture") as host,
            patch.object(c_compiler, "compile_c") as build,
            patch.object(binding_checks.subprocess, "run") as launch,
            self.assertRaisesRegex(ValueError, "requires a Windows host"),
        ):
            binding_checks.check_c(
                source="abi.c",
                include_dirs=(),
                library="fixture",
                work_parent="unused",
                argv=["--windows-architecture", "x64"],
            )
        host.assert_not_called()
        build.assert_not_called()
        launch.assert_not_called()


class WindowsPublicationTests(unittest.TestCase):
    def fixture(self, root):
        (root / "ThirdParty").mkdir()
        (root / "ThirdParty/notice.txt").write_bytes(b"original\r\nnotice")
        (root / "LICENSE").write_text("retained")
        return SimpleNamespace(
            root=root,
            compiler="1.99.1",
            package="fixture-ffi",
            library="fixture",
            ffi=root / "crates/ffi",
            symbol_prefix="fixture_",
            retain_notices=Mock(),
            abi_versions=lambda windows: {"fixture_abi_version": 1},
            build_sources=lambda: {"runtime.rs": "first"},
            dependencies=lambda env, targets: fixture_dependencies(*targets),
        )

    def test_abi_probe_runs_the_selected_machine_with_the_staged_dll(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        output = root / "win-x64"
        output.mkdir()
        library, imports = (output / name for name in ("fixture.dll", "fixture.dll.lib"))
        library.write_bytes(b"known DLL")
        imports.write_bytes(b"known import library")
        builder = Mock()

        def compile_probe(executable, *, libraries, **kwargs):
            self.assertEqual(libraries[0].read_bytes(), imports.read_bytes())
            self.assertEqual(libraries[0].parent, executable.parent)
            self.assertTrue(kwargs["windows"])
            write_pe(executable, 0x8664)
            return executable

        builder.compile_abi_probe.side_effect = compile_probe

        def execute(executable, _env):
            staged = executable.with_name(library.name)
            self.assertEqual(staged.read_bytes(), library.read_bytes())
            staged.write_bytes(b"changed after execution")

        builder.run_abi_probe.side_effect = execute
        original_remove = tempfile.TemporaryDirectory._rmtree
        locked = PermissionError("Windows still holds the ABI probe executable")
        locked.winerror = 5

        def remove_after_release(name, *, ignore_errors):
            if Path(name).name.startswith("abi-") and not wait.called:
                raise locked
            return original_remove(name, ignore_errors=ignore_errors)

        with (
            patch.object(tempfile.TemporaryDirectory, "_rmtree", side_effect=remove_after_release),
            patch.object(workspaces.time, "sleep") as wait,
            self.assertRaisesRegex(ValueError, "changed during the ABI probe"),
        ):
            probe_abi(builder, library, imports, root / "msvc", {}, "x64")
        wait.assert_called_once()
        self.assertEqual(library.read_bytes(), b"known DLL")
        self.assertEqual(list(root.iterdir()), [output])

    def compile(self, command, *, graph, target, library, env):
        self.assertEqual(command[command.index("--target") + 1], target)
        self.assertIn("--message-format=json-render-diagnostics", command)
        out = Path(env["CARGO_TARGET_DIR"]) / target / "release"
        out.mkdir(parents=True, exist_ok=True)
        self.assertEqual(library, out / "fixture.dll")
        (out / "fixture.dll").write_bytes(b"DLL")
        (out / "fixture.dll.lib").write_bytes(b"import library")
        self.assertIn("+crt-static", env["CARGO_ENCODED_RUSTFLAGS"])
        return [FIXTURE_UNIT]

    def build(self, builder, **options):
        with (
            patch("sdk_tooling.windows.sys.platform", "win32"),
            patch(
                "sdk_tooling.windows.subprocess.check_output",
                return_value="rustc 1.99.1 fixture\nhost: aarch64-pc-windows-msvc\n",
            ),
            patch("sdk_tooling.windows.CargoDependencies") as dependencies,
            patch("sdk_tooling.windows.compile_units", side_effect=self.compile),
            patch("sdk_tooling.windows.probe_abi"),
        ):
            target = MSVC_TARGETS[options.get("architecture", "arm64")]
            dependencies.return_value.provenance.return_value = builder.dependencies({}, [target])
            return build_library(
                builder, {}, inspect=lambda path: {"exports": ["fixture_run"]}, **options
            )

    def test_two_products_keep_independent_receipts_and_verify_dependency_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.fixture(Path(temporary).resolve())
            parents = [
                builder.root / ".native-build/windows" / item for item in ("first", "second")
            ]
            for parent in parents:
                result = self.build(builder, output_root=parent)
                self.assertEqual(result, parent / "win-arm64")
                self.assertEqual(
                    builder.retain_notices.call_args.kwargs["windows_target"],
                    MSVC_TARGETS["arm64"],
                )
                self.assertEqual(
                    (result / "ThirdParty/notice.txt").read_bytes(), b"original\r\nnotice"
                )
                _, receipt = verify_artifact(builder, output_root=parent, environment={})
                self.assertFalse(receipt["windowsRuntimeTested"])
            builder.dependencies = lambda env, targets: {"source": "changed"}
            with self.assertRaisesRegex(ValueError, "dependency source"):
                verify_artifact(builder, output_root=parents[0], environment={})

    def test_publication_failure_restores_previous_artifact_and_extra_files_are_protected(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.fixture(Path(temporary).resolve())
            result = self.build(builder)
            original = hashes(files(result), result)
            with patch(
                "sdk_tooling.windows.verify_artifact", side_effect=ValueError("fixture rejection")
            ):
                with self.assertRaisesRegex(ValueError, "fixture rejection"):
                    self.build(builder)
            self.assertEqual(hashes(files(result), result), original)
            (result / "not-owned.txt").write_text("preserve")
            with self.assertRaisesRegex(ValueError, "outside its previous receipt"):
                self.build(builder)
            self.assertTrue((result / "not-owned.txt").exists())

    def test_staging_source_or_dependency_change_cannot_replace_candidate(self):
        for source in (True, False):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as temporary:
                builder = self.fixture(Path(temporary).resolve())
                result = self.build(builder)
                original = hashes(files(result), result)

                def stage(_):
                    if source:
                        builder.build_sources = lambda: {"runtime.rs": "changed"}
                    else:
                        builder.dependencies = lambda env, targets: {"source": "changed"}

                with self.assertRaisesRegex(ValueError, "changed during Windows staging"):
                    self.build(builder, stage_extra=stage)
                self.assertEqual(hashes(files(result), result), original)

    def test_build_requires_a_windows_worker_and_checkout_local_outputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            builder = self.fixture(Path(temporary).resolve())
            with (
                patch("sdk_tooling.windows.sys.platform", "darwin"),
                self.assertRaisesRegex(ValueError, "native Windows worker"),
            ):
                build_library(builder, {})
            with self.assertRaisesRegex(ValueError, "native build directory"):
                self.build(builder, output_root=builder.root / "user-files")

    def test_arm64_host_builds_x64_with_the_checked_rust_compiler_and_target_std(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        builder = self.fixture(root)
        env, tools = x64_msvc_fixture(root / "VC")
        env["RUSTC"] = "inherited-rustc"
        rustc = root / "rustc.exe"
        rustc.touch()
        std = root / "target-std"
        std.mkdir()
        arm64 = "aarch64-pc-windows-msvc"
        x64 = "x86_64-pc-windows-msvc"

        def compile(command, *, graph, target, library, env):
            self.assertEqual(command[:2], ["cargo", "build"])
            self.assertEqual(command[command.index("--target") + 1], x64)
            self.assertEqual(env["RUSTC"], str(rustc))
            self.assertEqual(env["RUSTUP_AUTO_INSTALL"], "0")
            self.assertEqual(
                env["CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER"], str(tools / "link.exe")
            )
            return self.compile(command, graph=graph, target=target, library=library, env=env)

        for host, compiler_host, target_installed, selected_tools, failure in (
            ("x64", arm64, False, tools, "ARM64"),
            ("arm64", arm64, False, root, "MSVC tools differ"),
            ("arm64", x64, False, tools, "pinned native Rust compiler"),
            ("arm64", arm64, False, tools, "Rust standard library"),
            ("arm64", arm64, True, tools, None),
        ):
            if target_installed:
                (std / "libstd-fixture.rlib").touch()
            with (
                self.subTest(failure=failure),
                patch("sdk_tooling.windows.sys.platform", "win32"),
                patch("sdk_tooling.windows.host_architecture", return_value=host),
                patch(
                    "sdk_tooling.windows.shutil.which",
                    side_effect=lambda name, *, path: str(
                        rustc if name == "rustc" else tools / name
                    ),
                ),
                patch(
                    "sdk_tooling.windows.subprocess.check_output",
                    side_effect=[f"rustc 1.99.1 fixture\nhost: {compiler_host}\n", str(std)],
                ) as probe,
                patch("sdk_tooling.windows.CargoDependencies") as dependencies,
                patch("sdk_tooling.windows.compile_units", side_effect=compile) as run,
                patch("sdk_tooling.windows.probe_abi") as abi_probe,
            ):
                dependencies.return_value.provenance.return_value = fixture_dependencies(x64)

                def build():
                    return build_library(
                        builder,
                        env,
                        selected_tools,
                        "x64",
                        native_host="arm64",
                        inspect=lambda path: {"exports": ["fixture_run"]},
                    )

                if failure:
                    with self.assertRaisesRegex(ValueError, failure):
                        build()
                    run.assert_not_called()
                    if host == "x64" or selected_tools != tools:
                        probe.assert_not_called()
                else:
                    result = build()
                    self.assertEqual(result.name, "win-x64")
                    self.assertEqual(
                        [call.args[0] for call in probe.call_args_list],
                        [
                            [str(rustc), "--version", "--verbose"],
                            [str(rustc), "--print", "target-libdir", "--target", x64],
                        ],
                    )
                    _, receipt = verify_artifact(builder, "x64")
                    self.assertEqual(
                        receipt["toolchain"], {"backend": "msvc", "host": arm64, "target": x64}
                    )
                    self.assertFalse(receipt["windowsRuntimeTested"])
                    self.assertEqual(receipt["abiProbeArchitecture"], "x64")
                    abi_probe.assert_called_once()


if __name__ == "__main__":
    unittest.main()
