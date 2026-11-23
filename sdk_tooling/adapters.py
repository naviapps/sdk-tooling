"""Execute SDK-owned Python fixtures with the selected SDK Tooling package."""

import os
import re
import subprocess
import sys
from importlib import import_module
from pathlib import Path

from .artifacts import hashes
from .native_plan import SPECIFICATION
from .quality import EXCLUDED

_TOOLING_ROOT = Path(__file__).resolve().parents[1]
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
    if not re.fullmatch(r"[a-z_]+(?:\.[a-z_]+)*", name):
        raise ValueError("Invalid SDK Tooling module")
    return import_module("sdk_tooling." + name)


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
        for name in (
            SPECIFICATION,
            "global.json",
            "mise.toml",
            "sdk-checks.toml",
            "sdk-distribution.toml",
        )
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
