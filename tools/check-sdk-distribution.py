#!/usr/bin/env python3
"""Run a published SDK revision's distribution checks from a trusted dispatcher."""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPOSITORY = "naviapps/sdk-tooling"
REVISION = re.compile(r"[0-9a-f]{40}\Z")
WINDOWS_DRIVERS = {
    "configuration-storage": ("tools/windows.py",),
    "desktop-actions": ("tools/check-windows.py",),
    "desktop-applications": ("tools/windows.py",),
    "pointer-gestures": ("-m", "tools.native_host", "--architecture", "x86_64"),
    "preset-documents": ("tools/windows.py",),
    "software-licensing": ("tools/windows.py",),
    "software-licensing-polar": ("tools/windows.py",),
    "system-permissions": ("tools/windows.py",),
    "window-management": ("tools/check-ci.py", "--skip-desktop"),
}
MACOS_TARGETS = {
    "desktop-applications": ("test-installed-consumers",),
    "pointer-gestures": ("check-rust-consumer", "check-c-consumer", "check-dotnet"),
    "software-licensing": ("test-installed-consumers",),
    "system-permissions": ("test-installed-consumers",),
    "window-management": ("test-installed-consumers", "test-c", "test-dotnet"),
}
DEPENDENCY_CHECKOUTS = {
    "desktop-actions": "window-management",
    "software-licensing-polar": "software-licensing",
}
RUST_FORMAT_ON_WINDOWS = {"desktop-actions", "pointer-gestures", "window-management"}
WASM_BINDGEN_VERSION = "0.2.104"
WASM_BINDGEN_SHA256 = "167c0dd3366b42c3475d8b2f3c97472fecdf98ced4c234ffe6a86ec741f7dad8"
CREDENTIAL_VARIABLES = {
    "ACTIONS_ID_TOKEN_REQUEST_TOKEN",
    "ACTIONS_ID_TOKEN_REQUEST_URL",
    "ACTIONS_RUNTIME_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GH_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    "GITHUB_TOKEN",
    "MISE_GITHUB_TOKEN",
    "SSH_AUTH_SOCK",
}


def run(root, env, *command, stdout=None):
    print("[distribution]", *command, flush=True)
    return subprocess.run(command, cwd=root, env=env, stdout=stdout, text=True, check=True).stdout


def child_environment(revision, tooling=None):
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in CREDENTIAL_VARIABLES and not key.startswith("GIT_CONFIG_")
    }
    for key in ("NAVI_CI_SOURCE_COMMIT", "NAVI_CI_SOURCE_FINGERPRINT", "NAVI_CI_SOURCES"):
        env.pop(key, None)
    env.update(
        GITHUB_SHA=revision,
        GIT_CONFIG_COUNT="1",
        GIT_CONFIG_KEY_0="credential.helper",
        GIT_CONFIG_VALUE_0="",
        GIT_NO_LAZY_FETCH="1",
        GIT_NO_REPLACE_OBJECTS="1",
        GIT_TERMINAL_PROMPT="0",
    )
    if tooling is not None:
        env["NAVI_SDK_TOOLING_SOURCE"] = str(tooling)
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
        # Only approved SDK commits may run alongside the private tooling source.
        run(root, env, "git", "merge-base", "--is-ancestor", revision, "refs/remotes/origin/main")


def tooling_revision(source):
    path = source / "tools/tooling-sources.json"
    if path.is_symlink() or not path.is_file():
        raise ValueError("SDK revision must declare its distribution tooling pin")
    pins = json.loads(path.read_bytes())
    if (
        not isinstance(pins, dict)
        or set(pins) != {REPOSITORY}
        or not isinstance(pins[REPOSITORY], str)
        or not REVISION.fullmatch(pins[REPOSITORY])
    ):
        raise ValueError("SDK distribution tooling pin must be one full lowercase Git SHA")
    return pins[REPOSITORY]


def check_web(source, env, temp):
    name = f"wasm-bindgen-{WASM_BINDGEN_VERSION}-aarch64-apple-darwin"
    url = f"https://github.com/wasm-bindgen/wasm-bindgen/releases/download/{WASM_BINDGEN_VERSION}"
    archive = temp / f"{name}.tar.gz"
    run(source, env, "curl", "-fsSL", "--retry", "3", "-o", archive, f"{url}/{archive.name}")
    with archive.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != WASM_BINDGEN_SHA256:
            raise ValueError("Pinned wasm-bindgen download checksum differs")
    with tarfile.open(archive) as package:
        package.extractall(temp, filter="data")
    env = dict(env, PATH=os.pathsep.join((str(temp / name), env["PATH"])))
    mise = ("mise", "exec", "--")
    run(source, env, *mise, "rustup", "target", "add", "wasm32-unknown-unknown")
    run(
        source,
        env,
        *mise,
        "cargo",
        "fetch",
        "--locked",
        "--manifest-path",
        "bindings/web/rust/Cargo.toml",
    )
    run(source, env, *mise, "make", "check-web")
    run(
        source,
        env,
        *mise,
        "python3",
        "-I",
        "-B",
        ".github/scripts/check-web-browser.py",
        "--artifacts",
        str(temp / "pointer-gestures-artifacts"),
    )


def check_macos(source, sdk, env, temp):
    mise = ("mise", "exec", "--")
    run(source, env, "mise", "trust", "mise.toml")
    run(source, env, "mise", "install")
    run(source, env, "xcrun", "swift", "--version")
    run(source, env, *mise, "cargo", "fetch", "--locked")
    if sdk == "desktop-applications":
        run(
            source,
            env,
            *mise,
            "cargo",
            "fetch",
            "--locked",
            "--manifest-path",
            "fixtures/windows/Cargo.toml",
        )
    run(source, env, *mise, "make", "check-distribution", *MACOS_TARGETS.get(sdk, ()))
    if sdk in DEPENDENCY_CHECKOUTS:
        dependency = "naviapps/" + DEPENDENCY_CHECKOUTS[sdk]
        checkout = temp / DEPENDENCY_CHECKOUTS[sdk]
        run(source, env, "git", "clone", f"https://github.com/{dependency}.git", checkout)
        run(
            source,
            env,
            *mise,
            "python3",
            "-B",
            "tools/install-consumers.py",
            "--git-dependency-source",
            f"{dependency}={checkout}",
        )
    if sdk == "pointer-gestures":
        run(source, env, *mise, "make", "check-consumer")
        check_web(source, env, temp)
    else:
        run(source, env, *mise, "python3", "-B", "tools/consumer.py", "--distribution")


def check_windows(source, sdk, env):
    if sdk in RUST_FORMAT_ON_WINDOWS:
        run(source, env, "cargo", "fmt", "--all", "--", "--check")
    python = (sys.executable, "-E", "-S", "-B")
    run(source, env, *python, "-m", "unittest", "discover", "-s", "tools/tests")
    run(source, env, *python, "tools/windows-ci.py", *WINDOWS_DRIVERS[sdk])


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("pin", "macos", "windows"))
    parser.add_argument("sdk", choices=WINDOWS_DRIVERS)
    parser.add_argument("revision")
    parser.add_argument("--tooling", type=Path)
    args = parser.parse_args(argv)
    source = Path.cwd().resolve()
    env = child_environment(args.revision)
    verify_checkout(source, args.revision, "naviapps/" + args.sdk, env, require_main=True)
    pin = tooling_revision(source)
    if args.mode == "pin":
        with Path(os.environ["GITHUB_OUTPUT"]).open("a", encoding="utf-8") as output:
            print(f"revision={pin}", file=output)
        return
    if args.tooling is None:
        parser.error("Distribution checks require --tooling with the pinned checkout")
    tooling = args.tooling.resolve(strict=True)
    verify_checkout(tooling, pin, REPOSITORY, env)
    env = child_environment(args.revision, tooling)
    if args.mode == "windows":
        check_windows(source, args.sdk, env)
    else:
        with tempfile.TemporaryDirectory(
            dir=env.get("RUNNER_TEMP"), prefix="sdk-distribution-"
        ) as temporary:
            check_macos(source, args.sdk, env, Path(temporary))


if __name__ == "__main__":
    main()
