"""Build, audit, and exercise NuGet packages containing accepted native assets."""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from contextlib import ExitStack
from pathlib import Path, PurePosixPath

from . import artifacts, windows
from . import dotnet as dotnet_tools
from .artifacts import build_lock, file_sha256, hashes, stage_file, write_json
from .workspaces import temporary_workspace

_PACKAGE_CHUNK_BYTES = 256 * 1024
_WINDOWS_PREFIX_OVERLAP = len(b"a:\\users\\") - 1
_WINDOWS_USER_PATH = re.compile(
    rb"[a-z]:\\users\\(?:[^\\\x00]+(?P<end>\\)|(?P<partial>[^\\\x00]*)\Z)"
)
_WINDOWS_USER_END = re.compile(rb"[\\\x00]")


class _PackageMaterialScan:
    def __init__(self, markers):
        self.markers = markers
        self.overlap = max(_WINDOWS_PREFIX_OVERLAP, max(map(len, markers), default=0) - 1)
        self.tail = b""
        self.pending_user = None

    def found_in(self, data):
        window = self.tail + data.lower()
        start = len(self.tail)
        self.tail = window[-self.overlap :]
        if any(marker in window for marker in self.markers):
            return True

        # A Windows username may be arbitrarily long. Remember whether it has
        # any bytes, rather than retaining the unmatched username across reads.
        if self.pending_user is not None:
            delimiter = _WINDOWS_USER_END.search(window, start)
            if delimiter is None:
                self.pending_user |= bool(data)
                return False
            if delimiter[0] == b"\\" and (self.pending_user or delimiter.start() > start):
                return True
            self.pending_user = None

        match = _WINDOWS_USER_PATH.search(window, max(0, start - _WINDOWS_PREFIX_OVERLAP))
        if match is None:
            return False
        if match["end"] is not None:
            return True
        self.pending_user = match.start("partial") != match.end("partial")
        return False


def _audit_package_member(archive, name, sources, markers, expected_digest):
    scanner = _PackageMaterialScan(markers)
    private = b"" in markers
    mismatch = ValueError("Packaged bytes differ from the accepted input: " + name)
    digest = hashlib.sha256() if expected_digest is not None else None
    with archive.open(name) as member, ExitStack() as source_files:
        comparisons = []
        for source in sources:
            try:
                comparisons.append(source_files.enter_context(Path(source).open("rb")))
            except OSError as error:
                comparisons.append(error)

        def compare(data, count):
            for index, source in enumerate(comparisons):
                if isinstance(source, Exception):
                    continue
                try:
                    if source.read(count) != data:
                        comparisons[index] = mismatch
                except OSError as error:
                    comparisons[index] = error

        # Drain the ZIP stream even after a rejection so CRC failures still
        # take precedence over policy or retained-input failures.
        while chunk := member.read(_PACKAGE_CHUNK_BYTES):
            if digest is not None:
                digest.update(chunk)
            if not private:
                private = scanner.found_in(chunk)
                if not private:
                    compare(chunk, len(chunk))
        if private:
            raise ValueError("Private path/material in package member: " + name)
        compare(b"", 1)
        for comparison in comparisons:
            if isinstance(comparison, Exception):
                raise comparison
        if digest is not None and digest.hexdigest() != expected_digest:
            raise mismatch


def audit_package(
    package,
    *,
    expected_files=None,
    expected_sha256=None,
    runtime_files=None,
    forbidden_suffixes=(".pdb", ".cs", ".rs", ".py"),
    forbidden_names=("registry-notices.json",),
    forbidden_markers=(
        b"/users/",
        b".codex/",
        b".codex\\",
        b"-----begin private key",
        b"-----begin openssh private key",
    ),
    forbidden_nuspec_elements=("repository", "projectUrl"),
):
    """Check package content and optional exact runtime members against source bytes.

    expected_files maps archive paths to retained files (notices, documentation,
    etc.). expected_sha256 binds member content to already staged inputs.
    runtime_files additionally forbids any unselected runtimes/ members.
    Stream binary/data members while retaining exact comparisons. Parse .nuspec
    XML as before. Return the package's SHA-256; errors never include member data.
    """
    package = Path(package)
    before = file_sha256(package)
    forbidden_tags = {name.casefold() for name in forbidden_nuspec_elements}
    markers = tuple(marker.lower() for marker in forbidden_markers)
    expected_sha256 = expected_sha256 or {}
    selected_sources = {}
    for selected in (expected_files or {}, runtime_files or {}):
        for member, source in selected.items():
            selected_sources.setdefault(member, set()).add(os.fspath(source))
    with zipfile.ZipFile(package) as archive:
        entries = archive.infolist()
        # Validate the ZIP spelling before zipfile or the consumer normalizes it.
        # Windows and default macOS volumes can alias names that differ only by case.
        seen = set()
        for entry in entries:
            original = entry.orig_filename
            name = original.removesuffix("/")
            if (
                "\\" in original
                or "\x00" in original
                or any(
                    part in ("", ".", "..") or ":" in part or part.endswith((".", " "))
                    for part in name.split("/")
                )
            ):
                raise ValueError("Invalid package member path: " + repr(original))
            key = name.casefold()
            if key in seen:
                raise ValueError("Duplicate or aliased package member: " + repr(original))
            seen.add(key)
        files = {entry.filename for entry in entries if not entry.is_dir()}
        if runtime_files is not None:
            actual = {name for name in files if name.casefold().startswith("runtimes/")}
            if actual != set(runtime_files):
                raise ValueError("Package runtime assets differ from the selected set")
        for name in files:
            if (
                name.lower().endswith(tuple(suffix.lower() for suffix in forbidden_suffixes))
                or PurePosixPath(name).name in forbidden_names
            ):
                raise ValueError("Forbidden package member: " + name)
            sources = selected_sources.get(name, ())
            digest = expected_sha256.get(name)
            if not name.lower().endswith(".nuspec"):
                _audit_package_member(archive, name, sources, markers, digest)
                continue

            # Keep the existing XML parse and malformed-metadata behavior.
            data = archive.read(name)
            if _PackageMaterialScan(markers).found_in(data):
                raise ValueError("Private path/material in package member: " + name)
            metadata = ET.fromstring(data)
            if any(
                element.tag.rsplit("}", 1)[-1].casefold() in forbidden_tags
                for element in metadata.iter()
            ):
                raise ValueError("Forbidden package metadata in: " + name)
            if (digest is not None and hashlib.sha256(data).hexdigest() != digest) or any(
                data != Path(source).read_bytes() for source in sources
            ):
                raise ValueError("Packaged bytes differ from the accepted input: " + name)
        for member in selected_sources.keys() | expected_sha256.keys():
            if member not in files:
                raise ValueError("Packaged bytes differ from the accepted input: " + member)
    if file_sha256(package) != before:
        raise ValueError("Package changed during inspection")
    return before


def _publish_package(archive, destination, receipt):
    """Publish one package/receipt pair, restoring prior files after any failed swap."""
    if archive.is_symlink() or not archive.is_file():
        raise ValueError("Package publication requires an ordinary staged package")
    record = destination.with_name(destination.name + ".json")
    staged_record = archive.with_name(archive.name + ".json")
    write_json(staged_record, receipt)
    if file_sha256(archive) != receipt["sha256"]:
        raise ValueError("Package changed after inspection; no package was replaced.")
    selected = ((archive, destination), (staged_record, record))
    previous = {}
    for _, target in selected:
        if target.is_symlink() or target.exists() and not target.is_file():
            raise ValueError("Package publication requires ordinary package/receipt files")
        previous[target] = target.exists()
    recovery = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.rollback-", dir=destination.parent)
    )
    try:
        for _, target in selected:
            if previous[target]:
                os.replace(target, recovery / target.name)
        for staged, target in selected:
            os.replace(staged, target)
        if file_sha256(destination) != receipt["sha256"]:
            raise ValueError("Package changed during publication")
    except BaseException:
        try:
            for staged, target in selected:
                backup = recovery / target.name
                if backup.exists():
                    os.replace(backup, target)
                elif not previous[target] and not staged.exists():
                    target.unlink(missing_ok=True)
        except BaseException as rollback_error:
            rollback_error.add_note(f"Previous package files retained at {recovery}")
            raise
        recovery.rmdir()
        raise
    shutil.rmtree(recovery)


_MAX_NATIVE_RECEIPT_BYTES = 16 * 1024 * 1024


def _native_receipt_bytes(path):
    if path.is_symlink() or not path.is_file():
        raise ValueError("Native verification requires an ordinary receipt file")
    with path.open("rb") as receipt:
        data = receipt.read(_MAX_NATIVE_RECEIPT_BYTES + 1)
    if len(data) > _MAX_NATIVE_RECEIPT_BYTES:
        raise ValueError("Native receipt exceeds the size limit")
    return data


def _verified_package_libraries(native, runtimes):
    root = native.root.resolve()
    libraries, receipt_paths, accepted = {}, {}, {}

    # The macOS and Windows native producers use this same checkout lock.
    with build_lock(native.root / ".native-build"):
        for rid in runtimes:
            macos = rid.startswith("osx-")
            if macos:
                directory = native.artifact / "macos-arm64_x86_64" / (native.module + ".framework")
                inventory_root = root
                receipt_path = native.receipt
                library = directory / native.module
                filename = "lib" + native.library + ".dylib"
            else:
                architecture = rid.removeprefix("win-")
                directory, _ = windows._output(native, architecture, None)
                inventory_root = directory.resolve()
                receipt_path = directory / "native-build.json"
                filename = native.library + ".dll"
                library = directory / filename
            # Both macOS RIDs use the same verified universal framework.
            if receipt_path not in accepted:
                raw = _native_receipt_bytes(receipt_path)
                record = json.loads(raw)
                binary = library.resolve(strict=True)
                if not binary.is_file() or not binary.is_relative_to(directory.resolve()):
                    raise ValueError("Native library must stay inside its verified artifact")
                if macos:
                    native.verify(inspect_native=sys.platform == "darwin")
                else:
                    verified, result = windows.verify_artifact(native, architecture=architecture)
                    if verified != directory or result != record:
                        raise ValueError("Native receipt changed during verification")
                if _native_receipt_bytes(receipt_path) != raw:
                    raise ValueError("Native receipt changed during verification")
                if library.resolve(strict=True) != binary:
                    raise ValueError("Native library changed during verification")
                digest = record["outputs"].get(binary.relative_to(inventory_root).as_posix())
                if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                    raise ValueError("Native receipt does not identify the verified library bytes")
                accepted[receipt_path] = hashlib.sha256(raw).hexdigest(), digest
            libraries[rid] = (library, filename, accepted[receipt_path][1])
            receipt_paths[rid] = receipt_path
    return libraries, receipt_paths, {path: values[0] for path, values in accepted.items()}


def pack_native(
    native,
    *,
    project,
    version,
    authors,
    package_files,
    nuget_config,
    input_files=(),
    argv=None,
    environment=None,
):
    """Pack explicit SDK files and accepted native assets through the developer CLI.

    The SDK selects its project, version, metadata and notice/file policy. This
    driver verifies receipts, builds managed output, stages/audits exact bytes and
    rejects changed inputs before replacing a previously accepted package.
    """
    parser = argparse.ArgumentParser(description=pack_native.__doc__.splitlines()[0])
    parser.add_argument(
        "--runtime",
        action="append",
        required=True,
        choices=("osx-arm64", "osx-x64", "win-arm64", "win-x64"),
    )
    parser.add_argument(
        "--package-source", action="append", default=[], help="Private/local NuGet source"
    )
    args = parser.parse_args(argv)
    runtimes = sorted(set(args.runtime))
    root, project = native.root, Path(project)
    env = dotnet_tools.subprocess_environment(environment)
    dotnet = env.get("DOTNET_BIN") or shutil.which("dotnet", path=env.get("PATH"))
    if not dotnet:
        raise ValueError("Install .NET 10 or provide DOTNET_BIN.")
    properties = ET.parse(project).getroot()
    framework = properties.findtext(".//TargetFramework")
    description = properties.findtext(".//Description")
    if not framework or not re.fullmatch(r"net[0-9]+\.[0-9]+", framework) or not description:
        raise ValueError(
            "The managed SDK project must declare one target framework and its Description."
        )
    dependencies = []
    for reference in properties.findall(".//PackageReference"):
        identifier, requirement = reference.get("Include"), reference.get("Version")
        if not identifier or not requirement or "$" in identifier + requirement:
            raise ValueError(
                "Package references must declare explicit IDs and version requirements."
            )
        dependencies.append((identifier, requirement))
    package_files = {member: Path(source) for member, source in package_files.items()}
    for member in package_files:
        path = PurePosixPath(member)
        if (
            not member
            or path.is_absolute()
            or ".." in path.parts
            or path.as_posix() != member
            or any(char in member for char in "\\:\0")
            or path.parts[0] in ("lib", "runtimes")
        ):
            raise ValueError(
                "Package file must use an unreserved canonical relative path: " + member
            )
    if not {"README.md", "LICENSE"}.issubset(package_files):
        raise ValueError("Select the package README.md and LICENSE explicitly.")

    def managed_sources():
        paths = [*input_files, *package_files.values(), project, nuget_config]
        for directory, subdirectories, names in project.parent.walk():
            # Generated output is excluded from both snapshots; don't traverse it.
            subdirectories[:] = [name for name in subdirectories if name not in {"bin", "obj"}]
            paths.extend(
                path
                for name in names
                if (path := directory / name).suffix in {".cs", ".csproj", ".props", ".targets"}
                and path.is_file()
            )
        return hashes(paths, root)

    output = root / ".native-build/packages"
    tool_sources = [
        Path(module) for module in (__file__, artifacts.__file__, dotnet_tools.__file__)
    ]
    with build_lock(output):
        inputs, managed_inputs = native.build_sources(), managed_sources()
        tool_inputs = hashes(tool_sources, Path(__file__).parent)
        libraries, receipt_paths, receipt_hashes = _verified_package_libraries(native, runtimes)
        dotnet_tools.build_managed(
            dotnet,
            project,
            env=env,
            cwd=root,
            nuget_config=nuget_config,
            sources=args.package_source,
            properties={"Version": version, "UseSharedCompilation": "false"},
            verify_style=True,
        )
        with tempfile.TemporaryDirectory(prefix="pack-", dir=output) as temporary:
            stage = Path(temporary)
            package = ET.Element("package")
            metadata = ET.SubElement(package, "metadata")
            for key, value in {
                "id": project.stem,
                "version": version,
                "authors": authors,
                "description": description,
                "readme": "README.md",
            }.items():
                ET.SubElement(metadata, key).text = value
            ET.SubElement(metadata, "license", {"type": "file"}).text = "LICENSE"
            group = ET.SubElement(
                ET.SubElement(metadata, "dependencies"), "group", {"targetFramework": framework}
            )
            for identifier, requirement in dependencies:
                ET.SubElement(group, "dependency", {"id": identifier, "version": requirement})
            files = ET.SubElement(package, "files")
            selected, accepted_bytes, staged_sources = {}, {}, {}

            def include(source, target, *, native_digest=None):
                source = Path(source)
                # Both macOS RIDs consume the same universal binary. Keep the
                # staged basename equal to the target's for NuGet file mapping.
                key = source, PurePosixPath(target).name
                if key not in staged_sources:
                    staged = stage / "payload" / target
                    staged_sources[key] = staged, stage_file(source, staged)
                staged, digest = staged_sources[key]
                if native_digest is not None and digest != native_digest:
                    raise ValueError("Native library differs from its verified receipt")
                accepted_bytes[target] = digest
                ET.SubElement(files, "file", {"src": str(staged), "target": target})
                selected[target] = source

            assembly = project.parent / "bin/Release" / framework
            for extension in ("dll", "xml"):
                member = project.stem + "." + extension
                include(assembly / member, "lib/" + framework + "/" + member)
            for member, source in package_files.items():
                include(source, member)
            runtime_files = {}
            for rid, (library, filename, digest) in libraries.items():
                member = f"runtimes/{rid}/native/{filename}"
                include(library, member, native_digest=digest)
                runtime_files[member] = library
            nuspec = stage / "package.nuspec"
            ET.ElementTree(package).write(nuspec, encoding="utf-8", xml_declaration=True)
            dotnet_tools.pack_project(
                dotnet,
                project,
                output=stage,
                env=env,
                cwd=root,
                no_build=True,
                properties={"IsPackable": "true", "NuspecFile": str(nuspec)},
            )
            archives = list(stage.glob("*.nupkg"))
            if len(archives) != 1:
                raise ValueError("Packing must produce exactly one native SDK package.")
            archive = archives[0]
            package_sha256 = audit_package(
                archive,
                expected_files=selected,
                expected_sha256=accepted_bytes,
                runtime_files=runtime_files,
            )
            if any(
                file_sha256(source) != digest for (source, _), (_, digest) in staged_sources.items()
            ):
                raise ValueError(
                    "Accepted package inputs changed while packing; no package was replaced."
                )
            if any(
                hashlib.sha256(_native_receipt_bytes(path)).hexdigest() != digest
                for path, digest in receipt_hashes.items()
            ):
                raise ValueError("Native receipt changed while packing; no package was replaced.")
            if (
                inputs != native.build_sources()
                or managed_inputs != managed_sources()
                or tool_inputs != hashes(tool_sources, Path(__file__).parent)
            ):
                raise ValueError(
                    "Native, managed or packaging inputs changed while packing; no package was replaced."
                )
            destination = output / archive.name
            _publish_package(
                archive,
                destination,
                {
                    "package": destination.name,
                    "sha256": package_sha256,
                    "version": version,
                    "runtimes": runtimes,
                    "nativeReceipts": {
                        rid: receipt_hashes[path] for rid, path in receipt_paths.items()
                    },
                    "managedInputs": managed_inputs,
                    "packagingToolInputs": tool_inputs,
                    "files": accepted_bytes,
                },
            )
            print("Verified local package: " + str(destination))
            return destination


_CONSUMER_RESTORE_OVERRIDES = {
    "nuget_fallback_packages",
    "nuget_packages",
    "nugetpackagefolders",
    "nugetpackageroot",
    "restoreadditionalprojectfallbackfolders",
    "restoreadditionalprojectsources",
    "restoreconfigfile",
    "restorefallbackfolders",
    "restorepackagespath",
    "restoresources",
}


def _verify_restored_packages(work, feed, cache, checksums):
    assets = json.loads((work / "obj/project.assets.json").read_text(encoding="utf-8"))
    restore = assets["project"]["restore"]
    if {Path(source).resolve() for source in restore["sources"]} != {feed}:
        raise ValueError("Consumer restore used sources outside its local feed")
    if Path(restore["packagesPath"]).resolve() != cache or {
        Path(folder).resolve() for folder in assets["packageFolders"]
    } != {cache}:
        raise ValueError("Consumer restore used packages outside its fresh cache")
    libraries = assets["libraries"]
    if not libraries:
        raise ValueError("Consumer restore did not resolve any SDK packages")
    installed = set()
    for archive in cache.glob("*/*/*.nupkg"):
        if (
            archive.resolve() != archive
            or not archive.is_file()
            or file_sha256(archive) not in checksums
        ):
            name = archive.relative_to(cache).as_posix()
            raise ValueError("Restored package differs from the supplied local packages: " + name)
        installed.add(archive)
    for library in libraries.values():
        if library["type"] != "package":
            raise ValueError("Consumer restore resolved a source project instead of a package")
        name = library["path"]
        relative = PurePosixPath(name)
        if (
            len(relative.parts) != 2
            or relative.is_absolute()
            or relative.as_posix() != name
            or ".." in relative.parts
            or "\\" in name
            or ":" in name
        ):
            raise ValueError("Consumer restore recorded an invalid cache path")
        package, version = relative.parts
        if cache / package / version / f"{package}.{version}.nupkg" not in installed:
            raise ValueError("Consumer restore did not retain its package archive: " + name)


def run_isolated_consumer(
    dotnet,
    *,
    packages,
    references,
    source,
    work_parent,
    env,
    clear_environment=(),
    exclude_paths=(),
    runtime=None,
    properties=None,
    execute=True,
    runner=None,
):
    """Restore SDK-owned C# from only supplied local packages and a fresh cache.

    Native lookup overrides are caller-selected. Dynamic-library search variables
    are always removed. Package fixtures and runtime/portable policy stay local.
    With execute=False only restore/build run; that is not native acceptance.
    """
    packages = [Path(package).resolve(strict=True) for package in packages]
    if len({package.name.casefold() for package in packages}) != len(packages):
        raise ValueError("Duplicate local-feed package filename")
    if not packages or not references:
        raise ValueError("Provide local packages and explicit package references")
    if _CONSUMER_RESTORE_OVERRIDES.intersection(key.casefold() for key in (properties or {})):
        raise ValueError("Consumer project properties must use the isolated restore settings")
    work_parent = Path(work_parent).resolve()
    work_parent.mkdir(parents=True, exist_ok=True)
    with temporary_workspace(work_parent, prefix="consumer-") as work:
        feed = work / "feed"
        cache = work / "packages"
        feed.mkdir()
        package_hashes = {package: file_sha256(package) for package in packages}
        for package in packages:
            shutil.copyfile(package, feed / package.name)
            if file_sha256(feed / package.name) != package_hashes[package]:
                raise ValueError("Local feed copy differs from its package: " + package.name)
        config = ET.Element("configuration")
        sources = ET.SubElement(config, "packageSources")
        ET.SubElement(sources, "clear")
        ET.SubElement(sources, "add", key="owned-local", value=str(feed))
        ET.SubElement(ET.SubElement(config, "fallbackPackageFolders"), "clear")
        config_path = work / "NuGet.Config"
        ET.ElementTree(config).write(config_path, encoding="utf-8", xml_declaration=True)
        # MSBuild and NuGet search parent directories independently of NuGet.Config.
        # Stop at the fixture so checkout-local build policy cannot affect acceptance.
        for name in (
            "Directory.Build.props",
            "Directory.Build.targets",
            "Directory.Packages.props",
        ):
            (work / name).write_text("<Project />\n", encoding="utf-8")
        (work / "Directory.Build.rsp").touch()
        project = ET.Element("Project", Sdk="Microsoft.NET.Sdk")
        values = {
            "TargetFramework": "net10.0",
            "OutputType": "Exe",
            "UseAppHost": "false",
            "SelfContained": "false",
            "ImplicitUsings": "enable",
            **(properties or {}),
        }
        if runtime is not None:
            values["RuntimeIdentifier"] = runtime
        group = ET.SubElement(project, "PropertyGroup")
        for key, value in values.items():
            ET.SubElement(group, key).text = str(value)
        references_group = ET.SubElement(project, "ItemGroup")
        for name, version in references.items():
            ET.SubElement(references_group, "PackageReference", Include=name, Version=version)
        project_path = work / "Consumer.csproj"
        ET.ElementTree(project).write(project_path, encoding="utf-8", xml_declaration=True)
        (work / "Program.cs").write_text(source, encoding="utf-8")
        removed = {
            key.casefold()
            for key in (
                *clear_environment,
                "LD_LIBRARY_PATH",
                "DYLD_LIBRARY_PATH",
                "DYLD_FALLBACK_LIBRARY_PATH",
            )
        } | _CONSUMER_RESTORE_OVERRIDES
        clean = {key: value for key, value in env.items() if key.casefold() not in removed}
        clean["NUGET_PACKAGES"] = str(cache)
        roots = [Path(path).resolve() for path in exclude_paths]
        for key in list(clean):
            if key.casefold() == "path":
                clean[key] = os.pathsep.join(
                    part
                    for part in clean[key].split(os.pathsep)
                    if not any(Path(part).resolve().is_relative_to(root) for root in roots)
                )
        dotnet_tools.run_command(
            [dotnet, "restore", project_path, "--configfile", config_path],
            cwd=work,
            env=clean,
            runner=runner,
        )
        _verify_restored_packages(work, feed, cache, set(package_hashes.values()))
        if execute:
            result = dotnet_tools.run_managed(
                dotnet, project_path, env=clean, cwd=work, no_restore=True, runner=runner
            )
        else:
            result = dotnet_tools.run_command(
                [dotnet, "build", project_path, "--no-restore", "--configuration", "Release"],
                cwd=work,
                env=clean,
                runner=runner,
            )
        for package, checksum in package_hashes.items():
            if file_sha256(package) != checksum or file_sha256(feed / package.name) != checksum:
                raise ValueError("Package changed during consumer validation: " + package.name)
        return result
