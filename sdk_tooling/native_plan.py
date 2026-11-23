"""Select native C ABI products from the SDK's declared build specification."""

import re
import tomllib
from pathlib import Path, PurePosixPath, PureWindowsPath

from .native import NativeBuilder

SPECIFICATION = "native.toml"
_OPTIONS = {
    "ffi",
    "symbol_prefix",
    "minimum_macos",
    "receipt",
    "notice_packages",
    "notice_directory",
    "target_directory",
}


def _inside(root, path, label):
    relative = PurePosixPath(path)
    if (
        not relative.parts
        or relative.is_absolute()
        or relative.as_posix() != path
        or ".." in relative.parts
        or "\\" in path
        or PureWindowsPath(path).drive
        or not (root / relative).resolve().is_relative_to(root)
    ):
        raise ValueError(f"Native {label} must be a canonical path inside the SDK")
    return path


def builders(root=None, *, tooling=None):
    root = Path(root or Path.cwd()).resolve(strict=True)
    spec = root / SPECIFICATION
    if spec.is_symlink() or not spec.is_file():
        raise ValueError(f"Expected an ordinary {SPECIFICATION} at the SDK root")
    config = tomllib.loads(spec.read_text(encoding="utf-8"))
    if set(config) != {"artifacts"} or not isinstance(config["artifacts"], list):
        raise ValueError(f"Declare [[artifacts]] in {SPECIFICATION}")
    constructor = tooling("native").NativeBuilder if tooling else NativeBuilder
    products = {}
    for artifact in config["artifacts"]:
        if not isinstance(artifact, dict) or set(artifact) - (_OPTIONS | {"id"}):
            raise ValueError("Unknown native artifact setting")
        name = artifact.get("id")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9-]*", name):
            raise ValueError("Native artifact IDs must be lowercase names")
        if name in products:
            raise ValueError("Duplicate native artifact ID: " + name)
        options = {key: value for key, value in artifact.items() if key != "id"}
        if not {"symbol_prefix", "minimum_macos"} <= options.keys():
            raise ValueError("Native artifacts require a C prefix and minimum macOS version")
        for key, value in options.items():
            if key == "notice_packages":
                if (
                    not isinstance(value, list)
                    or not value
                    or any(not isinstance(package, str) or not package for package in value)
                    or len(set(value)) != len(value)
                ):
                    raise ValueError("Native notice packages must be distinct Cargo package names")
            elif not isinstance(value, str) or not value:
                raise ValueError(f"Native {key} must be a nonempty string")
            elif key in {"ffi", "notice_directory", "target_directory"}:
                _inside(root, value, key)
        products[name] = constructor(root, specification=SPECIFICATION, **options)
    if not products:
        raise ValueError("Declare at least one native artifact")
    for key in ("module", "receipt"):
        if len({getattr(product, key) for product in products.values()}) != len(products):
            raise ValueError(f"Native artifacts must have distinct {key} values")
    return products


def builder(root=None, kind=None, *, tooling=None):
    products = builders(root, tooling=tooling)
    if kind is not None:
        if kind not in products:
            raise ValueError("Unknown native artifact: " + kind)
        return products[kind]
    if len(products) != 1:
        raise ValueError("Select one of the native artifacts: " + ", ".join(products))
    return next(iter(products.values()))
