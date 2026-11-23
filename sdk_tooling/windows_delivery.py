"""Build and validate Windows x64 SDKs and publish verified package evidence."""

import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path

from . import artifacts, c_compiler, dotnet, vm_environment, windows
from .artifacts import file_sha256

TARGET = windows.MSVC_TARGETS["x64"]
RID = "win-x64"


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
        fetch_target = () if fetch_all else ("--target", TARGET)
        self.run("cargo", "fetch", "--locked", *fetch_target)
        self.run("cargo", "fmt", "--all", "--", "--check")
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
        dll = native.library + ".dll"
        names = (dll, dll + ".lib")
        library, import_library = (
            artifacts.stage_native(built / name, candidate / name) for name in names
        )
        expected = {name: receipt["outputs"][name] for name in names}
        if artifacts.hashes([library, import_library], candidate) != expected:
            raise ValueError("Staged DLL or import library differs from its verified receipt")
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


def publish_x64(package, receipt, *, receipt_name, output_dir, artifact_root, tooling):
    """Publish an accepted package and receipt, rolling back both locations on failure."""
    version = receipt["version"]
    if (
        not isinstance(version, str)
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]*", version)
        or version.endswith(".")
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*\.json", receipt_name)
        or receipt_name == package.name
    ):
        raise ValueError("Windows evidence requires local version and receipt component names")
    evidence_path = Path(RID) / version
    local = output_dir / evidence_path
    external = artifact_root / evidence_path if artifact_root else None
    local_path = local.resolve()
    external_path = external.resolve() if external is not None else local_path
    destinations = [local]
    if external_path != local_path:
        if external_path.is_relative_to(local_path) or local_path.is_relative_to(external_path):
            raise ValueError("Local and external x64 evidence directories must be disjoint")
        destinations.append(external)

    names = {package.name, receipt_name}
    digest = receipt["packages"][package.name]

    def verify(directory):
        if (
            {entry.name for entry in directory.iterdir()} != names
            or json.loads((directory / receipt_name).read_bytes()) != receipt
            or file_sha256(directory / package.name) != digest
        ):
            raise ValueError("Windows x64 evidence differs from the accepted package and receipt")

    def check_destination(destination):
        if (
            destination.parent.is_symlink()
            or destination.parent.is_junction()
            or destination.is_symlink()
            or destination.is_junction()
            or (
                destination.exists()
                and (
                    not destination.is_dir()
                    or any(
                        entry.name not in names or not entry.is_file() or entry.is_symlink()
                        for entry in destination.iterdir()
                    )
                )
            )
        ):
            raise ValueError("Windows x64 evidence directory contains unrelated files")

    with tooling.build_lock(output_dir), ExitStack() as temporary:
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
            shutil.copyfile(package, stage / package.name)
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
