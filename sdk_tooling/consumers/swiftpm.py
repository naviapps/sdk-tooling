"""Verify the SDK with an external SwiftPM package."""

import json
import os
import shutil
import subprocess
from pathlib import Path, PureWindowsPath

from ..workspaces import temporary_workspace

_SWIFT_DEVELOPER_INPUTS = {
    ".git",
    ".build",
    ".native-build",
    ".swiftpm",
    "__pycache__",
    "target",
    "bin",
    "obj",
    "build",
    "DerivedData",
    "node_modules",
    "tools",
    "crates",
    "sdk_tooling",
}
_SWIFT_CACHE_DIRECTORIES = {".git", ".build", ".native-build", ".swiftpm", "__pycache__"}
SWIFT_BUILD_SYSTEMS = ("native", "swiftbuild")


def _swift_copy_ignore(directory, names):
    return [
        name
        for name in names
        if name in _SWIFT_CACHE_DIRECTORIES and (Path(directory) / name).is_dir()
    ]


def _copy_swift_inputs(source, destination, inputs):
    for name in inputs:
        relative = Path(name)
        if (
            relative.is_absolute()
            or not relative.parts
            or ".." in relative.parts
            or relative.parts[0] in _SWIFT_DEVELOPER_INPUTS
        ):
            raise ValueError("Swift consumer inputs must exclude developer trees: " + str(name))
        origin = source / relative
        if not origin.resolve(strict=True).is_relative_to(source):
            raise ValueError("Swift consumer input escapes its source: " + str(name))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir() and not origin.is_symlink():
            shutil.copytree(
                origin,
                target,
                symlinks=True,
                ignore=_swift_copy_ignore,
            )
        else:
            shutil.copy2(origin, target, follow_symlinks=False)
    # Keep framework Versions/Current, resources and notices intact, but never
    # allow a link to make the original checkout part of this consumer build.
    for path in destination.rglob("*"):
        if path.is_symlink() and (
            Path(os.readlink(path)).is_absolute()
            or not path.resolve(strict=True).is_relative_to(destination)
        ):
            raise ValueError("Swift consumer link escapes its installed tree: " + str(path))


def _swift_policy_file(sources, destination):
    """Carry root dependency policy without a second copy of SDK pin metadata."""
    found = [path for path in sources if path.exists()]
    if not found:
        return False
    data = found[0].read_bytes()
    if any(path.read_bytes() != data for path in found[1:]):
        raise ValueError("Conflicting Swift consumer dependency policy: " + destination.name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return True


def _check_swift_dependencies(dependency, allowed_roots):
    path = Path(dependency["path"]).resolve(strict=True)
    if not any(path.is_relative_to(root) for root in allowed_roots):
        raise ValueError("Swift consumer dependency escapes its installed tree: " + str(path))
    for child in dependency["dependencies"]:
        _check_swift_dependencies(child, allowed_roots)


def _swift_environment(work, git_config):
    # Keep Xcode's developer home stable and isolate SwiftPM state and tool paths.
    env = {
        key: os.environ[key]
        for key in (
            "HOME",
            "CFFIXED_USER_HOME",
            "DEVELOPER_DIR",
            "LANG",
            "LC_ALL",
            "LC_CTYPE",
            "TZ",
            "TERM",
            "NO_COLOR",
            "HTTPS_PROXY",
            "HTTP_PROXY",
            "ALL_PROXY",
            "NO_PROXY",
        )
        if key in os.environ
    }
    for key, relative in {
        "XDG_CACHE_HOME": "cache",
        "XDG_CONFIG_HOME": "config",
        "TMPDIR": "tmp",
        "CLANG_MODULE_CACHE_PATH": "module-cache",
        "SWIFT_MODULECACHE_PATH": "module-cache",
    }.items():
        path = work / relative
        path.mkdir(exist_ok=True)
        env[key] = str(path)
    configuration = work / "gitconfig"
    configuration.write_bytes(Path(git_config).read_bytes() if git_config else b"")
    return dict(
        env,
        PATH="/usr/bin:/bin:/usr/sbin:/sbin",
        GIT_CONFIG_GLOBAL=str(configuration),
        GIT_CONFIG_NOSYSTEM="1",
        GIT_TERMINAL_PROMPT="0",
    )


def run_swift_consumer(
    *,
    root,
    inputs,
    fixture,
    executables,
    work_parent=None,
    verify_dependencies=True,
    git_config=None,
    mirror_config=None,
    build_system=None,
    warnings_as_errors=True,
    runner=None,
):
    """Build/run an SDK-owned external package against a stripped installable tree.

    The fixture owns product selection and assertions. Contributor checks run the
    selected products. Distribution checks also validate the dependency graph.
    """
    root = Path(root).resolve(strict=True)
    fixture = Path(fixture).resolve(strict=True)
    if build_system is not None and build_system not in SWIFT_BUILD_SYSTEMS:
        raise ValueError("Unsupported SwiftPM build system: " + str(build_system))
    if mirror_config is not None:
        mirror_config = Path(mirror_config).resolve(strict=True)
    verify_dependencies = verify_dependencies or git_config is not None or mirror_config is not None
    executables = tuple(executables)
    if (
        not executables
        or any(
            not isinstance(name, str)
            or not name
            or name in {".", ".."}
            or name.startswith("-")
            or "/" in name
            or "\\" in name
            or PureWindowsPath(name).drive
            or any(ord(char) < 32 for char in name)
            for name in executables
        )
        or len(executables) != len(set(executables))
    ):
        raise ValueError("Declare distinct Swift executable names without directory components")
    if work_parent is not None:
        work_parent = Path(work_parent).resolve()
        work_parent.mkdir(parents=True, exist_ok=True)
    run = runner or subprocess.run
    with temporary_workspace(work_parent, prefix="swift-consumer-") as directory:
        work = directory.resolve()
        sdk, consumer = work / "sdk", work / "consumer"
        sdk.mkdir()
        consumer.mkdir()
        _copy_swift_inputs(root, sdk, inputs)
        fixture_inputs = ("Package.swift", "Sources")
        if verify_dependencies:
            fixture_inputs = tuple(
                path.name for path in fixture.iterdir() if path.name not in _SWIFT_DEVELOPER_INPUTS
            )
        _copy_swift_inputs(fixture, consumer, fixture_inputs)
        if not (sdk / "Package.swift").is_file() or not (consumer / "Package.swift").is_file():
            raise ValueError("Both the installable SDK and consumer need Package.swift")
        pinned = False
        if verify_dependencies:
            # A dependency's Package.resolved does not control the SwiftPM root.
            pins = consumer / "Package.resolved"
            pinned = _swift_policy_file([root / "Package.resolved", pins], pins)
            mirrors = Path(".swiftpm/configuration/mirrors.json")
            _swift_policy_file(
                [root / mirrors, fixture / mirrors, *([mirror_config] if mirror_config else [])],
                consumer / mirrors,
            )
            git_config = git_config or os.environ.get("GIT_CONFIG_GLOBAL")
        env = _swift_environment(work, git_config)
        scratch = work / "scratch"
        swift = ("/usr/bin/xcrun", "swift")
        options = [
            "--scratch-path",
            str(scratch),
            "--cache-path",
            str(work / "swiftpm-cache"),
            "--config-path",
            str(work / "swiftpm-config"),
            "--security-path",
            str(work / "swiftpm-security"),
            "--disable-dependency-cache",
            "--manifest-cache",
            "none",
            *(["--build-system", build_system] if build_system else []),
            "-Xswiftc",
            "-strict-concurrency=complete",
            *(["-Xswiftc", "-warnings-as-errors"] if warnings_as_errors else []),
            *(["--force-resolved-versions"] if pinned else []),
        ]
        execution = {"cwd": consumer, "env": env, "check": True}
        if not verify_dependencies:
            for name in executables:
                run([*swift, "run", *options, name], **execution)
            return

        graph = run(
            [*swift, "package", *options, "show-dependencies", "--format", "json"],
            stdout=subprocess.PIPE,
            text=True,
            **execution,
        )
        # An absolute .package(path:) would otherwise bypass source-tree isolation.
        _check_swift_dependencies(json.loads(graph.stdout), (consumer, sdk, scratch))
        command = [*swift, "build", *options]
        run(command, **execution)
        result = run(
            [*command, "--show-bin-path"],
            stdout=subprocess.PIPE,
            text=True,
            **execution,
        )
        binary = Path(result.stdout.strip()).resolve(strict=True)
        if not binary.is_relative_to(scratch):
            raise ValueError("Swift consumer binary path must remain inside its fresh scratch tree")
        for name in executables:
            run([str(binary / name)], **execution)
