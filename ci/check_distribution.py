#!/usr/bin/env python3
"""Run distribution commands from a verified SDK checkout."""

import argparse
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path
from platform import machine

REVISION = re.compile(r"[a-f0-9]{40}")
SHA256 = re.compile(r"[a-f0-9]{64}")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.nupkg")
REPOSITORY = re.compile(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)*/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}")
PLATFORMS = ("macos", "windows")
PACKAGE_MODES = ("nuget-pack", "nuget-consume")
NUGET_RUNTIMES = ("osx-arm64", "osx-x64", "win-x64")
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


def builds_xcframeworks(source, platform):
    declaration = source / "native.toml"
    if platform != "macos" or not (declaration.exists() or declaration.is_symlink()):
        return False
    read_regular(source, "native.toml")
    return True


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


def file_digest(path):
    if path.resolve() != path or not path.is_file():
        raise ValueError("Expected an ordinary NuGet package")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def mise_configuration(source):
    if (source / "mise.toml").exists():
        return tomllib.loads(read_regular(source, "mise.toml").decode("utf-8"))
    return {}


def tooling_revision(source, repository):
    tasks = mise_configuration(source).get("tasks", {})
    if not isinstance(tasks, dict):
        raise ValueError("SDK mise tasks must be a table")
    task = tasks.get("sdk-tooling")
    command = task.get("run") if isinstance(task, dict) else None
    prefix = f"uvx --from git+https://github.com/{repository}.git@"
    suffix = " sdk-tooling"
    if (
        not isinstance(command, str)
        or not command.startswith(prefix)
        or not command.endswith(suffix)
    ):
        raise ValueError(
            "Declare the shared CLI at a full Git SHA in mise.toml [tasks.sdk-tooling]"
        )
    pin = command.removeprefix(prefix).removesuffix(suffix)
    if not REVISION.fullmatch(pin):
        raise ValueError("SDK tooling task must pin one full lowercase Git SHA")
    return pin


def has_dotnet_pin(source):
    path = source / "global.json"
    if not (path.exists() or path.is_symlink()):
        return False
    read_regular(source, "global.json")
    return True


def clean_text(value):
    return (
        isinstance(value, str)
        and bool(value)
        and all(character.isprintable() for character in value)
    )


def distribution_plan(source):
    plan = tomllib.loads(read_regular(source, "sdk-distribution.toml").decode("utf-8"))
    packaging = plan.pop("nuget", None)
    if not plan or not plan.keys() <= set(PLATFORMS):
        raise ValueError("Declare macos or windows distribution commands")
    selected = {}
    for platform, settings in plan.items():
        if not isinstance(settings, dict) or not settings.keys() <= {"commands"}:
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
        selected[platform] = commands
    if packaging is not None:
        if (
            not isinstance(packaging, dict)
            or packaging.keys() != {"pack"}
            or set(selected) != set(PLATFORMS)
            or not has_dotnet_pin(source)
            or not builds_xcframeworks(source, "macos")
        ):
            raise ValueError(
                "Combined NuGet checks require both platforms, global.json and native.toml"
            )
        script = packaging["pack"]
        if not isinstance(script, str) or not re.fullmatch(
            r"distribution/[a-z][a-z0-9_]*\.py", script
        ):
            raise ValueError("nuget.pack must select a Python file in distribution/")
        read_regular(source, script)
        selected["nuget"] = script
    return selected


def workflow_plan(source, plan, pin, *, windows_on_arm=False):
    combined = "nuget" in plan
    if windows_on_arm and not combined:
        raise ValueError("Windows on Arm requires a combined NuGet distribution plan")
    return {
        "revision": pin,
        "platforms": json.dumps(
            ["windows"] if combined else [platform for platform in PLATFORMS if platform in plan]
        ),
        "dotnet": str("windows" in plan and has_dotnet_pin(source)).lower(),
        "xcframeworks": str("macos" in plan and builds_xcframeworks(source, "macos")).lower(),
        "nuget": str(combined).lower(),
    }


def execute(source, platform, commands, env, temp, tooling, *, xcframework_output=None):
    if platform == "windows" and env.get("RUNNER_TEMP"):
        # Cargo clones can contain macOS framework paths even on Windows.
        env = dict(env, TMPDIR=env["RUNNER_TEMP"])
    prefix = ()
    launcher = tooling / "ci/run_sdk_tooling.py"
    if not launcher.is_file():
        raise ValueError("Pinned SDK Tooling must provide ci/run_sdk_tooling.py")
    sdk_cli = [sys.executable, "-I", "-B", str(launcher)]
    if platform == "macos" and (source / "mise.toml").is_file():
        run(source, env, "mise", "trust", "mise.toml")
        run(source, env, "mise", "install")
        prefix = ("mise", "exec", "--")
        # Make expands dollar signs before passing the command to the shell.
        # Set it for the run environment so Make still sees it inside mise tasks.
        env = dict(env, SDK_TOOLING=shlex.join(sdk_cli).replace("$", "$$"))

    for argv in commands:
        command = [
            argument.replace("{temp}", str(temp)).replace("{tooling}", str(tooling))
            for argument in argv
        ]
        if command[0] == "sdk-tooling":
            command = [*sdk_cli, *command[1:]]
        elif platform == "windows" and command[0] == "python":
            command[0] = sys.executable
        run(source, env, *prefix, *command)

    if xcframework_output is None:
        return

    # Installed Rust and C fixture checks above require the exact committed
    # source tree. Rebuild only after those checks finish in the CI checkout.
    run(
        source,
        env,
        *prefix,
        "rustup",
        "target",
        "add",
        "aarch64-apple-darwin",
        "x86_64-apple-darwin",
    )
    if ["cargo", "fetch", "--locked"] not in commands:
        run(source, env, *prefix, "cargo", "fetch", "--locked")
    run(source, env, *prefix, *sdk_cli, "xcframework")
    if (source / "Package.swift").is_file():
        run(source, env, *prefix, "make", "test")
        run(source, env, *prefix, *sdk_cli, "swift-consumer", "--distribution")

    # The dispatcher owns the archive format. Its verifier uses the exact
    # producer revision that generated the framework and receipt.
    dispatcher = Path(__file__).resolve().with_name("run_sdk_tooling.py")
    run(
        source,
        env,
        *prefix,
        sys.executable,
        "-I",
        "-B",
        str(dispatcher),
        "xcframework-archive",
        "--output",
        str(xcframework_output),
        "--revision",
        env["GITHUB_SHA"],
        "--producer",
        str(tooling),
    )


def check_dotnet_version(source, env, *, dotnet=None):
    settings = json.loads(read_regular(source, "global.json"))["sdk"]
    dotnet = dotnet or shutil.which("dotnet", path=env.get("PATH"))
    if (
        not dotnet
        or settings.get("rollForward") != "disable"
        or run(source, env, dotnet, "--version", stdout=subprocess.PIPE).strip()
        != settings.get("version")
    ):
        raise ValueError("Install the SDK's exact .NET version before checking its package")
    return dict(env, DOTNET_BIN=dotnet)


def require_macos_dotnet_rid(source, env, rid):
    env = dict(env, DOTNET_CLI_UI_LANGUAGE="en")
    info = run(source, env, env["DOTNET_BIN"], "--info", stdout=subprocess.PIPE)
    if not re.search(r"(?m)^ +RID: +" + re.escape(rid) + r" *$", info):
        raise ValueError(f"The selected .NET executable must report {rid}")


def macos_x64_environment(source, env, dotnet):
    if sys.platform != "darwin" or not dotnet.is_absolute() or not dotnet.is_file():
        raise ValueError("Select a separate absolute macOS x64 .NET executable")
    dotnet = dotnet.resolve(strict=True)
    env = dict(
        env,
        DOTNET_ROOT=str(dotnet.parent),
        DOTNET_ROOT_X64=str(dotnet.parent),
        DOTNET_HOST_PATH=str(dotnet),
    )
    env = check_dotnet_version(source, env, dotnet=str(dotnet))
    require_macos_dotnet_rid(source, env, "osx-x64")
    return env


def require_windows_host(expected):
    machines = {"x64": "amd64", "arm64": "arm64"}
    if sys.platform != "win32" or machine().casefold() != machines[expected]:
        raise ValueError(f"NuGet consumer requires a native Windows {expected} worker")


def package_digest(package, receipt, *, expected=None):
    checksum = file_digest(package)
    if (
        not isinstance(receipt, dict)
        or receipt.get("package") != package.name
        or receipt.get("runtimes") != list(NUGET_RUNTIMES)
        or receipt.get("sha256") != checksum
        or (expected is not None and checksum != expected)
    ):
        raise ValueError("Combined NuGet package differs from its accepted receipt")
    return checksum


def consume_package(source, env, sdk_cli, package, *, expected=None, windows_host=None):
    if not PACKAGE_NAME.fullmatch(package.name) or ".." in package.name:
        raise ValueError("Invalid combined NuGet package name")
    receipt_path = package.with_suffix(".nupkg.json")
    receipt_bytes = read_regular(receipt_path.parent, receipt_path.name)
    receipt = json.loads(receipt_bytes)
    checksum = package_digest(package, receipt, expected=expected)
    target = ()
    if windows_host is not None:
        require_windows_host(windows_host)
        target = ("--windows-architecture", "x64")
    run(source, env, *sdk_cli, "nuget-consumer", str(package), *target)
    if (
        file_digest(package) != checksum
        or read_regular(receipt_path.parent, receipt_path.name) != receipt_bytes
    ):
        raise ValueError("Combined NuGet package or receipt changed during consumer acceptance")
    print(f"Verified identical NuGet package bytes: {package.name} sha256:{checksum}")
    return checksum


def pack_combined_nuget(source, env, sdk_cli, script, output, *, macos_x64_env=None):
    if output.exists() or output.is_symlink() or output.is_relative_to(source):
        raise ValueError("NuGet artifact output must be a new directory outside the SDK checkout")
    with tempfile.TemporaryDirectory(dir=output.parent, prefix="nuget-candidate-") as directory:
        stage = Path(directory).resolve() / "package"
        stage.mkdir()
        result = stage / "package-name"
        run(
            source,
            env,
            *sdk_cli,
            "python",
            script,
            *(arg for rid in NUGET_RUNTIMES for arg in ("--runtime", rid)),
            "--result",
            str(result),
        )
        name = read_regular(stage, result.name).decode("utf-8")
        if not PACKAGE_NAME.fullmatch(name) or ".." in name:
            raise ValueError("SDK packer did not return one ordinary NuGet package name")
        result.unlink()
        built = source / ".native-build/packages" / name
        receipt = built.with_suffix(".nupkg.json")
        candidate = stage / name
        checksum = package_digest(built, json.loads(read_regular(receipt.parent, receipt.name)))
        shutil.copyfile(built, candidate)
        candidate.with_suffix(".nupkg.json").write_bytes(read_regular(receipt.parent, receipt.name))
        consume_package(source, env, sdk_cli, candidate, expected=checksum)
        if macos_x64_env is not None:
            print("Check the same NuGet package with macOS x64 .NET", flush=True)
            consume_package(source, macos_x64_env, sdk_cli, candidate, expected=checksum)
        if output.exists() or output.is_symlink():
            raise ValueError("Combined NuGet artifact output appeared during consumer acceptance")
        stage.rename(output)
    return checksum


def write_output(**values):
    with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
        for name, value in values.items():
            print(f"{name}={value}", file=output)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("identity", "plan", *PLATFORMS, *PACKAGE_MODES))
    parser.add_argument("sdk")
    parser.add_argument("revision")
    parser.add_argument(
        "--windows-on-arm", action="store_true", help="Require an Arm consumer plan"
    )
    parser.add_argument("--windows-host", choices=("x64", "arm64"), help="Require a Windows host")
    parser.add_argument("--tooling", type=Path)
    parser.add_argument("--nuget-artifact", type=Path, help="Combined NuGet artifact directory")
    parser.add_argument("--nuget-sha256", help="Digest of the package accepted on macOS")
    parser.add_argument(
        "--macos-x64-dotnet",
        type=Path,
        help="Also verify the package with a separate macOS x64 .NET",
    )
    parser.add_argument(
        "--xcframework-output",
        type=Path,
        help="Build, check and archive XCFrameworks from the disposable macOS SDK checkout",
    )
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
    if args.xcframework_output is not None and args.mode != "macos":
        parser.error("--xcframework-output requires the macos distribution mode")
    if args.mode in PACKAGE_MODES and args.nuget_artifact is None:
        parser.error("Combined NuGet modes require --nuget-artifact")
    if (
        args.nuget_artifact is not None
        and args.mode not in PACKAGE_MODES
        and not (args.mode == "macos" and args.xcframework_output is not None)
    ):
        parser.error("Packing during macOS distribution requires --xcframework-output")
    if args.windows_on_arm and args.mode != "plan":
        parser.error("--windows-on-arm applies only when reading the plan")
    if args.windows_host is not None and args.mode != "nuget-consume":
        parser.error("--windows-host applies only when consuming an accepted package")
    if args.mode == "nuget-consume":
        if not SHA256.fullmatch(args.nuget_sha256 or ""):
            parser.error("NuGet consumption requires --nuget-sha256 from macOS acceptance")
    elif args.nuget_sha256 is not None:
        parser.error("--nuget-sha256 applies only when consuming an accepted package")
    if args.macos_x64_dotnet is not None and (
        args.nuget_artifact is None or args.mode not in ("macos", "nuget-pack")
    ):
        parser.error("--macos-x64-dotnet requires macOS NuGet packing")
    if args.mode == "identity":
        write_output(repository=sdk_repository)
        return

    source = Path.cwd().resolve()
    env = child_environment(args.revision)
    verify_checkout(source, args.revision, sdk_repository, env, require_main=True)
    plan = distribution_plan(source)
    pin = tooling_revision(source, repository)
    if args.mode == "plan":
        write_output(**workflow_plan(source, plan, pin, windows_on_arm=args.windows_on_arm))
        return

    if args.mode not in plan and not (args.mode in PACKAGE_MODES and "nuget" in plan):
        parser.error("SDK distribution configuration does not declare this platform")
    if args.xcframework_output is not None and not builds_xcframeworks(source, args.mode):
        parser.error("--xcframework-output requires an SDK with native.toml")
    if args.tooling is None:
        parser.error("Distribution checks require --tooling with the pinned checkout")
    tooling = args.tooling.resolve(strict=True)
    verify_checkout(tooling, pin, repository, env)
    artifact = None
    macos_x64_env = None
    if args.nuget_artifact is not None:
        if "nuget" not in plan:
            parser.error("SDK distribution configuration does not declare combined NuGet packages")
        artifact = args.nuget_artifact.absolute()
        if artifact.is_symlink():
            raise ValueError("Combined NuGet artifact must be an ordinary directory")
        artifact = artifact.resolve()
        launcher = tooling / "ci/run_sdk_tooling.py"
        read_regular(tooling, "ci/run_sdk_tooling.py")
        sdk_cli = (sys.executable, "-I", "-B", str(launcher))
        env = check_dotnet_version(source, env)
        if args.macos_x64_dotnet is not None:
            require_macos_dotnet_rid(source, env, "osx-arm64")
            macos_x64_env = macos_x64_environment(source, env, args.macos_x64_dotnet)
    if args.mode == "nuget-consume":
        packages = list(artifact.glob("*.nupkg"))
        if len(packages) != 1:
            raise ValueError("Combined NuGet artifact must contain exactly one package")
        [package] = packages
        consume_package(
            source,
            env,
            sdk_cli,
            package,
            expected=args.nuget_sha256,
            windows_host=args.windows_host,
        )
        return
    if args.mode in PLATFORMS:
        with tempfile.TemporaryDirectory(
            dir=env.get("RUNNER_TEMP"), prefix="sdk-distribution-"
        ) as work:
            execute(
                source,
                args.mode,
                plan[args.mode],
                env,
                Path(work).resolve(),
                tooling,
                xcframework_output=args.xcframework_output,
            )
    if artifact is not None:
        checksum = pack_combined_nuget(
            source, env, sdk_cli, plan["nuget"], artifact, macos_x64_env=macos_x64_env
        )
        if os.environ.get("GITHUB_OUTPUT"):
            write_output(sha256=checksum)


if __name__ == "__main__":
    main()
