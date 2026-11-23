"""Launch SDK-owned Windows acceptance from verified tooling on a hosted runner."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


def main(sdk_root, command):
    if sys.platform != "win32":
        raise SystemExit("Hosted Windows SDK acceptance requires Windows.")
    if not command:
        raise SystemExit("Specify the SDK-owned Windows acceptance command.")

    temp = Path(os.environ.get("RUNNER_TEMP", ""))
    if not temp.is_absolute() or not temp.is_dir():
        raise SystemExit("Set RUNNER_TEMP to an absolute runner temporary directory.")
    dotnet = shutil.which("dotnet.exe")
    if not dotnet:
        raise SystemExit("The pinned x64 .NET SDK was not installed.")

    tooling = Path(__file__).resolve().parent.parent
    pins = json.loads((sdk_root / "tools/tooling-sources.json").read_text(encoding="utf-8"))
    subprocess.run(
        [
            sys.executable,
            "-E",
            "-S",
            "-B",
            *command,
            "--tooling-source",
            str(tooling),
            "--tooling-revision",
            pins["naviapps/sdk-tooling"],
        ],
        cwd=sdk_root,
        env=dict(os.environ, DOTNET_BIN=dotnet, NAVI_CI_ARTIFACTS=str(temp / "sdk-acceptance")),
        check=True,
    )
