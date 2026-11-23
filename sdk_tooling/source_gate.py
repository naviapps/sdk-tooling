"""Verify and identify the exact source checkout used by SDK distribution adapters."""

import hashlib
import os
import sys
from importlib import import_module
from pathlib import Path


def verify(source, pin, git):
    if (
        Path(os.fsdecode(git(source, "rev-parse", "--show-toplevel").strip())).resolve() != source
        or git(source, "rev-parse", "HEAD").decode().strip() != pin
        or git(source, "status", "--porcelain=v1", "--untracked-files=all")
    ):
        raise ValueError(f"SDK Tooling must be a clean checkout at {pin}")
    paths = ("sdk_tooling", "tools/consumer-identity", "tools/native-dep-info")
    inputs = {}
    for record in git(source, "ls-tree", "-rz", "HEAD", "--", *paths).split(b"\0"):
        if not record:
            continue
        metadata, name = record.split(b"\t", 1)
        mode, kind, expected = metadata.split()
        relative = name.decode("utf-8")
        path = source / relative
        if mode not in (b"100644", b"100755") or kind != b"blob" or path.resolve() != path:
            raise ValueError("Invalid SDK Tooling input: " + relative)
        data = path.read_bytes()
        object_id = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
        if object_id.encode() != expected:
            raise ValueError("SDK Tooling source differs from its pinned bytes: " + relative)
        inputs["@sdk-tooling/" + relative] = hashlib.sha256(data).hexdigest()
    if not inputs or any((source / "sdk_tooling").rglob("*.pyc")):
        raise ValueError("SDK Tooling requires ordinary source files without bytecode")
    return source, inputs


def load(source, module):
    existing = sys.modules.get("sdk_tooling")
    if existing is not None and Path(existing.__file__).resolve().parent != source / "sdk_tooling":
        raise RuntimeError("Loaded SDK Tooling differs from the selected source")
    sys.path.insert(0, str(source))
    try:
        return import_module("sdk_tooling." + module)
    finally:
        sys.path.remove(str(source))


def support_inputs(root, checked_inputs):
    inputs = dict(checked_inputs)
    for path in ("mise.toml", "tools/_tooling.py"):
        inputs[path] = hashlib.sha256((root / path).read_bytes()).hexdigest()
    return inputs
