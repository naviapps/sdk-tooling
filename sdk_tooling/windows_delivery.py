"""Identify Windows acceptance inputs and publish one verified package transactionally."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
from contextlib import ExitStack
from pathlib import Path


def _sha256(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def acceptance_source(root, *, match_actions=False):
    """Identify the exact clean checkout or captured Windows source snapshot."""
    commit = os.environ.get("NAVI_CI_SOURCE_COMMIT")
    fingerprint = os.environ.get("NAVI_CI_SOURCE_FINGERPRINT")
    if commit is not None or fingerprint is not None:
        if not re.fullmatch(r"[a-f0-9]{64}", fingerprint or ""):
            raise ValueError("Captured Windows x64 acceptance requires the workspace SHA-256")
    else:
        if subprocess.check_output(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=root, text=True
        ).strip():
            raise ValueError("Windows x64 acceptance requires a clean committed checkout")
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, text=True).strip()
    if not re.fullmatch(r"[a-f0-9]{40}(?:[a-f0-9]{24})?", commit or ""):
        raise ValueError("Windows x64 acceptance requires the full source commit SHA")
    if match_actions and os.environ.get("GITHUB_SHA", commit) != commit:
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
    evidence_path = Path("win-x64") / version
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
            or _sha256(directory / package.name) != digest
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
