"""Run binding fixtures declared by an SDK against its accepted artifacts."""

import argparse
import sys
import tomllib
from pathlib import Path

from . import consumers
from .adapters import tooling_command
from .sdk_checks import _boolean, _inside, _section, _string, _strings

KINDS = ("c", "dotnet", "nuget")


def command(kind, *arguments, root=None):
    return tooling_command("binding-consumer", kind, *arguments, root=root)


def _options(argv, *, windows_x64):
    if not windows_x64:
        return argv
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--windows-architecture", choices=("x64",))
    args, remaining = parser.parse_known_args(argv)
    if sys.platform == "win32":
        remaining.extend(("--windows-architecture", "x64"))
    elif args.windows_architecture is not None:
        parser.error("--windows-architecture requires Windows")
    return remaining


def _framework(root):
    paths = _section(root, "installed")
    framework = _inside(root, paths["c"]["framework"], "installed.c.framework")
    if framework.suffix != ".framework":
        raise ValueError("installed.c.framework must select a framework bundle")
    return framework / framework.stem


def run_consumer(kind, *, root, argv):
    root = Path(root).resolve(strict=True)
    config = _section(root, "bindings")
    argv = _options(
        argv, windows_x64=_boolean(config.get("windows_x64", False), "bindings.windows_x64")
    )
    work = root / ".native-build"

    if kind == "nuget":
        name = _string(config.get("nuget_id"), "bindings.nuget_id")
        fixtures = _strings(config.get("nuget_fixtures"), "bindings.nuget_fixtures")
        if not fixtures:
            raise ValueError("bindings.nuget_fixtures must select at least one fixture")
        for path in fixtures:
            consumers.check_nuget(
                package_id=name,
                fixture=_inside(root, path, "bindings.nuget_fixtures"),
                work_parent=work / "packages",
                argv=argv,
            )
        return

    ffi = _inside(root, config.get("ffi", "crates/ffi"), "bindings.ffi")
    if kind == "c":
        source = _inside(root, config.get("abi_fixture"), "bindings.abi_fixture")
        consumers.check_c(
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
    consumers.check_dotnet(
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
    run_consumer(args.kind, root=root, argv=args.options)


def add_commands(commands):
    parser = commands.add_parser("binding-consumer", help="Run an SDK's C, .NET, or NuGet fixture")
    parser.add_argument("kind", choices=KINDS)
    parser.add_argument(
        "options", nargs=argparse.REMAINDER, help="Arguments for the selected fixture"
    )
    parser.set_defaults(handler=_dispatch)
