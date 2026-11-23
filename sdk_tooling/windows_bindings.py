"""Build x64 Rust DLLs and run SDK-owned .NET contributor fixtures."""

import json
import re
import subprocess
from pathlib import Path

from . import dotnet, windows
from .artifacts import stage_native
from .cargo_build import compiler_artifacts
from .check_options import select_dotnet_host
from .sdk_checks import _inside, _section, _strings
from .workspaces import temporary_workspace


def _build_dlls(root, env, packages):
    metadata = json.loads(
        subprocess.check_output(
            ["cargo", "metadata", "--format-version", "1", "--locked", "--no-deps"],
            cwd=root,
            env=env,
        )
    )
    members = set(metadata["workspace_members"])
    selected = {
        package["id"]: package
        for package in metadata["packages"]
        if package["id"] in members and package["name"] in packages
    }
    if len(selected) != len(packages) or {p["name"] for p in selected.values()} != set(packages):
        raise ValueError("Select distinct Rust workspace packages for the Windows fixtures")

    command = [
        "cargo",
        "build",
        "--locked",
        "--target",
        windows.MSVC_TARGETS["x64"],
        "--message-format=json-render-diagnostics",
        *(f"--package={package}" for package in packages),
    ]
    libraries = {}
    for artifact in compiler_artifacts(command, root=root, env=env):
        identity = artifact["package_id"]
        if identity not in selected or "cdylib" not in artifact["target"]["crate_types"]:
            continue
        outputs = [Path(name) for name in artifact["filenames"] if name.lower().endswith(".dll")]
        if (
            identity in libraries
            or artifact["manifest_path"] != selected[identity]["manifest_path"]
            or len(outputs) != 1
            or not outputs[0].is_absolute()
        ):
            raise ValueError("Expected one Cargo-reported DLL for " + selected[identity]["name"])
        library = outputs[0].resolve(strict=True)
        windows.require_pe_machine(library, "x64")
        libraries[identity] = library
    if libraries.keys() != selected.keys():
        raise ValueError("Cargo did not report a DLL for each Windows fixture package")
    if len({path.name.casefold() for path in libraries.values()}) != len(libraries):
        raise ValueError("Windows fixture DLL names must be distinct")
    return tuple(libraries.values())


def _run(args, *, root):
    root = root.resolve(strict=True)
    packages, paths = args.package, args.project
    if not packages and not paths:
        config = _section(root, "windows_bindings")
        if config.keys() - {"packages", "projects"}:
            raise ValueError("Unknown setting in [windows_bindings]")
        packages = _strings(config.get("packages"), "windows_bindings.packages")
        paths = (
            _strings(config["projects"], "windows_bindings.projects")
            if "projects" in config
            else (_section(root, "bindings").get("dotnet_project"),)
        )
    if not packages or not paths or len(set(paths)) != len(paths):
        raise ValueError("Select Rust packages and distinct .NET projects for the Windows fixtures")
    if args.native_property is not None and (
        len(packages) != 1
        or len(paths) != 1
        or not re.fullmatch(r"[a-zA-Z_][a-zA-Z0-9_]*", args.native_property)
    ):
        raise ValueError("A native MSBuild property requires one Rust package and one .NET fixture")
    projects = [_inside(root, path, "Windows binding project") for path in paths]
    if any(project.suffix != ".csproj" or not project.is_file() for project in projects):
        raise ValueError("Select existing .csproj files for the Windows fixtures")

    env = dotnet.subprocess_environment()
    executable, _, emulated = select_dotnet_host(env, "x64")
    libraries = _build_dlls(root, env, packages)
    if args.native_property is not None:
        # Older consumers let the managed project copy its native dependency.
        dotnet.run_managed(
            executable,
            projects[0],
            env=env,
            cwd=root,
            properties={args.native_property: libraries[0]},
            arguments=args.argument,
        )
    else:
        for project in projects:
            with temporary_workspace(None, prefix="windows-bindings-") as output:
                dotnet.run_command(
                    [
                        executable,
                        "build",
                        project,
                        "--configuration",
                        "Release",
                        "--output",
                        output,
                    ],
                    cwd=root,
                    env=env,
                    runner=None,
                )
                for library in libraries:
                    stage_native(library, output / library.name)
                dotnet.run_command(
                    [executable, output / (project.stem + ".dll"), *args.argument],
                    cwd=root,
                    env=env,
                    runner=None,
                )
    if emulated:
        print("Windows x64 binding fixtures passed under emulation on ARM64.")


def add_command(commands):
    parser = commands.add_parser(
        "test-windows-bindings", help="Build x64 DLLs and test the .NET contributor fixtures"
    )
    parser.add_argument("--package", action="append", default=[], help="Rust workspace package")
    parser.add_argument("--project", action="append", default=[], help=".NET fixture project")
    parser.add_argument("--native-property", help="MSBuild property that stages the DLL")
    parser.add_argument("--argument", action="append", default=[], help=".NET fixture argument")
    parser.set_defaults(handler=_run)
