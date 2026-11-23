"""Check SDK binding code against its accepted native libraries."""

import argparse
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

from . import artifacts, c_compiler, windows
from . import dotnet as dotnet_tools
from .adapters import tooling_command
from .check_options import (
    add_windows_architecture_argument,
    select_dotnet_host,
    windows_x64_options,
)
from .dotnet import subprocess_environment
from .sdk_checks import _boolean, _inside, _section
from .workspaces import temporary_workspace

KINDS = ("c", "dotnet")


def command(kind, *arguments, root=None):
    return tooling_command("check-binding", kind, *arguments, root=root)


def check_c(*, source, include_dirs, library, work_parent, argv=None):
    parser = argparse.ArgumentParser(
        description="Compile and execute this SDK's existing C ownership fixture."
    )
    parser.add_argument("--library", type=Path, help="Accepted framework Mach-O or Windows DLL")
    add_windows_architecture_argument(parser)
    args = parser.parse_args(argv)
    host = windows.host_architecture() if sys.platform == "win32" else None
    if args.windows_architecture and host is None:
        raise ValueError("--windows-architecture requires a Windows host")
    architecture = args.windows_architecture or host
    emulated = (host, architecture) == ("arm64", "x64")
    if host != architecture and not emulated:
        raise ValueError("The C fixture requires a matching native Windows host or x64 emulation")
    library = (args.library or Path(library)).resolve(strict=True)
    work_parent = Path(work_parent).resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    with temporary_workspace(work_parent, prefix="c-consumer-") as work:
        if architecture:
            import_library = library.with_suffix(".dll.lib")
            if not import_library.is_file():
                raise ValueError("The accepted DLL's MSVC import library is required")
            staged = artifacts.stage_native(library, work / library.name)
            windows.require_pe_machine(staged, architecture)
            env, tools = windows.native_msvc_environment(
                host, target_architecture=args.windows_architecture
            )
            executable = work / "abi.exe"
            c_compiler.compile_c(
                source,
                output=executable,
                compiler=(tools / "cl.exe",),
                style="msvc",
                standard="c11",
                include_dirs=include_dirs,
                libraries=(import_library,),
                env=env,
                cwd=work,
            )
            windows.require_pe_machine(executable, architecture)
            env["PATH"] = str(library.parent) + os.pathsep + env.get("PATH", "")
        elif sys.platform == "darwin":
            env = dict(os.environ)
            framework = next(
                (path for path in library.parents if path.suffix == ".framework"), None
            )
            rpath = framework.parent if framework else library.parent
            executable = work / "abi"
            c_compiler.compile_c(
                source,
                output=executable,
                compiler=("xcrun", "clang"),
                standard="c11",
                include_dirs=include_dirs,
                libraries=(library,),
                flags=("-Wl,-rpath," + str(rpath),),
                env=env,
                cwd=work,
            )
        else:
            raise ValueError("C acceptance requires native macOS or Windows")
        subprocess.run([str(executable)], cwd=work, env=env, check=True)
    if emulated:
        print("Windows x64 C fixture passed under emulation on ARM64.")


def check_dotnet(
    *, project, nuget_config, framework, library_name, windows_root, work_parent, cwd, argv=None
):
    parser = argparse.ArgumentParser(
        description="Run this SDK's existing managed fixture against accepted native bytes."
    )
    parser.add_argument("--native-root", type=Path)
    parser.add_argument(
        "--package-source", action="append", default=[], help="Private/local NuGet source"
    )
    add_windows_architecture_argument(parser)
    args = parser.parse_args(argv)
    if sys.platform not in ("darwin", "win32"):
        raise ValueError("Managed native acceptance requires macOS or Windows")
    env = subprocess_environment()
    env["DOTNET_CLI_UI_LANGUAGE"] = "en"
    dotnet, architecture, emulated = select_dotnet_host(env, args.windows_architecture)
    project = Path(project).resolve()
    work_parent = Path(work_parent).resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    with temporary_workspace(work_parent, prefix="managed-consumer-") as work:
        env["NUGET_PACKAGES"] = str(work / "packages")
        output = project.parent / "bin/Release/net10.0"
        properties = {"UseSharedCompilation": "false"}
        if architecture is not None:
            library = (args.native_root or Path(windows_root) / f"win-{architecture}") / (
                library_name + ".dll"
            )
        if args.windows_architecture is not None:
            # Freeze the exact DLL before restoring packages or compiling the fixture.
            library = artifacts.stage_native(library, work / "native" / library.name)
            windows.require_pe_machine(library, architecture)
            rid = f"win-{architecture}"
            properties.update(RuntimeIdentifier=rid, UseAppHost="false")
            output /= rid
        dotnet_tools.build_managed(
            dotnet,
            project,
            env=env,
            cwd=cwd,
            nuget_config=nuget_config,
            sources=args.package_source,
            properties=properties,
            verify_style=True,
        )
        if architecture is None:
            artifacts.stage_native(framework, work / ("lib" + library_name + ".dylib"))
            env["DYLD_LIBRARY_PATH"] = str(work)
        else:
            # Windows resolves the native dependency beside the managed assembly.
            # Stage both in a disposable copy so acceptance cannot alter build outputs.
            runtime = work / "app"
            shutil.copytree(output, runtime)
            artifacts.stage_native(library, runtime / library.name)
            output = runtime
        subprocess.run(
            [dotnet, str(output / (project.stem + ".dll"))], cwd=cwd, env=env, check=True
        )
    if emulated:
        print("Windows x64 managed fixture passed under emulation on ARM64.")


def _framework(root):
    paths = _section(root, "rust_c")
    framework = _inside(root, paths["c"]["framework"], "rust_c.c.framework")
    if framework.suffix != ".framework":
        raise ValueError("rust_c.c.framework must select a framework bundle")
    return framework / framework.stem


def run_check(kind, *, root, argv):
    root = Path(root).resolve(strict=True)
    config = _section(root, "bindings")
    argv = windows_x64_options(
        argv, windows_x64=_boolean(config.get("windows_x64", False), "bindings.windows_x64")
    )
    work = root / ".native-build"

    ffi = _inside(root, config.get("ffi", "crates/ffi"), "bindings.ffi")
    if kind == "c":
        source = _inside(root, config.get("abi_fixture"), "bindings.abi_fixture")
        check_c(
            source=source,
            include_dirs=(ffi / "include",),
            library=_framework(root),
            work_parent=work,
            argv=argv,
        )
        print(f"{root.name} C ABI fixture passed.")
        return

    manifest = tomllib.loads((ffi / "Cargo.toml").read_text(encoding="utf-8"))
    name = manifest["package"]["name"].replace("-", "_")
    check_dotnet(
        project=_inside(root, config.get("dotnet_project"), "bindings.dotnet_project"),
        nuget_config=_inside(
            root,
            config.get("nuget_config", "bindings/dotnet/NuGet.Config"),
            "bindings.nuget_config",
        ),
        framework=_framework(root),
        library_name=manifest.get("lib", {}).get("name", name),
        windows_root=work / "windows",
        work_parent=work / "managed",
        cwd=root,
        argv=argv,
    )


def _dispatch(args, *, root):
    run_check(args.kind, root=root, argv=args.options)


def add_commands(commands):
    parser = commands.add_parser("check-binding", help="Run an SDK's C ABI or .NET binding fixture")
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument(
        "options", nargs=argparse.REMAINDER, help="Arguments for the selected fixture"
    )
    parser.set_defaults(handler=_dispatch)
