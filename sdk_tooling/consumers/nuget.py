"""Restore and run an external .NET project from the delivered NuGet packages."""

import argparse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from .. import nuget
from ..adapters import tooling_command
from ..check_options import add_windows_architecture_argument, select_dotnet_host
from ..dotnet import subprocess_environment


def command(*arguments, root=None):
    return tooling_command("nuget-consumer", *arguments, root=root)


def add_arguments(parser):
    parser.add_argument("package", type=Path, help="Local SDK .nupkg to install")
    parser.add_argument(
        "--dependency", action="append", default=[], type=Path, help="Required local feed package"
    )
    add_windows_architecture_argument(parser)


def check_nuget(*, package_id, fixture, work_parent, argv=None, args=None):
    if args is None:
        parser = argparse.ArgumentParser(description=__doc__)
        add_arguments(parser)
        args = parser.parse_args(argv)
    elif argv is not None:
        raise ValueError("Supply either parsed NuGet consumer arguments or argv")
    package = args.package.resolve(strict=True)
    with zipfile.ZipFile(package) as archive:
        specifications = [name for name in archive.namelist() if name.endswith(".nuspec")]
        if len(specifications) != 1:
            raise ValueError("A consumer package must contain one nuspec")
        metadata = ET.fromstring(archive.read(specifications[0]))
        metadata = next(
            (element for element in metadata if element.tag.rsplit("}", 1)[-1] == "metadata"), None
        )
        values = (
            {}
            if metadata is None
            else {element.tag.rsplit("}", 1)[-1]: element.text for element in metadata}
        )
    if values.get("id") != package_id or not values.get("version"):
        raise ValueError("Package identity/version does not match this SDK consumer")
    env = subprocess_environment()
    env["DOTNET_CLI_UI_LANGUAGE"] = "en"
    dotnet, architecture, emulated = select_dotnet_host(env, args.windows_architecture)
    source = (
        Path(fixture).read_text()
        + '\nConsole.WriteLine("Fresh NuGet consumer loaded the packaged native SDK and dependencies.");\n'
    )
    nuget.run_isolated_consumer(
        dotnet,
        packages=[package, *args.dependency],
        references={package_id: values["version"]},
        source=source,
        work_parent=work_parent,
        env=env,
        runtime=f"win-{architecture}" if args.windows_architecture is not None else None,
        properties={
            "Nullable": "enable",
            "TreatWarningsAsErrors": "true",
            "UseSharedCompilation": "false",
        },
    )
    if emulated:
        print("Windows x64 NuGet fixture passed under emulation on ARM64.")
