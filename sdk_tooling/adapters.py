"""Execute SDK-owned Python fixtures with the selected SDK Tooling package."""

import os
import re
import subprocess
import sys
import tomllib
from functools import partial
from importlib import import_module
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from uuid import uuid4

from .artifacts import hashes
from .manifest import LEGACY_MANIFESTS, MANIFEST
from .quality import EXCLUDED

_TOOLING_ROOT = Path(__file__).resolve().parents[1]
_DEFAULT_TOOLING_REPOSITORY = "naviapps/sdk-tooling"
GITHUB_REPOSITORY = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
_FIXTURE_SOURCE_EXTENSIONS = {
    ".c",
    ".cs",
    ".csproj",
    ".h",
    ".in",
    ".json",
    ".py",
    ".rs",
    ".swift",
    ".toml",
}
_FIXTURE_BOOTSTRAP = """
from pathlib import Path
import runpy
import sys

tooling, sdk, method, *arguments = sys.argv[1:]
sys.path[:0] = [tooling, sdk]
import sdk_tooling

if method == "-m":
    if not arguments:
        raise SystemExit("A Python module is required")
    sys.argv = arguments
    runpy.run_module(arguments[0], run_name="__main__", alter_sys=True)
else:
    path = Path(sdk, method).resolve(strict=True)
    if not path.is_relative_to(sdk) or not path.is_file():
        raise SystemExit("Python fixture must be an ordinary file inside the SDK")
    sys.path.insert(0, str(path.parent))
    sys.argv = [str(path), *arguments]
    runpy.run_path(str(path), run_name="__main__")
"""


def python_command(root, *arguments):
    if not arguments:
        raise ValueError("Select an SDK Python script or -m module")
    return [
        sys.executable,
        "-I",
        "-B",
        "-S",
        "-c",
        _FIXTURE_BOOTSTRAP,
        str(_TOOLING_ROOT),
        str(Path(root).resolve(strict=True)),
        *map(str, arguments),
    ]


def tooling_command(*arguments, root=None):
    return python_command(root or Path.cwd(), "-m", "sdk_tooling.__main__", *arguments)


def load(name):
    return _load("sdk_tooling", name)


def _load(package, name):
    if not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", name):
        raise ValueError("Invalid SDK Tooling module")
    return import_module(f"{package}.{name}")


def tooling_task_revision(configuration, url):
    """Require the exact immutable command selecting a Tooling checkout."""
    tasks = configuration.get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("SDK mise tasks must be a table")
    task = tasks.get("sdk-tooling")
    command = task.get("run") if isinstance(task, dict) else None
    pattern = rf"uvx --from git\+{re.escape(url)}@([a-f0-9]{{40}}) sdk-tooling"
    match = re.fullmatch(pattern, command) if isinstance(command, str) else None
    if match is None:
        raise ValueError("Pin [tasks.sdk-tooling] in mise.toml to a full Git SHA")
    return match[1]


def pinned_tooling(root, checkout, *, tooling_repository=_DEFAULT_TOOLING_REPOSITORY):
    """Load a dependency's build tools from its declared revision in a fresh checkout."""
    if (
        not isinstance(tooling_repository, str)
        or not GITHUB_REPOSITORY.fullmatch(tooling_repository)
        or ".." in tooling_repository
        or tooling_repository.endswith(".git")
    ):
        raise ValueError("Select a trusted tooling repository as GitHub owner/name")
    url = f"https://github.com/{tooling_repository}.git"
    manifest = Path(root).resolve(strict=True) / "mise.toml"
    if manifest.resolve() != manifest or not manifest.is_file():
        raise ValueError("Expected an ordinary mise.toml in the dependency SDK")
    revision = tooling_task_revision(tomllib.loads(manifest.read_text(encoding="utf-8")), url)

    checkout = Path(checkout)
    checkout.mkdir()
    git(None, "init", "--quiet", checkout)
    git(checkout, "config", "core.autocrlf", "false")
    git(checkout, "config", "core.longpaths", "true")
    git(checkout, "fetch", "--quiet", "--depth=1", "--no-tags", url, revision)
    git(checkout, "checkout", "--quiet", "--detach", revision)
    if git(checkout, "rev-parse", "HEAD^{commit}").decode().strip() != revision:
        raise ValueError("SDK Tooling checkout differs from the dependency pin")

    package = checkout.resolve() / "sdk_tooling"
    entry = package / "__init__.py"
    if entry.resolve() != entry or not entry.is_file():
        raise ValueError("Expected an ordinary sdk_tooling package in the pinned checkout")
    namespace = "_sdk_tooling_" + uuid4().hex
    spec = spec_from_file_location(namespace, entry, submodule_search_locations=[str(package)])
    module = module_from_spec(spec)
    sys.modules[namespace] = module
    spec.loader.exec_module(module)
    return partial(_load, namespace)


def git(root, *arguments):
    command = ["git", "-c", "core.hooksPath=" + os.devnull]
    if root is not None:
        command += ["-C", str(root)]
    try:
        return subprocess.check_output(
            [*command, *arguments],
            env=dict(
                os.environ,
                GIT_TERMINAL_PROMPT="0",
                GIT_NO_REPLACE_OBJECTS="1",
                GIT_NO_LAZY_FETCH="1",
            ),
            stderr=subprocess.PIPE,
            timeout=60,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
        raise RuntimeError("SDK source Git operation failed") from None


def support_inputs(root=None):
    root = Path(root or Path.cwd()).resolve(strict=True)
    package = _TOOLING_ROOT / "sdk_tooling"
    tooling = [
        *(
            path
            for path in package.rglob("*.py")
            if not EXCLUDED.intersection(path.relative_to(package).parts[:-1])
        ),
        _TOOLING_ROOT / "crates/cargo-consumer-identity/src/main.rs",
        _TOOLING_ROOT / "crates/rustc-dep-info/src/main.rs",
    ]
    inputs = {"@sdk-tooling/" + key: value for key, value in hashes(tooling, _TOOLING_ROOT).items()}
    declarations = [
        root / name
        for name in (MANIFEST, *LEGACY_MANIFESTS.values(), "global.json", "mise.toml")
        if (root / name).is_file()
    ]
    distribution_scripts = sorted((root / "distribution").glob("*.py"))
    fixture_inputs = []
    for fixture in (root / "fixtures/consumers", root / "fixtures/acceptance"):
        for directory, subdirectories, filenames in fixture.walk():
            subdirectories[:] = [name for name in subdirectories if name not in EXCLUDED]
            fixture_inputs.extend(
                directory / name
                for name in filenames
                if name == "Cargo.lock" or Path(name).suffix in _FIXTURE_SOURCE_EXTENSIONS
            )
    return inputs | hashes([*declarations, *distribution_scripts, *fixture_inputs], root)
