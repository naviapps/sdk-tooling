"""Remove declared build outputs from the current SDK checkout."""

import shutil
import stat
import subprocess
import unicodedata
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from .artifacts import build_lock
from .consumers.workspace import _git


def _rollback(name):
    return name.startswith(".") and ".rollback-" in name


def _relative(name):
    path = PurePosixPath(name)
    if (
        not path.parts
        or path.is_absolute()
        or "\\" in name
        or ":" in name
        or any(
            part == ".."
            or part.casefold() == ".git"
            or part.endswith((".", " "))
            or _rollback(part)
            for part in path.parts
        )
    ):
        raise ValueError(f"Expected a checkout-relative build directory: {name}")
    return path


def _git_key(name):
    return unicodedata.normalize("NFC", name).casefold()


def _ignored(root, path, tracked):
    name = path.as_posix()
    keys = (_git_key(entry.as_posix()) for entry in (path, *path.parents))
    if tracked.intersection(keys) or any(item.startswith(_git_key(name) + "/") for item in tracked):
        raise ValueError(f"Build directory overlaps tracked files: {name}")
    try:
        rule = _git(root, "check-ignore", "--no-index", "--verbose", "--", name + "/")
    except subprocess.CalledProcessError as error:
        if error.returncode != 1:
            raise
        rule = ""
    source, _, pattern = rule.partition("\t")[0].split(":", 2) if rule else ("", "", "")
    if (
        PurePosixPath(source).name != ".gitignore"
        or _git_key(source) not in tracked
        or pattern.startswith("!")
    ):
        raise ValueError(f"Build directory must be ignored by a tracked .gitignore: {name}")


def _ordinary_directory(path):
    if path.is_symlink() or path.is_junction() or path.is_mount():
        raise ValueError(f"Build directory must be inside the checkout: {path}")
    if path.exists() and not path.is_dir():
        raise ValueError(f"Build directory has an unexpected file: {path}")


def _checkout_directories(root, path):
    relative = path.relative_to(root)
    for part in (*reversed(relative.parents[:-1]), relative):
        directory = root / part
        _ordinary_directory(directory)
        yield directory


def _lock_file(path, locks):
    lock = path / "native.lock"
    try:
        info = lock.lstat()
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise ValueError(f"Expected an ordinary native build lock: {lock}")
    locks.add(path)


def _protected(root, paths):
    locks, retained = set(), set()
    for path in paths:
        for directory in _checkout_directories(root, path):
            _lock_file(directory, locks)
        if not path.exists():
            continue
        if _git_key(path.relative_to(root).as_posix()) == ".native-build":
            locks.add(path)
        for directory, subdirectories, files in path.walk(on_error=_raise_walk_error):
            _lock_file(directory, locks)
            for name in tuple(subdirectories):
                child = directory / name
                if _rollback(name):
                    retained.add(child)
                elif not child.is_junction():
                    _ordinary_directory(child)
                    continue
                subdirectories.remove(name)
            retained.update(directory / name for name in files if _rollback(name))
    retained.update(directory / "native.lock" for directory in locks)
    return locks, retained


def _raise_walk_error(error):
    raise error


def _remove(path, retained, ancestors):
    if path in retained or path.name.casefold() == "native.lock":
        return
    if path in ancestors:
        _ordinary_directory(path)
        for child in path.iterdir():
            _remove(child, retained, ancestors)
    elif path.is_junction():
        path.rmdir()
    elif not path.is_symlink() and path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()


def clean(args, *, root):
    root = root.resolve()
    if Path(_git(root, "rev-parse", "--show-toplevel")).resolve() != root:
        raise ValueError("Run clean from the SDK checkout root")
    tracked = {
        _git_key(name)
        for command in (("ls-files", "-z"), ("ls-tree", "-rz", "--name-only", "HEAD"))
        for name in _git(root, *command).split("\0")
    }
    relative = {_relative(name) for name in args.paths}
    for path in sorted(relative):
        for _ in _checkout_directories(root, root / path):
            pass
        _ignored(root, path, tracked)
    paths = sorted(root / path for path in relative if not relative.intersection(path.parents))
    locks, _ = _protected(root, paths)
    with ExitStack() as active:
        for directory in sorted(locks):
            active.enter_context(build_lock(directory))
        current_locks, retained = _protected(root, paths)
        if current_locks != locks:
            raise RuntimeError("Native build locks changed during cleanup. Run clean again.")
        ancestors = {parent for path in retained for parent in path.parents}
        for path in paths:
            if path.exists():
                _remove(path, retained, ancestors)
