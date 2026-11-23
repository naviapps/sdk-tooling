"""Isolate independently delivered Rust and C fixture workspaces."""

import os
import re
import subprocess
import sys


def _run(command, *, cwd, env):
    try:
        return subprocess.run(command, cwd=cwd, env=env, check=True, text=True, capture_output=True)
    except subprocess.CalledProcessError as error:
        print(error.stdout or "", end="", file=sys.stderr)
        print(error.stderr or "", end="", file=sys.stderr)
        raise


def _git(root, *arguments, env=None):
    if env is None:
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        env.update(GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
    return _run(
        ["git", "-c", "core.hooksPath=" + os.devnull, "-c", "core.fsmonitor=false", *arguments],
        cwd=root,
        env=dict(env, GIT_NO_REPLACE_OBJECTS="1", GIT_NO_LAZY_FETCH="1"),
    ).stdout.strip()


def _source_revision(root, requested):
    revision, tree = _git(root, "rev-parse", "HEAD", "HEAD^{tree}").splitlines()
    if not re.fullmatch(r"[a-f0-9]{40}", requested if requested is not None else revision):
        raise ValueError("Provide a full 40-character SDK commit ID")
    if requested is not None and requested != revision:
        raise ValueError("The producer source HEAD must equal the requested SDK revision")
    if _git(root, "status", "--porcelain=v1", "--untracked-files=all"):
        raise ValueError("The producer source must be a clean committed SDK tree")
    return revision, tree


def _environment(work, *, rust=False, windows_architecture=None):
    allowed = {"HOME", "CFFIXED_USER_HOME", "DEVELOPER_DIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ"}
    if rust:
        allowed.update(
            {
                "PATH",
                "RUSTUP_HOME",
                "HTTPS_PROXY",
                "HTTP_PROXY",
                "NO_PROXY",
                "SYSTEMROOT",
                "WINDIR",
                "PATHEXT",
                "COMSPEC",
            }
        )
    if windows_architecture:
        allowed.update(
            {
                "ALLUSERSPROFILE",
                "APPDATA",
                "LOCALAPPDATA",
                "PROCESSOR_ARCHITECTURE",
                "PROCESSOR_ARCHITEW6432",
                "PROGRAMDATA",
                "PROGRAMFILES",
                "PROGRAMFILES(X86)",
                "PROGRAMW6432",
                "SYSTEMDRIVE",
                "USERPROFILE",
            }
        )
        env = {key.upper(): value for key, value in os.environ.items() if key.upper() in allowed}
    else:
        env = {key: value for key, value in os.environ.items() if key in allowed}
    env.setdefault("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    for key, name in {
        "TMPDIR": "tmp",
        "XDG_CONFIG_HOME": "config",
        "XDG_CACHE_HOME": "cache",
    }.items():
        path = work / name
        path.mkdir()
        env[key] = str(path)
    if os.name == "nt" or windows_architecture:
        env.update(TEMP=env["TMPDIR"], TMP=env["TMPDIR"])
    if rust:
        config = work / "gitconfig"
        config.write_text("", encoding="utf-8")
        env.update(
            GIT_CONFIG_GLOBAL=str(config),
            GIT_CONFIG_NOSYSTEM="1",
            GIT_TERMINAL_PROMPT="0",
            GIT_ALLOW_PROTOCOL="file",
            GIT_NO_REPLACE_OBJECTS="1",
            GIT_NO_LAZY_FETCH="1",
        )
    return env
