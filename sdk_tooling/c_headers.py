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


def _package(ffi):
    manifest = tomllib.loads(_ordinary_path(ffi / "Cargo.toml").read_text(encoding="utf-8"))
    package = manifest["package"]
    settings = package.get("metadata", {}).get("c-bindings", {})
    if (
        not isinstance(settings, dict)
        or set(settings) - {"module", "umbrella", "generated"}
        or any(
            not isinstance(settings.get(flag, False), bool) for flag in ("umbrella", "generated")
        )
    ):
        raise ValueError(f"Invalid Cargo package.metadata.c-bindings in {ffi}")
    return package["name"], settings


def _generated_products(root, product=None):
    root, products = product_options(root, required=False)
    if not products:
        return root, []
    if product is not None:
        products = [select_product(products, product)]
    else:
        products = products.values()
    return root, [
        options
        for options in products
        if _package(root / options.get("ffi", "crates/ffi"))[1].get("generated", False)
    ]


def has_generated_products(root):
    """Whether the SDK opted into build-time C header generation."""
    return bool(_generated_products(root)[1])


def _render_header(root, executable, config, source, package, env):
    config = _ordinary_path(config)
    _ordinary_path(source)
    settings = tomllib.loads(config.read_text(encoding="utf-8"))
    command = [executable, "--config", str(config)]
    parse_dependencies = settings.get("parse", {}).get("parse_deps", False)
    if parse_dependencies and source.name != "lib.rs":
        raise ValueError(f"Dependency parsing requires the crate entry point: {config}")
    process = {"cwd": root}
    if env is not None:
        process["env"] = env
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
                    **process,
                )
            except subprocess.CalledProcessError as error:
                raise RuntimeError(
                    "Could not resolve locked Cargo metadata for cbindgen"
                ) from error
            metadata.write_bytes(locked)
            command.extend(("--crate", package, "--metadata", str(metadata)))
        try:
            raw = subprocess.check_output(command, **process)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"cbindgen failed for {config.relative_to(root)}") from error
    data = raw.decode("utf-8").replace("\r\n", "\n")
    if not data.strip() or not data.endswith("\n") or "\r" in data or "\0" in data:
        raise ValueError(f"cbindgen returned invalid header text for {config.relative_to(root)}")
    return data.encode("utf-8")


def _render(root, products, executable, env=None):
    outputs = {}
    retained = {}
    generated = {}
    for product in products:
        ffi = root / product.get("ffi", "crates/ffi")
        package, settings = _package(ffi)
        module = settings.get("module")
        if not isinstance(module, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", module):
            raise ValueError(
                f"Declare a Swift module name in {ffi}/Cargo.toml package.metadata.c-bindings"
            )
        prefix = product["symbol_prefix"]
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", prefix) or not prefix.removesuffix("_"):
            raise ValueError(f"Invalid C symbol prefix: {prefix}")
        module_map = ffi / "module.modulemap"
        if module_map in outputs:
            raise ValueError(f"C header generation requires distinct FFI crates: {ffi}")
        name = prefix.removesuffix("_") + ".h"
        header = ffi / "include" / name
        keyword = "umbrella header" if settings.get("umbrella", True) else "header"
        files = {
            module_map: f'framework module {module} {{\n  {keyword} "{name}"\n  export *\n}}\n'.encode(),
            header: _render_header(
                root, executable, ffi / "cbindgen.toml", ffi / "src/lib.rs", package, env
            ),
        }
        windows = ffi / "cbindgen-windows.toml"
        if windows.exists() or windows.is_symlink():
            files[ffi / "include/windows" / (prefix + "windows.h")] = _render_header(
                root, executable, windows, ffi / "src/windows.rs", package, env
            )
        outputs.update(files)
        if settings.get("generated", False):
            framework = (
                root
                / "Artifacts"
                / (module + ".xcframework")
                / "macos-arm64_x86_64"
                / (module + ".framework")
                / "Versions/A"
            )
            retained[framework / "Modules/module.modulemap"] = files[module_map]
            retained[framework / "Headers" / name] = files[header]
            generated.update(files)
        else:
            retained.update(files)
    return outputs, retained, generated


def _different(outputs):
    return [
        path
        for path, data in outputs.items()
        if not _ordinary_path(path, allow_missing=True).is_file() or path.read_bytes() != data
    ]


def _publish(root, outputs):
    if not _different(outputs):
        return
    build = root / ".native-build"
    if build.is_symlink():
        raise ValueError("Native build directory must not be a symlink")
    with (
        build_lock(build / "c-headers"),
        temporary_workspace(None, prefix="sdk-c-headers-") as stage,
    ):
        for index, path in enumerate(_different(outputs)):
            source = stage / str(index)
            source.write_bytes(outputs[path])
            stage_file(source, path)


def _verify_retained(root, retained):
    if changed := _different(retained):
        names = ", ".join(path.relative_to(root).as_posix() for path in changed)
        raise ValueError(
            f"Generated C headers or module maps differ: {names}. "
            "Run sdk-tooling c-headers generate or rebuild the affected XCFramework."
        )


def prepare_generated(root, *, env=None, product=None, verify=False):
    """Generate opted-in declarations, optionally verifying the bundled copies first."""
    root, products = _generated_products(root, product)
    if products:
        executable = quality.tool(root, "cbindgen", "cargo:cbindgen", env=env)
        outputs, retained, _ = _render(root, products, executable, env)
        if verify:
            _verify_retained(root, retained)
        _publish(root, outputs)


def run_operation(args, *, root):
    root, products = product_options(root)
    selected = [select_product(products, args.product)] if args.product else products.values()
    executable = quality.tool(root, "cbindgen", "cargo:cbindgen")
    outputs, retained, generated = _render(root, selected, executable)
    if args.operation == "generate":
        _publish(root, outputs)
        return
    _verify_retained(root, retained)
    _publish(root, generated)


def add_command(commands):
    command = commands.add_parser("c-headers", help="Generate or verify C headers and module maps")
    command.add_argument("operation", choices=["check", "generate"])
    command.add_argument("--product", help="Select a declared native product")
    command.set_defaults(handler=run_operation)
