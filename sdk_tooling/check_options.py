"""Select the host and target for C and .NET checks."""

import argparse
import shutil
import sys

from . import windows


def select_dotnet_host(env, target):
    if sys.platform == "win32":
        host = windows.host_architecture()
        architecture = target or host
        return windows.target_dotnet(host, architecture, env), architecture, host != architecture
    if target is not None:
        raise ValueError("--windows-architecture requires a Windows host")
    executable = env.get("DOTNET_BIN") or shutil.which("dotnet", path=env.get("PATH"))
    if not executable:
        raise ValueError("Install .NET 10 or provide DOTNET_BIN")
    return executable, None, False


def add_windows_architecture_argument(parser):
    parser.add_argument(
        "--windows-architecture",
        choices=tuple(windows.MSVC_TARGETS),
        help="Windows fixture target. x64 can run under emulation on ARM64",
    )


def windows_x64_options(argv, *, windows_x64):
    if not windows_x64:
        return argv
    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--windows-architecture", choices=("x64",))
    args, remaining = parser.parse_known_args(argv)
    if sys.platform == "win32":
        remaining.extend(("--windows-architecture", "x64"))
    elif args.windows_architecture is not None:
        parser.error("--windows-architecture requires Windows")
    return remaining
