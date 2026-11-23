"""Maintain pinned SDK tooling entry points, or invoke a native build explicitly."""

import argparse
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    from . import quality

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
    args = parser.parse_args(argv)
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
