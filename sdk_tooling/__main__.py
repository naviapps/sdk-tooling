"""Maintain pinned SDK tooling entry points, or invoke a native build explicitly."""

import argparse
import sys
from pathlib import Path


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["windows"] and argv[1:] not in (["-h"], ["--help"]):
        from .hosted_windows import run

        return run(Path.cwd(), argv[1:])

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    from . import binding_checks, quality, sdk_checks

    command = commands.add_parser(
        "quality", help="Check or format first-party source without building the product"
    )
    quality.add_arguments(command)
    command = commands.add_parser("native", help="Build an explicitly described C ABI framework")
    command.add_argument("root", type=Path)
    command.add_argument("--ffi", default="crates/ffi")
    command.add_argument("--symbol-prefix", required=True)
    command.add_argument("--minimum-macos", required=True)
    command.add_argument("--receipt", default="native-build.json")
    command.add_argument("--verify", action="store_true")
    sdk_checks.add_commands(commands)
    binding_checks.add_commands(commands)
    commands.add_parser("windows", help="Run an SDK-owned Windows x64 validation command")
    args = parser.parse_args(argv)
    if handler := getattr(args, "handler", None):
        return handler(args, root=Path.cwd()) or 0
    if args.command == "quality":
        return quality.run_operation(args, root=Path.cwd())

    from .native import NativeBuilder

    builder = NativeBuilder(
        args.root,
        args.ffi,
        symbol_prefix=args.symbol_prefix,
        minimum_macos=args.minimum_macos,
        receipt=args.receipt,
    )
    builder.verify() if args.verify else builder.build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
