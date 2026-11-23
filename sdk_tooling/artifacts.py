"""Source fingerprints and transactional publication of generated SDK artifacts."""

import errno
import hashlib
import json
import os
import shutil
import tempfile
from contextlib import contextmanager
from pathlib import Path


@contextmanager
def build_lock(directory):
    """One publisher per SDK checkout; unrelated checkouts remain independent."""
    directory = Path(directory)
    if directory.is_symlink():
        raise ValueError("Native build directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "native.lock"
    if path.is_symlink():
        raise ValueError("Native build lock must be an ordinary file")
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            if path.stat().st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            contention = (errno.EACCES,)
            acquire = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            contention = (errno.EAGAIN, errno.EWOULDBLOCK)
            acquire = lambda: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(stream, fcntl.LOCK_UN)
        try:
            acquire()
        except OSError as error:
            if error.errno in contention:
                raise RuntimeError(f"Another native build owns this SDK checkout: {path}") from None
            error.add_note(f"Could not acquire native build lock: {path}")
            raise
        try:
            yield
        finally:
            stream.seek(0)
            release()


def hashes(paths, root):
    root = Path(root).resolve()
    result = {}
    for path in sorted(set(map(Path, paths))):
        # Normalize parent aliases such as /var -> /private/var while hashing a
        # file symlink itself rather than the file it names.
        path = path.parent.resolve() / path.name
        relative = path.relative_to(root).as_posix()
        data = ("symlink:" + os.readlink(path)).encode() if path.is_symlink() else path.read_bytes()
        result[relative] = hashlib.sha256(data).hexdigest()
    return result


def file_sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def stage_file(source, destination):
    """Atomically stage unchanged bytes and return the accepted content digest."""
    source = Path(source).resolve(strict=True)
    if destination.is_symlink() or source == destination.resolve():
        raise ValueError("Native staging requires a separate regular-file destination")
    before = file_sha256(source)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="native-stage-") as directory:
        staged = Path(directory) / destination.name
        shutil.copyfile(source, staged)
        if file_sha256(staged) != before or file_sha256(source) != before:
            raise ValueError("Native staging changed the accepted artifact bytes")
        os.replace(staged, destination)
    return before


def stage_native(source, destination):
    """Stage an accepted native library and return the path for its consumer."""
    destination = Path(destination)
    stage_file(source, destination)
    return destination


def files(root):
    return sorted(path for path in Path(root).rglob("*") if path.is_file() or path.is_symlink())


def write_json(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + ".pending")
    owned = False
    try:
        with temporary.open("x", encoding="utf-8") as output:
            owned = True
            json.dump(value, output, indent=2, sort_keys=True)
            output.write("\n")
        os.replace(temporary, path)
    finally:
        if owned:
            temporary.unlink(missing_ok=True)


def publish(stage, destination, verify):
    """Swap a complete staged directory, retaining the old tree if rollback fails."""
    stage, destination = Path(stage), Path(destination)
    if (
        stage.is_symlink()
        or not stage.is_dir()
        or destination.is_symlink()
        or (destination.exists() and not destination.is_dir())
    ):
        raise ValueError("Artifact publication requires ordinary staged/destination directories")
    source_path, target_path = stage.resolve(), destination.resolve()
    if source_path.is_relative_to(target_path) or target_path.is_relative_to(source_path):
        raise ValueError("Artifact staging and destination must be disjoint")

    rejected = stage.parent / "rejected-artifacts"
    if rejected.exists() or rejected.is_symlink():
        raise FileExistsError("Artifact publication staging is already occupied")
    recovery = None
    backup = None
    try:
        if destination.exists():
            # The caller disposes of its staging directory even when rollback raises.
            recovery = Path(
                tempfile.mkdtemp(prefix=f".{destination.name}.rollback-", dir=destination.parent)
            )
            backup = recovery / "previous-artifacts"
            os.replace(destination, backup)
        os.replace(stage, destination)
        verify()
    except BaseException:
        try:
            if not stage.exists() and destination.exists():
                os.replace(destination, rejected)
            if backup is not None and backup.exists():
                os.replace(backup, destination)
        except BaseException as rollback_error:
            if backup is not None and backup.exists():
                rollback_error.add_note(f"Previous artifacts retained at {backup}")
            raise
        if recovery is not None:
            recovery.rmdir()
        raise
    if recovery is not None:
        shutil.rmtree(recovery)
