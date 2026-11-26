"""Select and report independently installed Rust and C clients for one SDK revision."""

import argparse
import hashlib
import json
import os
import re
import sys
from contextlib import nullcontext
from pathlib import Path

from .. import vm_environment, windows
from ..workspaces import temporary_workspace
from . import c_framework, rust_git
from .workspace import _source_revision


def add_arguments(parser):
    parser.add_argument("--revision", help="Exact tested SDK commit; defaults to clean HEAD")
    parser.add_argument(
        "--git-source",
        type=Path,
        help="Complete local Git repository or self-contained bundle; defaults to this SDK",
    )
    parser.add_argument("--language", choices=("rust", "c"), action="append")
    parser.add_argument(
        "--windows-architecture",
        choices=tuple(windows.MSVC_TARGETS),
        help="Windows Rust target. x64 can run under emulation on ARM64",
    )
    parser.add_argument(
        "--git-dependency-source",
        action="append",
        metavar="[OWNER/REPO=]PATH",
        help="Complete local mirror or bundle for an additional SDK Git repository",
    )
    parser.add_argument(
        "--evidence",
        type=Path,
        help="Retain a JSON report and the client's own Cargo.lock/metadata outside the SDK",
    )


def check_installed(
    *,
    root,
    git_url,
    rust_packages,
    framework=None,
    header=None,
    c_arguments=None,
    c_fixture=c_framework._C_FIXTURE,
    c_receipt=c_framework._C_RECEIPT,
    argv=None,
    args=None,
    rust_git_dependencies=None,
    rust_modes=None,
    rust_mode_check=None,
    rust_registry_dependencies=None,
):
    """Run the SDK's own Rust/C API assertions using only independently delivered inputs."""
    if args is None:
        parser = argparse.ArgumentParser(description=__doc__)
        add_arguments(parser)
        args = parser.parse_args(argv)
    elif argv is not None:
        raise ValueError("Supply either parsed consumer arguments or argv")
    root = Path(root).resolve(strict=True)
    revision, tree = _source_revision(root, args.revision)
    default_languages = ("rust", "c") if framework and not args.windows_architecture else ("rust",)
    selected = tuple(dict.fromkeys(args.language or default_languages))
    if "c" in selected:
        if framework is None or header is None:
            raise ValueError("This SDK check does not declare an installed C framework and header")
        framework = c_framework._relative(framework)
        if framework.parts[0] != "Artifacts" or framework.suffix != ".framework":
            raise ValueError("Select a tracked framework under Artifacts")
        if c_framework._relative(header).name != header:
            raise ValueError(
                "Select a public header filename under the framework Headers directory"
            )
    packages = tuple(rust_packages)
    dependencies = rust_git._extra_dependencies(rust_git_dependencies, git_url, packages)
    registry_dependencies = rust_git._registry_dependencies(
        rust_registry_dependencies, set(packages) | set(dependencies)
    )
    rust_options = {"dependencies": dependencies} if dependencies else {}
    if args.windows_architecture:
        if sys.platform != "win32" or "rust" not in selected:
            raise ValueError("A Windows architecture requires native Windows Rust acceptance")
        rust_options["windows_architecture"] = args.windows_architecture
    if registry_dependencies:
        rust_options["registry_dependencies"] = registry_dependencies
    if rust_modes is not None:
        if (
            not rust_modes
            or not callable(rust_mode_check)
            or any(
                not re.fullmatch(r"[a-z][a-z0-9-]*", label)
                or any(not re.fullmatch(r"[A-Za-z0-9_+.-]+", feature) for feature in features)
                or not active
                or set(active) - set(packages)
                for label, (features, active) in rust_modes.items()
            )
            or set().union(*(set(active) for _, active in rust_modes.values())) != set(packages)
        ):
            raise ValueError(
                "Rust modes must name valid features and cover the public SDK packages"
            )
        rust_options["modes"] = rust_modes
    if args.git_dependency_source:
        if not dependencies or "rust" not in selected:
            raise ValueError("Select the installed Rust consumer and declare its SDK Git sources")
        if vm_environment.source_manifest() is not None:
            raise ValueError("Choose either the CI source manifest or --git-dependency-source")
        rust_options["dependency_sources"] = rust_git._local_dependency_sources(
            args.git_dependency_source, rust_git._dependency_pins(dependencies)
        )
    if "c" in selected and sys.platform != "darwin":
        raise ValueError("The installed C framework consumer requires owned macOS acceptance")
    destination = args.evidence
    lock_path = None
    metadata_paths = {}
    if destination is not None:
        # Resolve the parent, never the final name of any output file.
        destination = destination.parent.resolve() / destination.name
        if destination.is_relative_to(root):
            raise ValueError("Retained consumer evidence must remain outside the SDK checkout")
        paths = [destination]
        if "rust" in selected:
            lock_path = destination.with_suffix(".Cargo.lock")
            metadata_paths = {
                mode: destination.with_suffix(
                    f".{mode}.cargo-metadata.json" if mode else ".cargo-metadata.json"
                )
                for mode in (rust_modes or (None,))
            }
            paths.extend((lock_path, *metadata_paths.values()))
        for path in paths:
            if os.path.lexists(path):
                raise ValueError(f"Consumer evidence output already exists: {path.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
    evidence = {
        "sdkRevision": revision,
        "sdkTree": tree,
        "remoteAuthenticationTested": False,
        "nativeRebuilt": False,
        "state": "running",
    }

    def save():
        if report is not None:
            report.seek(0)
            report.write(json.dumps(evidence, indent=2) + "\n")
            report.truncate()
            report.flush()

    def resolved(mirror_tree, git_identity, lock, metadata, *, mode=None, windows_host=None):
        first_resolution = "rust" not in evidence
        rust = evidence.setdefault(
            "rust",
            {
                "delivery": "complete-local-Git-mirror",
                "mirrorTree": mirror_tree,
                "gitIdentity": git_identity,
                "cargoLockSHA256": hashlib.sha256(lock).hexdigest(),
            },
        )
        if mode:
            rust.setdefault("modes", {})[mode] = {
                "state": "resolved",
                "resolvedPackageCount": len(metadata["packages"]),
            }
        else:
            rust["resolvedPackageCount"] = len(metadata["packages"])
        if args.windows_architecture:
            rust["host"] = windows.MSVC_TARGETS[windows_host]
            rust["target"] = windows.MSVC_TARGETS[args.windows_architecture]
        if dependencies:
            rust["gitDependencies"] = {
                name: rust_git._git_identity(pin["git"], pin["rev"])
                for name, pin in dependencies.items()
            }
        if registry_dependencies:
            rust["registryDependencies"] = {
                name: {
                    "version": pin["version"][1:],
                    "features": pin["features"],
                    "source": rust_git.PUBLIC_REGISTRY,
                }
                for name, pin in registry_dependencies.items()
            }
        if lock_path is not None:
            if first_resolution:
                with lock_path.open("xb") as retained_lock:
                    retained_lock.write(lock)
            with metadata_paths[mode].open("x", encoding="utf-8") as retained_metadata:
                retained_metadata.write(json.dumps(metadata, indent=2) + "\n")
        if mode:
            mode_evidence = rust["modes"][mode]
            mode_evidence["checks"] = rust_mode_check(metadata, mode)
            mode_evidence["state"] = "checked"
        save()

    def mode_completed(mode, output):
        evidence["rust"]["modes"][mode].update(state="passed", runtimeOutput=output)
        save()

    if rust_modes:
        rust_options["mode_completed"] = mode_completed

    with destination.open("x", encoding="utf-8") if destination else nullcontext() as report:
        try:
            # Cargo checks out full SDK trees, including macOS frameworks on Windows.
            with temporary_workspace(None, prefix="sdk-") as directory:
                work = directory.resolve()
                if work.is_relative_to(root):
                    raise ValueError("External clients must be outside the SDK workspace")
                for language in selected:
                    task = work / language
                    task.mkdir()
                    if language == "rust":
                        output = rust_git._run_rust(
                            root,
                            task,
                            git_url,
                            packages,
                            revision,
                            tree,
                            args.git_source or root,
                            resolved,
                            **rust_options,
                        )
                        if rust_modes:
                            output = "".join(output.values())
                        rust = evidence["rust"]
                        rust["runtimeOutput"] = output
                        if args.windows_architecture:
                            rust["execution"] = (
                                "native" if rust["host"] == rust["target"] else "emulated"
                            )
                    else:
                        c_framework._c_file(
                            root, c_receipt, ".json", directory="Artifacts", tracked=True
                        )
                        c_framework._c_file(root, c_fixture, ".c", tracked=True)
                        evidence["c"] = c_framework._run_c(
                            root,
                            task,
                            framework,
                            header,
                            c_arguments,
                            c_fixture=c_fixture,
                            c_receipt=c_receipt,
                        )
            _source_revision(root, revision)
            evidence["state"] = "passed"
        except Exception as error:
            evidence.update(state="failed", failure=str(error))
            raise
        finally:
            save()
    for language in selected:
        print(evidence[language]["runtimeOutput"], end="")
    print("Exact SDK Git revision/tree: " + revision + " / " + tree)
