"""Launch SDK-owned Windows acceptance from verified tooling on a hosted runner."""

import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

from . import c_headers, vm_environment
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


def run(sdk_root, command):
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
    vm_environment.set_value(env, vm_environment.Variable.ARTIFACTS_DIR, temp / "sdk-acceptance")
    invocation = python_command(sdk_root, *command)
    if not c_headers.has_generated_products(sdk_root):
        subprocess.run(invocation, cwd=sdk_root, env=env, check=True)
        return

    version = _cbindgen_pin(sdk_root)
    with temporary_workspace(temp, prefix="sdk-cbindgen-") as tools:
        subprocess.run(
            [
                "cargo",
                "install",
                "--locked",
                "--version",
                f"={version}",
                "--root",
                tools,
                "cbindgen",
            ],
            cwd=sdk_root,
            env=env,
            check=True,
        )
        binaries = tools / "bin"
        cbindgen = binaries / "cbindgen.exe"
        if not cbindgen.is_file() or cbindgen.is_symlink():
            raise ValueError("Cargo did not install the pinned cbindgen executable")
        env = dict(env, PATH=os.pathsep.join(filter(None, (str(binaries), env.get("PATH")))))
        c_headers.prepare_generated(sdk_root, env=env)
        subprocess.run(invocation, cwd=sdk_root, env=env, check=True)
