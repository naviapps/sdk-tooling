"""Read an SDK Tooling manifest or a historical split declaration."""

import tomllib
from pathlib import Path

MANIFEST = "sdk-tooling.toml"
LEGACY_MANIFESTS = {
    "native": "native.toml",
    "checks": "sdk-checks.toml",
    "distribution": "sdk-distribution.toml",
}


def manifest_path(root, name):
    root = Path(root).resolve(strict=True)
    unified = root / MANIFEST
    if unified.exists() or unified.is_symlink():
        return unified
    return root / LEGACY_MANIFESTS[name]


def read_section(root, name, *, required=True):
    path = manifest_path(root, name)
    if not path.exists() and not path.is_symlink() and not required:
        return None
    if path.resolve() != path or not path.is_file():
        raise ValueError(f"Expected an ordinary {path.name} at the SDK root")
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    if path.name != MANIFEST:
        return data
    if unknown := data.keys() - LEGACY_MANIFESTS.keys():
        raise ValueError(f"Unknown {MANIFEST} section: {', '.join(sorted(unknown))}")
    if name not in data and not required:
        return None
    value = data.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"Declare [{name}] in {MANIFEST}")
    return value
