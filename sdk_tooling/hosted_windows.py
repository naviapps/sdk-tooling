"""Launch SDK-owned Windows acceptance from verified tooling on a hosted runner."""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from . import vm_environment
from .adapters import python_command


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
    subprocess.run(
        python_command(sdk_root, *command),
        cwd=sdk_root,
        env=env,
        check=True,
    )
