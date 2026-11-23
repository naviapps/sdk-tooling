"""Record packages and feature sets from a native Cargo build."""

import json
import subprocess
from pathlib import Path


def _package(package):
    return package["name"], package["version"], package.get("source") or "workspace"


def _word(value):
    return (
        isinstance(value, str)
        and bool(value)
        and value.isprintable()
        and not any(separator in value for separator in ("/", "\\"))
    )


def _words(words, *, allow_empty=False):
    return (
        isinstance(words, list)
        and (allow_empty or bool(words))
        and all(_word(word) for word in words)
        and words == sorted(set(words))
    )


def _unit_key(unit):
    return (
        *_package(unit),
        unit["targetName"],
        tuple(unit["kind"]),
        unit["compilation"],
        tuple(unit["features"]),
    )


def compile_units(command, *, graph, target, library, env):
    """Run Cargo and select metadata for the compiler outputs of this invocation."""
    result = subprocess.run(command, cwd=graph.root, env=env, stdout=subprocess.PIPE, check=True)
    packages = {package["id"]: package for package in graph.packages}
    target_dir = Path(env["CARGO_TARGET_DIR"]).resolve()
    target_output = target_dir / target
    expected_library = library.resolve()
    units = {}
    linked = False
    finished = False
    for line in result.stdout.splitlines():
        if not line.startswith(b"{"):
            continue
        message = json.loads(line)
        if message.get("reason") == "build-finished":
            if message.get("success") is not True:
                raise ValueError("Cargo didn't report a successful native build")
            finished = True
            break
        if message.get("reason") != "compiler-artifact":
            continue
        package = packages.get(message["package_id"])
        if package is None or message["manifest_path"] != package["manifest_path"]:
            raise ValueError("Cargo built a package outside the verified dependency graph")
        paths = [Path(name) for name in message["filenames"]]
        if not paths or not all(path.is_absolute() for path in paths):
            raise ValueError("Cargo artifact requires absolute paths to its outputs")
        paths = [path.resolve() for path in paths]
        if not all(path.is_relative_to(target_dir) for path in paths):
            raise ValueError("Cargo artifact has an unexpected compilation directory")
        locations = {"target" if path.is_relative_to(target_output) else "host" for path in paths}
        if len(locations) != 1:
            raise ValueError("Cargo artifact has an unexpected compilation directory")
        (location,) = locations
        descriptor = message["target"]
        name, version, source = _package(package)
        unit = {
            "name": name,
            "version": version,
            "source": source,
            "targetName": descriptor["name"],
            "kind": sorted(descriptor["kind"]),
            "compilation": location,
            "features": sorted(message["features"]),
        }
        if expected_library in paths:
            if linked or name not in graph.roots or location != "target":
                raise ValueError("Cargo reported an ambiguous native library")
            if "cdylib" not in descriptor["crate_types"]:
                raise ValueError("Cargo native library isn't a cdylib")
            linked = True
        # Cached artifacts still participate in the build. Drop paths and freshness
        # so the result describes the same compilation on another checkout.
        units[_unit_key(unit)] = unit
    if not finished or not linked:
        raise ValueError("Cargo didn't report the selected native library")
    return [units[key] for key in sorted(units)]


def verify_units(build, dependencies, targets, package, library):
    """Verify portable receipt records against their declared native build scope."""
    if not isinstance(build, dict) or set(build) != set(targets):
        raise ValueError("Cargo build receipt differs from the selected targets")
    if (
        not isinstance(dependencies, dict)
        or not isinstance(dependencies.get("packages"), list)
        or dependencies.get("roots") != [package]
        or dependencies.get("targets") != sorted(targets)
    ):
        raise ValueError("Cargo build receipt requires verified dependency information")
    known = {_package(item): item["features"] for item in dependencies["packages"]}
    fields = {"name", "version", "source", "targetName", "kind", "compilation", "features"}
    for target, units in build.items():
        if not isinstance(units, list) or not units:
            raise ValueError("Cargo build receipt has no compiled units")
        ordered, linked = [], 0
        for unit in units:
            if (
                not isinstance(unit, dict)
                or set(unit) != fields
                or not all(
                    _word(unit[field]) for field in ("name", "version", "targetName", "compilation")
                )
                or not isinstance(unit["source"], str)
                or not _words(unit["kind"])
                or unit["compilation"] not in {"host", "target"}
                or not _words(unit["features"], allow_empty=True)
            ):
                raise ValueError("Cargo build receipt has an invalid compiler artifact")
            resolved = known.get(_package(unit), {}).get(target)
            if resolved is None or not set(unit["features"]).issubset(resolved):
                raise ValueError(
                    "Cargo build receipt contains a package or feature outside its dependency graph"
                )
            ordered.append(_unit_key(unit))
            linked += (
                unit["name"] == package
                and unit["source"] == "workspace"
                and unit["targetName"] == library
                and "cdylib" in unit["kind"]
                and unit["compilation"] == "target"
            )
        if linked != 1 or ordered != sorted(set(ordered)):
            raise ValueError("Cargo build receipt doesn't identify one ordered native build")
