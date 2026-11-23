"""Run consumer and documentation checks from an SDK-owned description."""

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

from . import consumers, docs, installed_consumers

CONFIGURATION = "sdk-checks.toml"
_SWIFT_BUILD_SYSTEMS = ("native", "swiftbuild")
_SWIFT_ENVIRONMENT = (
    "HOME",
    "CFFIXED_USER_HOME",
    "DEVELOPER_DIR",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
)


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


def _add_swift_arguments(parser):
    parser.add_argument("--distribution", action="store_true", help="Verify the dependency graph")
    parser.add_argument("--git-config", type=Path, help="Explicit job-private Git configuration")
    parser.add_argument("--mirror-config", type=Path, help="Explicit SwiftPM mirrors.json")
    parser.add_argument("--build-system", choices=_SWIFT_BUILD_SYSTEMS, help="SwiftPM build system")
    parser.add_argument("--allow-warnings", action="store_true", help="Allow Swift warnings")


def _swift_smoke(root, inputs, fixture, executables, *, build_system, warnings_as_errors):
    with tempfile.TemporaryDirectory(prefix="swift-consumer-") as directory:
        work = Path(directory).resolve()
        sdk, client = work / "sdk", work / "consumer"
        sdk.mkdir()
        client.mkdir()
        consumers._copy_swift_inputs(root, sdk, inputs)
        consumers._copy_swift_inputs(fixture, client, ("Package.swift", "Sources"))
        if not (sdk / "Package.swift").is_file():
            raise ValueError("The installable SDK needs Package.swift")
        env = {key: os.environ[key] for key in _SWIFT_ENVIRONMENT if key in os.environ}
        env.update(
            PATH="/usr/bin:/bin:/usr/sbin:/sbin",
            GIT_CONFIG_GLOBAL=os.devnull,
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
        )
        command = [
            "/usr/bin/xcrun",
            "swift",
            "run",
            "--cache-path",
            str(work / "cache"),
            "--config-path",
            str(work / "config"),
            "--security-path",
            str(work / "security"),
            "--disable-dependency-cache",
            *(["--build-system", build_system] if build_system else []),
            "-Xswiftc",
            "-strict-concurrency=complete",
            *(["-Xswiftc", "-warnings-as-errors"] if warnings_as_errors else []),
        ]
        for executable in executables:
            subprocess.run([*command, executable], cwd=client, env=env, check=True)


def swift_command(args, *, root):
    if sys.platform != "darwin":
        raise ValueError("Swift native consumer acceptance requires macOS")
    root = Path(root).resolve(strict=True)
    config = _section(root, "swift")
    inputs = tuple(
        _relative(path, "swift.inputs") for path in _strings(config.get("inputs"), "swift.inputs")
    )
    fixture = _inside(root, config.get("fixture", "fixtures/swift-consumer"), "swift.fixture")
    if ("executable" in config) == ("executables" in config):
        raise ValueError("Declare swift.executable or swift.executables")
    names = config.get("executables", [config.get("executable")])
    executables = tuple(
        _relative(name, "swift executable") for name in _strings(names, "swift executable names")
    )
    if (
        not executables
        or len(executables) != len(set(executables))
        or any("/" in name or name.startswith("-") for name in executables)
    ):
        raise ValueError("Declare distinct Swift executable names without directory components")
    build_system = args.build_system or config.get("build_system")
    if build_system is not None and build_system not in _SWIFT_BUILD_SYSTEMS:
        raise ValueError("swift.build_system must be native or swiftbuild")
    settings = {
        "build_system": build_system,
        "warnings_as_errors": not args.allow_warnings,
    }
    verify_dependencies = _boolean(
        config.get("verify_dependencies", False), "swift.verify_dependencies"
    )
    if verify_dependencies or args.distribution or args.git_config or args.mirror_config:
        consumers.run_swift_consumer(
            root=root,
            inputs=inputs,
            fixture=fixture,
            executables=executables,
            git_config=args.git_config,
            mirror_config=args.mirror_config,
            **settings,
        )
    else:
        _swift_smoke(root, inputs, fixture, executables, **settings)


def _validate_declaration_docs(source_root, graphs):
    missing = []
    for graph in graphs:
        for symbol in graph.get("symbols", []):
            try:
                location = Path.from_uri(symbol.get("location", {}).get("uri", ""))
            except ValueError:
                continue
            if not location.is_relative_to(source_root):
                continue
            kind = symbol["kind"]["identifier"]
            # Extension containers and Equatable witnesses add no independent API contract.
            if kind == "swift.extension" or (
                kind == "swift.func.op" and symbol["names"]["title"] == "==(_:_:)"
            ):
                continue
            if not any(
                line["text"].strip() for line in symbol.get("docComment", {}).get("lines", [])
            ):
                missing.append(".".join(symbol["pathComponents"]))
    if missing:
        raise ValueError(
            "Missing public Swift declaration documentation: " + ", ".join(sorted(set(missing)))
        )


def docs_command(args, *, root):
    root = Path(root).resolve(strict=True)
    config = _section(root, "docs")
    output = args.output or _inside(root, config.get("output", "build/docs"), "docs.output")
    patterns = _strings(config.get("catalogs", ["Sources/**/*.docc"]), "docs.catalogs")
    catalogs = []
    for pattern in patterns:
        matches = sorted(root.glob(_relative(pattern, "docs.catalogs")))
        if not matches or any(
            path.suffix != ".docc" or not path.is_dir() or not path.resolve().is_relative_to(root)
            for path in matches
        ):
            raise ValueError("docs.catalogs must select SDK DocC directories: " + pattern)
        catalogs.extend(matches)
    require_docs = _boolean(
        config.get("require_declaration_docs", False), "docs.require_declaration_docs"
    )

    def validate(_module, graphs):
        _validate_declaration_docs(root / "Sources", graphs)

    docs.check_catalogs(
        root,
        tuple(dict.fromkeys(catalogs)),
        output=output,
        bundle_identifier_prefix=_string(
            config.get("bundle_identifier_prefix", "com.naviapps."), "docs.bundle_identifier_prefix"
        ),
        graph_arguments=_strings(config.get("graph_arguments", []), "docs.graph_arguments"),
        symbol_validator=validate if require_docs else None,
        retain_archives=_boolean(config.get("retain_archives", False), "docs.retain_archives"),
    )


def _c_fixture(config):
    if "c" not in config:
        return {}
    c = _table(config["c"], "[installed.c]")
    options = {
        key: _relative(c[key], "installed.c." + key)
        for key in ("framework", "header", "c_fixture", "c_receipt")
        if key in c
    }
    for key in ("framework", "header"):
        if key not in options:
            raise ValueError(f"installed.c.{key} is required")

    if "options" in c:
        report = _table(c["options"], "[installed.c.options]")

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
    names = _strings(config.get("rust_git_dependencies", []), "installed.rust_git_dependencies")
    if not names:
        return {}
    workspace = tomllib.loads((root / "Cargo.toml").read_text(encoding="utf-8"))["workspace"]
    return {name: workspace["dependencies"][name] for name in names}


def _registry_dependencies(root, config):
    table = _table(
        config.get("rust_registry_dependencies", {}), "installed.rust_registry_dependencies"
    )
    if not table:
        return {}
    lock = tomllib.loads((root / "Cargo.lock").read_text(encoding="utf-8"))
    dependencies = {}
    for name, features in table.items():
        (entry,) = (
            row
            for row in lock["package"]
            if row["name"] == name and row.get("source") == installed_consumers._PUBLIC_REGISTRY
        )
        dependencies[name] = {
            "version": "=" + entry["version"],
            "features": list(_strings(features, "registry dependency features")),
        }
    return dependencies


def installed_command(args, *, root):
    root = Path(root).resolve(strict=True)
    config = _section(root, "installed")
    installed_consumers.check_installed(
        root=root,
        git_url=_string(config.get("git_url"), "installed.git_url"),
        rust_packages=_strings(config.get("rust_packages"), "installed.rust_packages"),
        rust_git_dependencies=_git_dependencies(root, config),
        rust_registry_dependencies=_registry_dependencies(root, config),
        args=args,
        **_c_fixture(config),
    )


def add_commands(commands):
    swift = commands.add_parser("swift-consumer", help="Check the SDK's external Swift package")
    _add_swift_arguments(swift)
    swift.set_defaults(handler=swift_command)
    docc = commands.add_parser("docs", help="Validate SDK DocC catalogs against public symbols")
    docc.add_argument("--output", type=Path, help="Override the SDK documentation output")
    docc.set_defaults(handler=docs_command)
    installed = commands.add_parser(
        "installed-consumers", help="Check independently installed Rust/C clients"
    )
    installed_consumers.add_arguments(installed)
    installed.set_defaults(handler=installed_command)
