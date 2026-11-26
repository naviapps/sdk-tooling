"""Read and execute a repository's distribution plan."""

import json
import subprocess
import sys
import tomllib
from pathlib import Path

from .adapters import python_command, tooling_command
from .manifest import MANIFEST, read_section
from .workspaces import temporary_workspace

PLATFORMS = ("macos", "windows", "linux")
HOSTS = {"darwin": "macos", "win32": "windows", "linux": "linux"}


def ordinary_file(root, value):
    if not isinstance(value, (str, Path)):
        raise ValueError("Select a checkout-relative file")
    relative = Path(value)
    path = root / relative
    if (
        relative.is_absolute()
        or not relative.parts
        or ".." in relative.parts
        or path.resolve() != path
        or not path.is_file()
    ):
        raise ValueError(f"Expected an ordinary file inside the checkout: {value}")
    return path


def _argument(value, *, allow_blank=False):
    return (
        isinstance(value, str)
        and bool(value if allow_blank else value.strip())
        and value.isprintable()
    )


def platform_commands(data, platforms, *, allow_blank_arguments=False):
    """Read argv arrays shared by local and hosted distribution checks."""
    if not data or data.keys() - set(platforms):
        raise ValueError(f"Declare distribution commands under {' or '.join(platforms)}")

    plan = {}
    for platform, settings in data.items():
        if not isinstance(settings, dict) or settings.keys() - {"commands"}:
            raise ValueError(f"Declare commands for {platform}")
        commands = settings.get("commands")
        if (
            not isinstance(commands, list)
            or not commands
            or not all(
                isinstance(argv, list)
                and argv
                and _argument(argv[0])
                and all(_argument(value, allow_blank=allow_blank_arguments) for value in argv[1:])
                for argv in commands
            )
        ):
            raise ValueError(f"Distribution commands for {platform} must be nonempty argv arrays")
        plan[platform] = commands
    return plan


def load_plan(root, manifest=None):
    if manifest is None:
        data = read_section(root, "distribution")
    else:
        path = ordinary_file(root, manifest)
        data = (
            read_section(root, "distribution")
            if path == root / MANIFEST
            else tomllib.loads(path.read_text(encoding="utf-8"))
        )
    packaging = data.pop("nuget", None)
    plan = platform_commands(data, PLATFORMS)
    if packaging is not None:
        if not isinstance(packaging, dict) or packaging.keys() != {"pack"}:
            raise ValueError("Declare the SDK's NuGet packer under nuget.pack")
        script = ordinary_file(root, packaging["pack"])
        if script.suffix != ".py":
            raise ValueError("nuget.pack must select a Python script")
        plan["nuget"] = {"pack": script.relative_to(root).as_posix()}
    return plan


def execute(plan, root, platform):
    if platform not in plan:
        raise ValueError(f"No distribution commands declared for {platform}")
    if platform != HOSTS.get(sys.platform):
        raise ValueError(f"The {platform} plan requires a {platform} host")
    with temporary_workspace(None, prefix="sdk-distribution-") as scratch:
        for command, *arguments in plan[platform]:
            arguments = [argument.replace("{temp}", str(scratch)) for argument in arguments]
            if command == "sdk-tooling":
                argv = tooling_command(*arguments, root=root)
            elif command == "python":
                argv = python_command(root, *arguments)
            else:
                argv = [command, *arguments]
            print("[distribution]", command, *arguments, flush=True)
            subprocess.run(argv, cwd=root, check=True)


def add_command(commands):
    command = commands.add_parser(
        "distribution", help="Inspect or run a repository's distribution plan"
    )
    operations = command.add_subparsers(dest="operation", required=True)
    plan = operations.add_parser("plan", help="Read and print the commands as JSON")
    check = operations.add_parser("check", help="Execute the current host's declared commands")
    check.add_argument("--platform", choices=PLATFORMS, help="Require the selected host platform")
    pack = operations.add_parser("pack-nuget", help="Invoke the SDK's declared NuGet packer")
    pack.add_argument("--runtime", action="append", required=True, help="NuGet runtime identifier")
    pack.add_argument("--result", type=Path, help="New file recording the resulting package names")
    for operation in (plan, check, pack):
        operation.add_argument("--root", type=Path, help="SDK checkout directory; defaults to cwd")
        operation.add_argument(
            "--manifest", help=f"Path within the checkout; defaults to {MANIFEST}"
        )
        operation.set_defaults(handler=run_operation)


def run_operation(args, *, root):
    try:
        result = args.result.absolute() if getattr(args, "result", None) else None
        root = (args.root or root).resolve(strict=True)
        plan = load_plan(root, args.manifest)
        if args.operation == "plan":
            print(json.dumps(plan, indent=2, sort_keys=True))
        elif args.operation == "check":
            execute(plan, root, args.platform or HOSTS.get(sys.platform))
        else:
            if "nuget" not in plan:
                raise ValueError("Declare the SDK's NuGet packer under nuget.pack")
            if len(set(args.runtime)) != len(args.runtime) or not all(map(_argument, args.runtime)):
                raise ValueError("Select distinct nonempty NuGet runtime identifiers")
            options = [item for runtime in args.runtime for item in ("--runtime", runtime)]
            if result is not None:
                if result.exists() or result.is_symlink():
                    raise ValueError("The NuGet result path must not already exist")
                options.extend(("--result", str(result)))
            command = python_command(root, plan["nuget"]["pack"], *options)
            subprocess.run(command, cwd=root, check=True)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None
