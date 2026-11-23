import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import adapters, hosted_windows


class HostedWindowsTests(unittest.TestCase):
    def test_runs_sdk_owned_command_without_loading_a_candidate_pin(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as temporary:
            root = Path(temporary)
            sdk = root / "sdk checkout"
            sdk.mkdir()
            command = ["-m", "tools.native_host", "--architecture", "x86_64"]
            dotnet = str(root / "dotnet installation" / "dotnet.exe")
            tooling = str(root / "pinned tooling")
            env = {"RUNNER_TEMP": str(root), "NAVI_SDK_TOOLING_SOURCE": tooling}

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

    def test_legacy_template_forwards_the_verified_candidate(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as temporary:
            root = Path(temporary)
            sdk = root / "sdk checkout"
            (sdk / "tools").mkdir(parents=True)
            revision = "a" * 40
            (sdk / "tools/tooling-sources.json").write_text(
                json.dumps({"naviapps/sdk-tooling": revision}), encoding="utf-8"
            )
            tooling = root / "verified tooling"
            command = ["-m", "tools.native_host", "--architecture", "x86_64"]
            dotnet = str(root / "dotnet installation" / "dotnet.exe")
            env = {"RUNNER_TEMP": str(root), "DOTNET_CLI_TELEMETRY_OPTOUT": "1"}

            with (
                patch.object(hosted_windows, "__file__", tooling / "sdk_tooling/hosted_windows.py"),
                patch.object(hosted_windows.sys, "platform", "win32"),
                patch.dict(os.environ, env, clear=True),
                patch.object(hosted_windows.shutil, "which", return_value=dotnet) as which,
                patch.object(hosted_windows.subprocess, "run") as run,
            ):
                hosted_windows.main(sdk, command)

            which.assert_called_once_with("dotnet.exe")
            run.assert_called_once_with(
                [
                    sys.executable,
                    "-E",
                    "-S",
                    "-B",
                    *command,
                    "--tooling-source",
                    str(tooling.resolve()),
                    "--tooling-revision",
                    revision,
                ],
                cwd=sdk,
                env=dict(env, DOTNET_BIN=dotnet, NAVI_CI_ARTIFACTS=str(root / "sdk-acceptance")),
                check=True,
            )
