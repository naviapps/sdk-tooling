"""Run consumer checks from an SDK-owned description."""

import json
import sys
import tomllib
from copy import copy
from pathlib import Path, PurePosixPath, PureWindowsPath

from .consumers import nuget, rust_c, rust_git, swiftpm

CONFIGURATION = "sdk-checks.toml"


def _table(value, label):
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a TOML table")
    return value


def _section(root, name):
    path = root / CONFIGURATION
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Expected an ordinary {CONFIGURATION} in the SDK root")
    config = tomllib.loads(path.read_text(encoding="utf-8"))
    return _table(config.get(name), f"[{name}]")


def _string(value, label):
    if not isinstance(value, str) or not value or any(ord(char) < 32 for char in value):
        raise ValueError(f"{label} must be a nonempty string")
    return value


def _strings(value, label):
    if not isinstance(value, list):
        raise ValueError(f"{label} must be an array of strings")
    return tuple(_string(item, label) for item in value)


def _boolean(value, label):
    if type(value) is not bool:
        raise ValueError(f"{label} must be a boolean")
    return value


def _relative(value, label):
    value = _string(value, label)
    path = PurePosixPath(value)
    if (
        not path.parts
        or path.is_absolute()
        or path.as_posix() != value
        or ".." in path.parts
        or "\\" in value
        or PureWindowsPath(value).drive
    ):
        raise ValueError(f"{label} must be a canonical relative path")
    return value


def _inside(root, value, label):
    path = root / _relative(value, label)
    if not path.resolve().is_relative_to(root):
        raise ValueError(f"{label} escapes the SDK checkout")
    return path


def swift_command(args, *, root):
    if sys.platform != "darwin":
        raise ValueError("Swift native consumer acceptance requires macOS")
    root = Path(root).resolve(strict=True)
    config = _section(root, "swift")
    inputs = tuple(
        _relative(path, "swift.inputs") for path in _strings(config.get("inputs"), "swift.inputs")
    )
    fixture = _inside(root, config.get("fixture", "fixtures/consumers/swift"), "swift.fixture")
    if ("executable" in config) == ("executables" in config):
        raise ValueError("Declare swift.executable or swift.executables")
    verify_dependencies = _boolean(
        config.get("verify_dependencies", False), "swift.verify_dependencies"
    )
    swiftpm.run_swift_consumer(
        root=root,
        inputs=inputs,
        fixture=fixture,
        executables=_strings(
            config.get("executables", [config.get("executable")]), "swift executable names"
        ),
        verify_dependencies=verify_dependencies or args.distribution,
        git_config=args.git_config,
        mirror_config=args.mirror_config,
        build_system=args.build_system or config.get("build_system"),
        warnings_as_errors=not args.allow_warnings,
    )


def _c_fixture(config):
    if "c" not in config:
        return {}
    c = _table(config["c"], "[rust_c.c]")
    options = {
        key: _relative(c[key], "rust_c.c." + key)
        for key in ("framework", "header", "c_fixture", "c_receipt")
        if key in c
    }
    for key in ("framework", "header"):
        if key not in options:
            raise ValueError(f"rust_c.c.{key} is required")

    if "options" in c:
        report = _table(c["options"], "[rust_c.c.options]")

        def file_arguments(client, owned):
            options_file = client / "file-options.json"
            payload = {
                key: value.replace("{owned}", str(owned)) if isinstance(value, str) else value
                for key, value in report.items()
            }
            options_file.write_text(json.dumps(payload), encoding="utf-8")
            return (options_file,)

        options["c_arguments"] = file_arguments
    return options


def _git_dependencies(root, config):
    names = _strings(config.get("rust_git_dependencies", []), "rust_c.rust_git_dependencies")
    if not names:
        return {}
    workspace = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))["workspace"]
    return {name: workspace["dependencies"][name] for name in names}


def _registry_dependencies(root, config):
    table = _table(
        config.get("rust_registry_dependencies", {}), "rust_c.rust_registry_dependencies"
    )
    if not table:
        return {}
    lock = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))
    dependencies = {}
    for name, features in table.items():
        (entry,) = (
            row
            for row in lock["package"]
            if row["name"] == name and row.get("source") == rust_git.PUBLIC_REGISTRY
        )
        dependencies[name] = {
            "version": "=" + entry["version"],
            "features": list(_strings(features, "registry dependency features")),
        }
    return dependencies


def rust_c_command(args, *, root):
    root = Path(root).resolve(strict=True)
    config = _section(root, "rust_c")
    rust_c.check_installed(
        root=root,
        git_url=_string(config.get("git_url"), "rust_c.git_url"),
        rust_packages=_strings(config.get("rust_packages"), "rust_c.rust_packages"),
        rust_git_dependencies=_git_dependencies(root, config),
        rust_registry_dependencies=_registry_dependencies(root, config),
        args=args,
        **_c_fixture(config),
    )


def nuget_command(args, *, root):
    root = Path(root).resolve(strict=True)
    config = _section(root, "nuget")
    package_id = _string(config.get("package_id"), "nuget.package_id")
    fixtures = _strings(config.get("fixtures"), "nuget.fixtures")
    if not fixtures:
        raise ValueError("nuget.fixtures must select at least one fixture")
    if _boolean(config.get("windows_x64", False), "nuget.windows_x64"):
        if args.windows_architecture not in (None, "x64"):
            raise ValueError("This SDK tests NuGet packages on Windows x64")
        if sys.platform == "win32":
            args = copy(args)
            args.windows_architecture = "x64"
        elif args.windows_architecture is not None:
            raise ValueError("--windows-architecture requires a Windows host")
    for fixture in fixtures:
        nuget.check_nuget(
            package_id=package_id,
            fixture=_inside(root, fixture, "nuget.fixtures"),
            work_parent=root / ".native-build/packages",
            args=args,
        )


def add_commands(commands):
    swift = commands.add_parser("swift-consumer", help="Check the SDK's external Swift package")
    swift.add_argument("--distribution", action="store_true", help="Verify the dependency graph")
    swift.add_argument("--git-config", type=Path, help="Explicit job-private Git configuration")
    swift.add_argument("--mirror-config", type=Path, help="Explicit SwiftPM mirrors.json")
    swift.add_argument(
        "--build-system", choices=swiftpm.SWIFT_BUILD_SYSTEMS, help="SwiftPM build system"
    )
    swift.add_argument("--allow-warnings", action="store_true", help="Allow Swift warnings")
    swift.set_defaults(handler=swift_command)
    rust_c_cli = commands.add_parser(
        "rust-c-consumers", help="Check Rust from Git and C against a copied framework"
    )
    rust_c.add_arguments(rust_c_cli)
    rust_c_cli.set_defaults(handler=rust_c_command)
    nuget_cli = commands.add_parser(
        "nuget-consumer", help="Restore and run a standalone .NET client from local packages"
    )
    nuget.add_arguments(nuget_cli)
    nuget_cli.set_defaults(handler=nuget_command)
