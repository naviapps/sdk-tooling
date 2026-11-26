"""Launch SDK-owned Windows acceptance from verified tooling on a hosted runner."""

import os
import re
import shutil
import subprocess
import sys
from contextlib import nullcontext
from pathlib import Path

from . import c_headers, vm_environment, windows
from .adapters import python_command
from .quality import tool_pins
from .workspaces import temporary_workspace


def _cbindgen_pin(root):
    pin = tool_pins(root).get("cargo:cbindgen")
    version = pin.get("version") if isinstance(pin, dict) else pin
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[A-Za-z0-9.-]+)?", version
    ):
        raise ValueError("Pin the exact cargo:cbindgen version in SDK mise.toml")
    return version


def _install_cbindgen(tools, version, *, cwd, env, cargo=("cargo",), target=()):
    subprocess.run(
        [
            *cargo,
            "install",
            "--locked",
            "--version",
            f"={version}",
            "--root",
            tools,
            *target,
            "cbindgen",
        ],
        cwd=cwd,
        env=env,
        check=True,
    )


def _cached_cbindgen(temp, version):
    executable = temp / "sdk-cbindgen" / version / "bin/cbindgen.exe"
    if executable.resolve() != executable:
        raise ValueError("Use an ordinary cached cbindgen executable path")
    return executable


def prepare_cbindgen_cache(temp, version, rust, *, warm):
    """Populate the hosted cache before executing any candidate SDK code."""
    if sys.platform != "win32":
        raise ValueError("The hosted cbindgen cache requires Windows")
    temp = temp.resolve(strict=True)
    executable = _cached_cbindgen(temp, version)
    env = dict(os.environ)
    if not warm:
        executable.unlink(missing_ok=True)
        target = windows.MSVC_TARGETS["x64"]
        toolchain = f"{rust}-{target}"
        subprocess.run(
            [
                "rustup",
                "toolchain",
                "install",
                toolchain,
                "--profile",
                "minimal",
                "--no-self-update",
            ],
            cwd=temp,
            env=env,
            check=True,
        )
        _install_cbindgen(
            executable.parent.parent,
            version,
            cwd=temp,
            env=env,
            cargo=("cargo", f"+{toolchain}"),
            target=("--target", target),
        )
    windows.require_pe_machine(executable, "x64")
    installed = subprocess.check_output(
        [executable, "--version"], cwd=temp, env=env, text=True
    ).strip()
    if installed != f"cbindgen {version}":
        raise ValueError("Cached cbindgen version differs from the SDK pin")


def run(sdk_root, command, *, prepare_headers=True):
    if sys.platform != "win32":
        raise SystemExit("Hosted Windows SDK acceptance requires Windows.")
    if not command:
        raise SystemExit("Specify the SDK-owned Windows acceptance command.")

    temp = Path(os.environ.get("RUNNER_TEMP", ""))
    if not temp.is_absolute() or not temp.is_dir():
        raise SystemExit("Set RUNNER_TEMP to an absolute runner temporary directory.")
    dotnet = shutil.which("dotnet.exe")
    if not dotnet or not Path(dotnet).is_absolute():
        raise SystemExit("The pinned x64 .NET SDK was not installed.")

    env = dict(os.environ, DOTNET_BIN=dotnet)
    use_cache = env.pop("SDK_TOOLING_HOSTED_CBINDGEN_CACHE", None) == "true"
    vm_environment.set_value(env, vm_environment.Variable.ARTIFACTS_DIR, temp / "sdk-acceptance")
    invocation = python_command(sdk_root, *command)
    if not c_headers.has_generated_products(sdk_root):
        subprocess.run(invocation, cwd=sdk_root, env=env, check=True)
        return

    version = _cbindgen_pin(sdk_root)
    installation = (
        nullcontext(_cached_cbindgen(temp.resolve(), version).parent.parent)
        if use_cache
        else temporary_workspace(temp, prefix="sdk-cbindgen-")
    )
    with installation as tools:
        cbindgen = tools / "bin/cbindgen.exe"
        if not use_cache:
            _install_cbindgen(tools, version, cwd=sdk_root, env=env)
        if not cbindgen.is_file() or cbindgen.resolve() != cbindgen:
            raise ValueError("Expected the pinned cbindgen executable")
        env = dict(env, PATH=os.pathsep.join(filter(None, (str(cbindgen.parent), env.get("PATH")))))
        if prepare_headers:
            c_headers.prepare_generated(sdk_root, env=env)
        subprocess.run(invocation, cwd=sdk_root, env=env, check=True)
