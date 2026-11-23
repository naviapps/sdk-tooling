#!/usr/bin/env python3
"""Run distribution commands from a verified SDK checkout."""

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

REVISION = re.compile(r"[a-f0-9]{40}")
REPOSITORY = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
PLATFORMS = ("macos", "windows")
CREDENTIAL_VARIABLES = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_RUNTIME_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GH_TOKEN",
    "GIT_ASKPASS",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
    "MISE_GITHUB_TOKEN",
    "SSH_ASKPASS",
    "SSH_AUTH_SOCK",
}
SOURCE_VARIABLES = {"NAVI_SDK_TOOLING_SOURCE", "SDK_TOOLING"}


def run(root, env, *command, stdout=None):
    print("[distribution]", *command, flush=True)
    return subprocess.run(command, cwd=root, env=env, stdout=stdout, text=True, check=True).stdout


def child_environment(revision):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in CREDENTIAL_VARIABLES | SOURCE_VARIABLES
        and not key.upper().startswith(("GIT_CONFIG_", "VM_RUNNER_", "NAVI_CI_"))
    }
    env.update(
        GITHUB_SHA=revision,
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_CONFIG_NOSYSTEM="1",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    return env


def verify_checkout(root, revision, repository, env, *, require_main=False):
    if not REVISION.fullmatch(revision):
        raise ValueError("Specify the full lowercase Git commit SHA")
    top = Path(
        run(root, env, "git", "rev-parse", "--show-toplevel", stdout=subprocess.PIPE).strip()
    )
    head = run(root, env, "git", "rev-parse", "HEAD^{commit}", stdout=subprocess.PIPE).strip()
    remote = run(root, env, "git", "remote", "get-url", "origin", stdout=subprocess.PIPE).strip()
    canonical = "https://github.com/" + repository
    if top.resolve() != root or head != revision or remote not in (canonical, canonical + ".git"):
        raise ValueError(f"Checkout does not match {repository}@{revision}")
    if run(
        root,
        env,
        "git",
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        stdout=subprocess.PIPE,
    ):
        raise ValueError(f"Distribution requires a clean checkout of {repository}")
    if require_main:
        # Run SDK code only after its commit is known to belong to the default release branch.
        run(root, env, "git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main")


def read_regular(source, relative):
    path = source.resolve() / relative
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"Expected an ordinary SDK file: {relative}")
    return path.read_bytes()


def mise_configuration(source):
    if (source / "mise.toml").exists():
        return tomllib.loads(read_regular(source, "mise.toml").decode("utf-8"))
    return {}


def tooling_revision(source, repository):
    tasks = mise_configuration(source).get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("SDK mise tasks must be a table")
    task = tasks.get("sdk-tooling")
    if task is not None:
        command = task.get("run") if isinstance(task, dict) else None
        prefix = f"uvx --from git+https://github.com/{repository}.git@"
        suffix = " sdk-tooling"
        if (
            not isinstance(command, str)
            or not command.startswith(prefix)
            or not command.endswith(suffix)
        ):
            raise ValueError("SDK tooling task must run the shared CLI at a full Git SHA")
        pin = command.removeprefix(prefix).removesuffix(suffix)
        if not REVISION.fullmatch(pin):
            raise ValueError("SDK tooling task must pin one full lowercase Git SHA")
        return pin
    pins = json.loads(read_regular(source, "tools/tooling-sources.json"))
    if (
        not isinstance(pins, dict)
        or set(pins) != {repository}
        or not isinstance(pins[repository], str)
        or not REVISION.fullmatch(pins[repository])
    ):
        raise ValueError("SDK distribution tooling pin must be one full lowercase Git SHA")
    return pins[repository]


def windows_dotnet_version(source):
    tools = mise_configuration(source).get("tools", {})
    if not isinstance(tools, dict):
        raise ValueError("SDK mise tools must be a table")
    if "dotnet" not in tools:
        return ""
    version = tools["dotnet"]
    if isinstance(version, dict):
        systems = version.get("os", ["windows"])
        if not isinstance(systems, list) or not all(isinstance(item, str) for item in systems):
            raise ValueError("SDK .NET operating systems must be an array")
        if "windows" not in systems:
            return ""
        version = version.get("version")
    if not isinstance(version, str) or not re.fullmatch(
        r"[0-9]+\.[0-9]+\.[0-9]+(?:-[a-zA-Z0-9]+(?:[.-][a-zA-Z0-9]+)*)?", version
    ):
        raise ValueError("Windows distribution requires one exact .NET SDK version in mise.toml")
    return version


def clean_text(value):
    return (
        isinstance(value, str)
        and bool(value)
        and all(character.isprintable() for character in value)
    )


def archive_entry(value):
    if (
        not isinstance(value, dict)
        or value.keys() != {"url", "sha256", "bin"}
        or not all(clean_text(item) for item in value.values())
        or not re.fullmatch(r"[a-f0-9]{64}", value["sha256"])
    ):
        raise ValueError("Tool archives require url, sha256 and bin")
    parsed = urlsplit(value["url"])
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or any(character.isspace() for character in value["url"])
    ):
        raise ValueError("Tool archive URL must use HTTPS without credentials or a fragment")
    bindir = PurePosixPath(value["bin"])
    if (
        not bindir.parts
        or bindir.is_absolute()
        or bindir.as_posix() != value["bin"]
        or ".." in bindir.parts
        or any(character in value["bin"] for character in "\\:;")
    ):
        raise ValueError("Tool archive bin must be a canonical relative directory")
    return value


def distribution_plan(source):
    plan = tomllib.loads(read_regular(source, "sdk-distribution.toml").decode("utf-8"))
    if not plan or not plan.keys() <= set(PLATFORMS):
        raise ValueError("Declare macos or windows distribution commands")
    selected = {}
    for platform, settings in plan.items():
        if not isinstance(settings, dict) or not settings.keys() <= {"commands", "archives"}:
            raise ValueError(f"Unknown distribution settings for {platform}")
        commands = settings.get("commands")
        if (
            not isinstance(commands, list)
            or not commands
            or not all(
                isinstance(argv, list)
                and argv
                and all(clean_text(argument) for argument in argv)
                and argv[0].strip()
                for argv in commands
            )
        ):
            raise ValueError(f"Distribution commands for {platform} must be nonempty argv arrays")
        archives = settings.get("archives", [])
        if not isinstance(archives, list):
            raise ValueError(f"Tool archives for {platform} must be an array")
        selected[platform] = {
            "commands": commands,
            "archives": [archive_entry(entry) for entry in archives],
        }
    return selected


def install_archive(source, env, temp, index, configuration):
    root = temp / f"tool-{index}"
    root.mkdir()
    archive = root / "tool.tar"
    run(
        source,
        env,
        "curl",
        "--proto",
        "=https",
        "--proto-redir",
        "=https",
        "-fsSL",
        "--retry",
        "3",
        "-o",
        archive,
        configuration["url"],
    )
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != configuration["sha256"]:
            raise ValueError("Pinned tool archive checksum differs")
    extracted = root / "contents"
    with tarfile.open(archive) as package:
        package.extractall(extracted, filter="data")
    binary_path = extracted / configuration["bin"]
    if not binary_path.is_dir() or not binary_path.resolve().is_relative_to(extracted):
        raise ValueError("Pinned tool archive does not provide the expected bin directory")
    return binary_path


def execute(source, platform, settings, env, temp, tooling):
    pinned_cli = "sdk-tooling" in mise_configuration(source).get("tasks", {})
    if not pinned_cli or (source / "tools/_tooling.py").is_file():
        env = dict(env, NAVI_SDK_TOOLING_SOURCE=str(tooling))
    tool_dirs = [
        str(install_archive(source, env, temp, index, archive))
        for index, archive in enumerate(settings["archives"])
    ]
    if tool_dirs:
        env = dict(env, PATH=os.pathsep.join((*tool_dirs, env["PATH"])))

    prefix = ()
    launcher = tooling / "ci/run_sdk_tooling.py"
    if not launcher.is_file():
        # Published SDK revisions may pin the previous checkout layout.
        launcher = tooling / "tasks/sdk"
    sdk_cli = [sys.executable, "-I", "-B", str(launcher)]
    make_env = env
    if platform == "macos" and (source / "mise.toml").is_file():
        run(source, env, "mise", "trust", "mise.toml")
        run(source, env, "mise", "install")
        prefix = ("mise", "exec", "--")
        if pinned_cli:
            # Make expands dollar signs before passing the command to the shell.
            make_env = dict(env, SDK_TOOLING=shlex.join(sdk_cli).replace("$", "$$"))

    for argv in settings["commands"]:
        command = [
            argument.replace("{temp}", str(temp)).replace("{tooling}", str(tooling))
            for argument in argv
        ]
        if command[0] == "sdk-tooling":
            command = [*sdk_cli, *command[1:]]
        elif platform == "windows" and command[0] == "python":
            command[0] = sys.executable
        run(source, make_env if command[0] == "make" else env, *prefix, *command)


def write_output(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            print(f"{name}={value}", file=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identity", "pin", *PLATFORMS))
    parser.add_argument("sdk")
    parser.add_argument("revision")
    parser.add_argument("--platform", choices=PLATFORMS)
    parser.add_argument("--tooling", type=Path)
    parser.add_argument("--tooling-repository", default=os.environ.get("GITHUB_REPOSITORY"))
    args = parser.parse_args(argv)
    repository = args.tooling_repository or ""
    owner = repository.partition("/")[0]
    sdk_repository = f"{owner}/{args.sdk}"
    if (
        not REPOSITORY.fullmatch(repository)
        or not REPOSITORY.fullmatch(sdk_repository)
        or ".." in sdk_repository
        or sdk_repository.endswith(".git")
        or not REVISION.fullmatch(args.revision)
        or repository.casefold() == sdk_repository.casefold()
    ):
        parser.error("Specify a repository in the tooling owner and its full lowercase Git SHA")
    if args.mode == "identity":
        write_output(repository=sdk_repository)
        return

    source = Path.cwd().resolve()
    env = child_environment(args.revision)
    verify_checkout(source, args.revision, sdk_repository, env, require_main=True)
    plan = distribution_plan(source)
    pin = tooling_revision(source, repository)
    if args.mode == "pin":
        if args.platform is None:
            parser.error("Reading the plan requires --platform")
        enabled = args.platform in plan
        dotnet = windows_dotnet_version(source) if enabled and args.platform == "windows" else ""
        write_output(revision=pin, enabled=str(enabled).lower(), dotnet=dotnet)
        return

    if args.mode not in plan:
        parser.error("SDK distribution configuration does not declare this platform")
    if args.tooling is None:
        parser.error("Distribution checks require --tooling with the pinned checkout")
    tooling = args.tooling.resolve(strict=True)
    verify_checkout(tooling, pin, repository, env)
    with tempfile.TemporaryDirectory(
        dir=env.get("RUNNER_TEMP"), prefix="sdk-distribution-"
    ) as work:
        temp = Path(work).resolve()
        execute(source, args.mode, plan[args.mode], env, temp, tooling)


if __name__ == "__main__":
    main()
