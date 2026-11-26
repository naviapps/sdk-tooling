"""Build, check and package native SDKs."""

import argparse
import subprocess
import sys
from pathlib import Path


def _xcframework(args, *, root):
    from .c_headers import prepare_generated
    from .xcframework import xcframework, xcframeworks

    root = args.root or root
    prepare_generated(root, product=args.product, verify=args.verify)
    products = [xcframework(root, args.product)] if args.product else xcframeworks(root).values()
    for product in products:
        if args.verify:
            product.verify()
        else:
            product.build()
    return 0


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["windows"] and argv[1:] not in (["-h"], ["--help"]):
        from .hosted_windows import run

        return run(Path.cwd(), argv[1:])

    if len(argv) > 1 and argv[0] == "python" and argv[1:] not in (["-h"], ["--help"]):
        from .adapters import python_command

        root = Path.cwd()
        return subprocess.run(python_command(root, *argv[1:]), cwd=root).returncode

    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    from . import (
        binding_checks,
        c_headers,
        clean,
        distribution,
        quality,
        sdk_checks,
        windows_bindings,
        xcframework_archive,
    )

    command = commands.add_parser(
        "quality", help="Check or format first-party source without building the product"
    )
    quality.add_arguments(command)
    command.set_defaults(handler=quality.run_operation)
    command = commands.add_parser("clean", help="Remove declared local SDK build directories")
    command.add_argument("paths", nargs="+", help="Checkout-relative ignored build directories")
    command.set_defaults(handler=clean.clean)
    command = commands.add_parser(
        "xcframework", help="Build or verify macOS XCFrameworks declared in sdk-tooling.toml"
    )
    command.add_argument("root", nargs="?", type=Path)
    command.add_argument("--product", help="Select a declared native product")
    command.add_argument("--verify", action="store_true")
    command.set_defaults(handler=_xcframework)
    command = commands.add_parser(
        "xcframework-archive", help="Archive verified XCFrameworks and native build receipts"
    )
    command.add_argument("--output", type=Path, required=True, help="New directory outside the SDK")
    command.add_argument("--revision", help="Exact SDK commit; defaults to HEAD")
    command.add_argument("--producer", type=Path, help="Pinned SDK Tooling producer checkout")
    command.set_defaults(handler=xcframework_archive.archive_command)
    c_headers.add_command(commands)
    distribution.add_command(commands)
    sdk_checks.add_commands(commands)
    binding_checks.add_commands(commands)
    windows_bindings.add_command(commands)
    commands.add_parser("windows", help="Run an SDK-owned Windows x64 validation command")
    commands.add_parser("python", help="Run an SDK-owned Python fixture or -m module")
    args = parser.parse_args(argv)
    if handler := getattr(args, "handler", None):
        return handler(args, root=Path.cwd()) or 0
    parser.error("Specify an SDK Python script or -m module")


if __name__ == "__main__":
    sys.exit(main())
