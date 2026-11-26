"""Build and validate Windows x64 SDKs and publish verified package evidence."""

import json
import ntpath
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

from . import artifacts, c_compiler, dotnet, vm_environment, windows
from .artifacts import file_sha256
from .ffi import NOTICE_TARGETS

TARGET = windows.MSVC_TARGETS["x64"]
RID = "win-x64"


def locked_fetch_args(*, notice_targets=NOTICE_TARGETS):
    """Fetch the x64 build and notice graphs before offline inspection."""
    targets = dict.fromkeys((TARGET, *notice_targets))
    flags = (argument for target in targets for argument in ("--target", target))
    return ("cargo", "fetch", "--locked", *flags)


class X64Acceptance:
    """Build and exercise one native SDK on a Windows x64 or ARM64 worker."""

    def __init__(self, root):
        self.root = root
        self.host = windows.host_architecture()
        environment = {
            key.upper(): value
            for key, value in os.environ.items()
            if key.upper() != "DOTNET_HOST_PATH"
        }
        self.env, self.tools, self.dotnet = windows.native_environment(
            self.host, environment, target_architecture="x64"
        )
        self.env["RUSTUP_AUTO_INSTALL"] = "0"
        self.toolchain = {
            "backend": "msvc",
            "host": windows.MSVC_TARGETS[self.host],
            "target": TARGET,
        }
        self.dotnet_version = subprocess.check_output(
            [str(self.dotnet), "--version"], cwd=root, env=self.env, text=True
        ).strip()
        if self.dotnet_version != dotnet.pinned_sdk_version(root):
            raise ValueError("Use the pinned x64 .NET SDK version from global.json")

    def run(self, *args):
        subprocess.run([str(value) for value in args], cwd=self.root, env=self.env, check=True)

    def build(self, native, *, fetch_all=False):
        fetch = ("cargo", "fetch", "--locked") if fetch_all else locked_fetch_args()
        self.run(*fetch)
        built = windows.build_library(native, self.env, self.tools, "x64", native_host=self.host)
        workspace = ("--workspace", "--target", TARGET, "--locked", "--offline")
        self.run("cargo", "clippy", *workspace, "--all-targets", "--", "-D", "warnings")
        self.run("cargo", "test", *workspace)
        return built

    def stage(self, native, built, candidate):
        """Stage the verified DLL and import library under the caller's build lock."""
        verified, receipt = windows.verify_artifact(native, "x64", environment=self.env)
        if verified != built or receipt.get("toolchain") != self.toolchain:
            raise ValueError("Windows x64 library differs from the selected host build")
        library, import_library = stage_verified_library(
            native, built, candidate, receipt, artifacts
        )
        return library, import_library, receipt

    def run_c(self, source, *, include_directory, import_library):
        # Windows searches the executable directory before PATH.
        candidate = import_library.parent
        executable = candidate / "abi.exe"
        c_compiler.compile_c(
            source,
            output=executable,
            compiler=[self.tools / "cl.exe"],
            include_dirs=[include_directory],
            libraries=[import_library],
            style="msvc",
            cwd=candidate,
            env=self.env,
        )
        windows.require_pe_machine(executable, "x64")
        self.run(executable)


def stage_verified_library(native, built, candidate, receipt, tooling=artifacts):
    """Copy a verified x64 DLL and its import library for an independent consumer."""
    dll = native.library + ".dll"
    names = (dll, dll + ".lib")
    staged = tuple(tooling.stage_native(built / name, candidate / name) for name in names)
    expected = {name: receipt["outputs"][name] for name in names}
    if tooling.hashes(staged, candidate) != expected:
        raise ValueError("Staged DLL or import library differs from its verified receipt")
    return staged


def acceptance_source(root, *, match_actions=False, environment=None):
    """Identify the exact clean checkout or captured Windows source snapshot."""
    env = os.environ if environment is None else environment
    captured = vm_environment.source_identity(env)
    if captured is None:
        if subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=root,
            env=env,
            text=True,
        ).strip():
            raise ValueError("Windows x64 acceptance requires a clean committed checkout")
        commit = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, env=env, text=True
        ).strip()
        fingerprint = None
    else:
        commit, fingerprint = captured
    if not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit or ""):
        raise ValueError("Windows x64 acceptance requires the full source commit SHA")
    if match_actions and env.get("GITHUB_SHA", commit) != commit:
        raise ValueError("Windows x64 acceptance source differs from the Actions revision")
    return commit, fingerprint


def _ordinary_path(path):
    return not (path.is_symlink() or path.is_junction())


def export_native(built, inventory, artifact_root):
    """Export an exact accepted x64 build and receipt under the caller's build lock."""
    built = Path(built).resolve(strict=True)
    root = Path(artifact_root).resolve()
    destination = root / "sdk-windows" / RID

    def require_new_output():
        parent = destination.parent.resolve()
        if (
            not _ordinary_path(destination)
            or destination.exists()
            or not parent.is_relative_to(root)
            or parent.is_relative_to(built)
        ):
            raise ValueError(
                "Windows native export requires a new directory inside the artifact root"
            )

    require_new_output()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=destination.parent, prefix="native-export-") as temporary:
        stage = Path(temporary) / RID
        shutil.copytree(built, stage, symlinks=True)
        if artifacts.hashes(artifacts.files(stage), stage) != inventory:
            raise ValueError("Exported Windows native files differ from the accepted build")
        require_new_output()
        stage.rename(destination)


def _owned_file(path, names):
    return path.name in names and _ordinary_path(path) and path.is_file()


def _component(value, pattern):
    return (
        isinstance(value, str)
        and re.fullmatch(pattern, value) is not None
        and not ntpath.isreserved(value)
    )


def verify_packages(directory, packages, *, receipt_name=None):
    """Check the exact set of package names and digests in a staging directory."""
    names = {*packages}
    if receipt_name is not None:
        names.add(receipt_name)
    entries = tuple(directory.iterdir()) if _ordinary_path(directory) and directory.is_dir() else ()
    if (
        not packages
        or {entry.name for entry in entries} != names
        or not all(_owned_file(entry, names) for entry in entries)
        or any(file_sha256(directory / name) != digest for name, digest in packages.items())
    ):
        raise ValueError("Windows package output differs from the accepted package set")


def publish_package_evidence(
    package_directory,
    receipt,
    *,
    receipt_name,
    destination,
    mirror=None,
    lock_root=None,
    legacy_packages_directory=False,
    tooling=artifacts,
):
    """Publish an accepted package set and receipt, with transactional mirroring."""
    packages = receipt["packages"]
    if (
        not isinstance(packages, dict)
        or not packages
        or not _component(receipt_name, r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json")
        or any(
            not _component(name, r"[A-Za-z0-9][A-Za-z0-9_.+-]*\.nupkg")
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            for name, digest in packages.items()
        )
        or len({name.casefold() for name in packages}) != len(packages)
    ):
        raise ValueError("Windows evidence requires package names, SHA-256 digests and a receipt")

    local = Path(destination)
    external = Path(mirror) if mirror is not None else local
    local_path = local.resolve()
    external_path = external.resolve()
    destinations = [local]
    if external_path != local_path:
        if external_path.is_relative_to(local_path) or local_path.is_relative_to(external_path):
            raise ValueError("Local and external x64 evidence directories must be disjoint")
        destinations.append(external)

    names = {*packages, receipt_name}

    def verify(directory):
        try:
            verify_packages(directory, packages, receipt_name=receipt_name)
            if json.loads((directory / receipt_name).read_bytes()) == receipt:
                return
        except ValueError:
            pass
        raise ValueError("Windows x64 evidence differs from the accepted package and receipt")

    def can_replace(entry):
        if _owned_file(entry, names):
            return True
        return (
            legacy_packages_directory
            and entry.name == "packages"
            and _ordinary_path(entry)
            and entry.is_dir()
            and all(_owned_file(file, packages) for file in entry.iterdir())
        )

    def check_destination(destination):
        if (
            not _ordinary_path(destination.parent)
            or not _ordinary_path(destination)
            or (
                destination.exists()
                and (
                    not destination.is_dir()
                    or not all(can_replace(entry) for entry in destination.iterdir())
                )
            )
        ):
            raise ValueError("Windows x64 evidence directory contains unrelated files")

    with ExitStack() as temporary:
        if lock_root is not None:
            temporary.enter_context(tooling.build_lock(lock_root))
        for destination in destinations:
            check_destination(destination)
        stages = []
        for destination in destinations:
            destination.parent.mkdir(parents=True, exist_ok=True)
            path = temporary.enter_context(
                tempfile.TemporaryDirectory(dir=destination.parent, prefix="evidence-")
            )
            stage = Path(path) / destination.name
            stage.mkdir()
            for name in packages:
                package = package_directory / name
                if not _owned_file(package, packages):
                    raise ValueError("Windows x64 evidence requires ordinary package files")
                shutil.copyfile(package, stage / name)
            tooling.write_json(stage / receipt_name, receipt)
            verify(stage)
            stages.append(stage)

        cleanup_failure = None

        def verify_local():
            nonlocal cleanup_failure
            verify(local)
            if len(stages) == 1:
                return
            check_destination(external)
            external_verified = False

            def verify_external():
                nonlocal external_verified
                verify(external)
                external_verified = True

            # Keep the local rollback available until the external pair verifies.
            try:
                tooling.publish(stages[1], external, verify_external)
            except BaseException as error:
                if not external_verified:
                    raise
                # The helper cleans its previous tree after verification. Preserve both
                # verified outputs if that cleanup failed.
                verify(external)
                cleanup_failure = error

        check_destination(local)
        tooling.publish(stages[0], local, verify_local)
        if cleanup_failure is not None:
            raise cleanup_failure


def publish_versioned_x64(
    package_directory,
    receipt,
    *,
    version,
    receipt_name,
    output_dir,
    artifact_root,
    tooling=artifacts,
):
    """Publish a versioned package set under the Windows x64 runtime directory."""
    if not _component(version, r"[A-Za-z0-9][A-Za-z0-9.+-]*") or version.endswith("."):
        raise ValueError("Windows evidence requires a local version component name")
    evidence_path = Path(RID) / version
    publish_package_evidence(
        package_directory,
        receipt,
        receipt_name=receipt_name,
        destination=output_dir / evidence_path,
        mirror=artifact_root / evidence_path if artifact_root else None,
        lock_root=output_dir,
        tooling=tooling,
    )


def publish_x64(package, receipt, *, receipt_name, output_dir, artifact_root, tooling):
    """Publish a solitary accepted package and receipt to the versioned output."""
    if set(receipt["packages"]) != {package.name}:
        raise ValueError("Windows x64 evidence must identify the accepted package")
    publish_versioned_x64(
        package.parent,
        receipt,
        version=receipt["version"],
        receipt_name=receipt_name,
        output_dir=output_dir,
        artifact_root=artifact_root,
        tooling=tooling,
    )
