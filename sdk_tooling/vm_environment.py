"""Read VM Runner's environment contract, including already-pinned guest runners."""

import os
import re
from collections.abc import Mapping, MutableMapping
from enum import StrEnum
from pathlib import Path


class Variable(StrEnum):
    """Variables owned by the VM Runner delivery boundary."""

    ARTIFACTS_DIR = "VM_RUNNER_ARTIFACTS_DIR"
    SOURCE_MANIFEST = "VM_RUNNER_SOURCE_MANIFEST"
    SOURCE_FILES = "VM_RUNNER_SOURCE_FILES"
    SOURCE_FILES_SHA256 = "VM_RUNNER_SOURCE_FILES_SHA256"
    SOURCE_COMMIT = "VM_RUNNER_SOURCE_COMMIT"
    SOURCE_FINGERPRINT = "VM_RUNNER_SOURCE_FINGERPRINT"

    @property
    def legacy_name(self) -> str:
        suffix = self.removeprefix("VM_RUNNER_")
        return "NAVI_CI_" + {"ARTIFACTS_DIR": "ARTIFACTS", "SOURCE_MANIFEST": "SOURCES"}.get(
            suffix, suffix
        )


def value(variable: Variable, environment: Mapping[str, str] | None = None) -> str | None:
    """Use the canonical value, with an exact legacy fallback and no conflicting aliases."""
    env = os.environ if environment is None else environment
    current = env.get(variable)
    legacy = env.get(variable.legacy_name)
    if current is not None and legacy is not None and current != legacy:
        raise ValueError(f"Conflicting {variable} and {variable.legacy_name} values")
    return current if current is not None else legacy


def set_value(
    environment: MutableMapping[str, str],
    variable: Variable,
    selected: str | Path | None,
) -> None:
    """Replace both aliases in a child environment with the canonical name."""
    environment.pop(variable, None)
    environment.pop(variable.legacy_name, None)
    if selected is not None:
        environment[variable] = str(selected)


def _optional_path(variable: Variable, environment: Mapping[str, str] | None) -> Path | None:
    selected = value(variable, environment)
    if not selected:
        return None
    path = Path(selected)
    if not path.is_absolute():
        raise ValueError(f"{variable} must be an absolute path")
    return path


def artifacts_dir(environment: Mapping[str, str] | None = None) -> Path | None:
    """Destination selected by the controller for retained acceptance evidence."""
    return _optional_path(Variable.ARTIFACTS_DIR, environment)


def source_manifest(environment: Mapping[str, str] | None = None) -> Path | None:
    """Path to the controller's manifest of captured dependency bundles."""
    return _optional_path(Variable.SOURCE_MANIFEST, environment)


def source_files(environment: Mapping[str, str] | None = None) -> tuple[Path, str] | None:
    """Read a captured source inventory and its digest as a single assertion."""
    name = value(Variable.SOURCE_FILES, environment)
    digest = value(Variable.SOURCE_FILES_SHA256, environment)
    if name is None and digest is None:
        return None
    if not name or not re.fullmatch(r"[0-9a-f]{64}", digest or ""):
        raise ValueError("Source selection requires a manifest path and SHA-256")
    path = Path(name)
    if (
        not path.is_absolute()
        or str(path) != name
        or name.startswith("//")
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in name)
    ):
        raise ValueError("Source selection requires the ordinary workspace manifest")
    return path, digest


def source_identity(
    environment: Mapping[str, str] | None = None, *, require_fingerprint: bool = True
) -> tuple[str, str | None] | None:
    """Read a captured source identity. An explicit Git archive may omit the fingerprint."""
    commit = value(Variable.SOURCE_COMMIT, environment)
    fingerprint = value(Variable.SOURCE_FINGERPRINT, environment)
    if commit is None and fingerprint is None:
        return None
    if (require_fingerprint or fingerprint is not None) and not re.fullmatch(
        r"[0-9a-f]{64}", fingerprint or ""
    ):
        raise ValueError("Captured source requires a valid workspace SHA-256")
    if not re.fullmatch(r"[0-9a-f]{40}(?:[0-9a-f]{24})?", commit or ""):
        raise ValueError("Captured source requires the full source commit SHA")
    return commit, fingerprint
