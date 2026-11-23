"""Maintain pinned SDK tooling entry points, or invoke a native build explicitly."""

import argparse
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["windows"] and argv[1:] not in (["-h"], ["--help"]):
        from .hosted_windows import run

        return run(Path.cwd(), argv[1:])

    if argv[:1] == ["python"] and argv[1:] not in (["-h"], ["--help"]):
        from .adapters import python_command

        root = Path.cwd()
        subprocess.run(python_command(root, *argv[1:]), cwd=root, check=True)
        return 0

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    from . import binding_checks, quality, sdk_checks

    command = commands.add_parser(
        "quality", help="Check or format first-party source without building the product"
    )
    quality.add_arguments(command)
    command = commands.add_parser("native", help="Build an explicitly described C ABI framework")
    command.add_argument("root", nargs="?", type=Path, default=Path.cwd())
    command.add_argument("--product", help="Select one product in native.toml")
    command.add_argument("--ffi")
    command.add_argument("--symbol-prefix")
    command.add_argument("--minimum-macos")
    command.add_argument("--receipt")
    command.add_argument("--verify", action="store_true")
    sdk_checks.add_commands(commands)
    binding_checks.add_commands(commands)
    commands.add_parser("windows", help="Run an SDK-owned Windows x64 validation command")
    commands.add_parser("python", help="Run an SDK-owned Python fixture or -m module")
    args = parser.parse_args(argv)
    if handler := getattr(args, "handler", None):
        return handler(args, root=Path.cwd()) or 0
    if args.command == "quality":
        return quality.run_operation(args, root=Path.cwd())

    if args.command != "native":
        parser.error("Specify an SDK Python script or -m module")

    if any(
        value is not None
        for value in (args.ffi, args.symbol_prefix, args.minimum_macos, args.receipt)
    ):
        from .native import NativeBuilder

        if args.product or not (args.symbol_prefix and args.minimum_macos):
            parser.error("Explicit native builds require --symbol-prefix and --minimum-macos")
        selected = [
            NativeBuilder(
                args.root,
                args.ffi or "crates/ffi",
                symbol_prefix=args.symbol_prefix,
                minimum_macos=args.minimum_macos,
                receipt=args.receipt or "native-build.json",
            )
        ]
    else:
        from .native_plan import builder, builders

        selected = (
            [builder(args.root, args.product)] if args.product else builders(args.root).values()
        )
    for product in selected:
        product.verify() if args.verify else product.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
