"""Generate C declarations and Swift module maps from SDK-owned Rust ABI sources."""

import re
import subprocess
import tomllib
from contextlib import nullcontext

from . import quality
from .artifacts import build_lock, stage_file
from .native_plan import product_options, select_product
from .workspaces import temporary_workspace


def _ordinary_path(path, *, allow_missing=False):
    if (
        path.resolve() != path
        or (not path.is_file() and (path.exists() or not allow_missing))
        or any(parent.exists() and not parent.is_dir() for parent in path.parents)
    ):
        raise ValueError(f"C header generation requires an ordinary file: {path}")
    return path


def _render_header(root, executable, config, source, package):
    config = _ordinary_path(config)
    _ordinary_path(source)
    settings = tomllib.loads(config.read_text(encoding="utf-8"))
    command = [executable, "--config", str(config)]
    parse_dependencies = settings.get("parse", {}).get("parse_deps", False)
    if parse_dependencies and source.name != "lib.rs":
        raise ValueError(f"Dependency parsing requires the crate entry point: {config}")
    with (
        temporary_workspace(None, prefix="sdk-cbindgen-")
        if parse_dependencies
        else nullcontext() as stage
    ):
        if stage is None:
            command.append(str(source))
        else:
            metadata = stage / "metadata.json"
            try:
                locked = subprocess.check_output(
                    ["cargo", "metadata", "--locked", "--all-features", "--format-version", "1"],
                    cwd=root,
                )
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    "Could not resolve locked Cargo metadata for cbindgen"
                ) from error
            metadata.write_bytes(locked)
            command.extend(("--crate", package, "--metadata", str(metadata)))
        try:
            raw = subprocess.check_output(command, cwd=root)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"cbindgen failed for {config.relative_to(root)}") from error
    data = raw.decode("utf-8").replace("\r\n", "\n")
    if not data.strip() or not data.endswith("\n") or "\r" in data or "\0" in data:
        raise ValueError(f"cbindgen returned invalid header text for {config.relative_to(root)}")
    return data.encode("utf-8")


def _render(root, products, executable):
    outputs = {}
    for product in products:
        ffi = root / product.get("ffi", "crates/ffi")
        manifest = tomllib.loads(_ordinary_path(ffi / "Cargo.toml").read_text(encoding="utf-8"))
        package = manifest["package"]
        settings = package.get("metadata", {}).get("c-bindings", {})
        if (
            not isinstance(settings, dict)
            or set(settings) - {"module", "umbrella"}
            or not isinstance(settings.get("umbrella", True), bool)
        ):
            raise ValueError(f"Invalid Cargo package.metadata.c-bindings in {ffi}")
        module = settings.get("module")
        if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            raise ValueError(
                f"Declare a Swift module name in {ffi}/Cargo.toml package.metadata.c-bindings"
            )
        prefix = product["symbol_prefix"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prefix) or not prefix.removesuffix("_"):
            raise ValueError(f"Invalid C symbol prefix: {prefix}")
        if ffi / "module.modulemap" in outputs:
            raise ValueError(f"C header generation requires distinct FFI crates: {ffi}")
        name = prefix.removesuffix("_") + ".h"
        keyword = "umbrella header" if settings.get("umbrella", True) else "header"
        outputs[ffi / "module.modulemap"] = (
            f'framework module {module} {{\n  {keyword} "{name}"\n  export *\n}}\n'.encode()
        )
        outputs[ffi / "include" / name] = _render_header(
            root, executable, ffi / "cbindgen.toml", ffi / "src/lib.rs", package["name"]
        )
        windows = ffi / "cbindgen-windows.toml"
        if windows.exists() or windows.is_symlink():
            outputs[ffi / "include/windows" / (prefix + "windows.h")] = _render_header(
                root, executable, windows, ffi / "src/windows.rs", package["name"]
            )
    return outputs


def run_operation(args, *, root):
    root, products = product_options(root)
    selected = [select_product(products, args.product)] if args.product else products.values()
    executable = quality.tool(root, "cbindgen", "cargo:cbindgen")
    generate = args.operation == "generate"
    with build_lock(root / ".native-build") if generate else nullcontext():
        outputs = _render(root, selected, executable)
        changed = [
            path
            for path, data in outputs.items()
            if not _ordinary_path(path, allow_missing=True).is_file() or path.read_bytes() != data
        ]
        if changed and not generate:
            names = ", ".join(path.relative_to(root).as_posix() for path in changed)
            raise ValueError(
                f"Generated C headers or module maps differ: {names}. "
                "Run sdk-tooling c-headers generate in this SDK."
            )
        if changed:
            with temporary_workspace(None, prefix="sdk-c-headers-") as stage:
                sources = {path: stage / str(index) for index, path in enumerate(changed)}
                for path, source in sources.items():
                    source.write_bytes(outputs[path])
                for path, source in sources.items():
                    stage_file(source, path)


def add_command(commands):
    command = commands.add_parser("c-headers", help="Generate or verify C headers and module maps")
    command.add_argument("operation", choices=["check", "generate"])
    command.add_argument("--product", help="Select one product in native.toml")
    command.set_defaults(handler=run_operation)
