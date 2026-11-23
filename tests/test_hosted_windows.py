import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import adapters, hosted_windows


class HostedWindowsTests(unittest.TestCase):
    def test_runs_sdk_owned_command_with_hosted_paths(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as temporary:
            root = Path(temporary)
            sdk = root / "sdk checkout"
            sdk.mkdir()
            command = ["-m", "tools.native_host", "--architecture", "x86_64"]
            dotnet = str(root / "dotnet installation" / "dotnet.exe")
            env = {"RUNNER_TEMP": str(root)}

            with (
                patch.object(hosted_windows.sys, "platform", "win32"),
                patch.dict(
                    os.environ,
                    env | {"NAVI_CI_ARTIFACTS": "legacy stale", "VM_RUNNER_ARTIFACTS_DIR": "stale"},
                    clear=True,
                ),
                patch.object(hosted_windows.shutil, "which", return_value=dotnet),
                patch.object(hosted_windows.subprocess, "run") as run,
            ):
                hosted_windows.run(sdk, command)

            run.assert_called_once_with(
                adapters.python_command(sdk, *command),
                cwd=sdk,
                env=dict(
                    env, DOTNET_BIN=dotnet, VM_RUNNER_ARTIFACTS_DIR=str(root / "sdk-acceptance")
                ),
                check=True,
            )

    def test_rejects_a_relative_dotnet_path(self):
        with (
            tempfile.TemporaryDirectory() as temporary,
            patch.object(hosted_windows.sys, "platform", "win32"),
            patch.dict(os.environ, {"RUNNER_TEMP": temporary}, clear=True),
            patch.object(hosted_windows.shutil, "which", return_value="dotnet.exe"),
            patch.object(hosted_windows.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(SystemExit, "The pinned x64 .NET SDK was not installed"):
                hosted_windows.run(Path(temporary), ["tools/windows.py"])

        run.assert_not_called()
