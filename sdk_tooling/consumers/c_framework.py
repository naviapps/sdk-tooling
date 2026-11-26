"""Build and run a C client against a copied framework and its retained receipt."""

import hashlib
import json
import os
import plistlib
import re
import shutil
from pathlib import Path

from ..artifacts import files, hashes
from ..c_headers import prepare_generated
from ..manifest import MANIFEST, manifest_path, read_section
from .workspace import _environment, _git, _run

_C_FIXTURE = "fixtures/consumers/c/main.c"
_C_RECEIPT = "Artifacts/native-build.json"
_C_DEPENDENCY_TARGET = "sdk-consumer"
_NATIVE_SECTION_INPUT = f"{MANIFEST}#native"


def _relative(name):
    path = Path(name)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Installed C paths must stay inside their declared roots")
    return path


def _c_file(root, name, suffix, *, directory=".", tracked=False):
    relative = _relative(name)
    source = root / relative
    scope = root / directory
    if source.suffix != suffix or not source.is_relative_to(scope):
        raise ValueError("Select a C fixture in the SDK or a JSON receipt under Artifacts")
    resolved = source.resolve(strict=True)
    if not resolved.is_relative_to(scope) or not resolved.is_file():
        raise ValueError("The C fixture or receipt must resolve to a file inside its declared root")
    if tracked:
        for path in {relative, resolved.relative_to(root)}:
            if _git(root, "--literal-pathspecs", "ls-files", "-z", "--", path.as_posix()) != (
                path.as_posix() + "\0"
            ):
                raise ValueError("The C fixture and receipt must be tracked in the SDK")
    return resolved


def _matches_receipt_inputs(root, expected):
    sources = {name: root / _relative(name) for name in expected if name != _NATIVE_SECTION_INPUT}
    missing = {
        name
        for name, path in sources.items()
        if (path.suffix == ".h" or path.name == "module.modulemap")
        and not (path.exists() or path.is_symlink())
    }
    observed = hashes([path for name, path in sources.items() if name not in missing], root)
    if _NATIVE_SECTION_INPUT in expected:
        if manifest_path(root, "native").name != MANIFEST:
            raise ValueError(f"The native receipt requires [native] in {MANIFEST}")
        # Match the canonical section recorded by FfiProduct._source_hashes.
        section = read_section(root, "native")
        canonical = json.dumps(section, sort_keys=True, separators=(",", ":"), allow_nan=False)
        observed[_NATIVE_SECTION_INPUT] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if observed != {name: digest for name, digest in expected.items() if name not in missing}:
        return False
    if missing:
        # Verify present inputs before the generator can replace any of them.
        prepare_generated(root, verify=True)
        observed.update(hashes(sources.values(), root))
    return observed == expected


def _stage_c(root, work, framework, *, c_receipt=_C_RECEIPT):
    path = _c_file(root, c_receipt, ".json", directory="Artifacts")
    receipt = json.loads(path.read_text(encoding="utf-8"))
    configuration = receipt.get("configuration")
    minimum_macos = configuration.get("minimumMacOS") if isinstance(configuration, dict) else None
    if not isinstance(minimum_macos, str) or not re.fullmatch(r"[0-9]+\.[0-9]+", minimum_macos):
        raise ValueError("The native receipt must declare a minimum macOS version")
    inputs, outputs = receipt["inputs"], receipt["outputs"]
    if (
        not inputs
        or not outputs
        or hashes([root / _relative(name) for name in outputs], root) != outputs
        or not _matches_receipt_inputs(root, inputs)
    ):
        raise ValueError("The accepted native input/output bytes differ from their receipt")
    install = work / "install"
    install.mkdir()
    shutil.copytree(root / framework, install / framework.name, symlinks=True)
    shutil.copytree(root / "ThirdParty", install / "ThirdParty", symlinks=True)
    if (root / "LICENSE").is_file():
        shutil.copy2(root / "LICENSE", install / "LICENSE", follow_symlinks=False)
    for path in install.rglob("*"):
        if path.is_symlink() and (
            Path(os.readlink(path)).is_absolute()
            or not path.resolve(strict=True).is_relative_to(install)
        ):
            raise ValueError("An installed C payload symlink escapes its copied tree")
    installed = hashes(files(install), install)
    accepted_framework = {
        (Path(framework.name) / path.relative_to(framework)).as_posix(): digest
        for name, digest in receipt["outputs"].items()
        if (path := Path(name)).is_relative_to(framework)
    }
    if (
        not accepted_framework
        or {
            name: digest
            for name, digest in installed.items()
            if Path(name).is_relative_to(framework.name)
        }
        != accepted_framework
    ):
        raise ValueError("The copied framework differs from its accepted output inventory")
    info = plistlib.loads((install / framework.name / "Resources/Info.plist").read_bytes())
    if info.get("MinimumOSVersion") != minimum_macos:
        raise ValueError(
            "The copied framework and its receipt declare different minimum macOS versions"
        )
    return install, installed, minimum_macos


def _clang_dependencies(text, client):
    # Clang writes a single Make rule. Quotes are literal in dependency names,
    # while whitespace, hashes and dollar signs have Make-specific spellings.
    rule = text.replace("\\\n", " ").rstrip("\n")
    target, separator, dependencies = rule.partition(":")
    if target != _C_DEPENDENCY_TARGET or not separator or "\n" in dependencies:
        raise ValueError("Expected one Clang dependency rule for the C consumer")
    words = re.findall(r"(?:\\[ \t#\\]|[^ \t])+", dependencies)
    return [
        (client / re.sub(r"\\([ \t#\\])", r"\1", word).replace("$$", "$")).resolve()
        for word in words
    ]


def _run_c(
    root,
    work,
    framework,
    header,
    c_arguments,
    runner=None,
    *,
    c_fixture=_C_FIXTURE,
    c_receipt=_C_RECEIPT,
):
    run = runner or _run
    env = _environment(work)
    install, installed, minimum_macos = _stage_c(root, work, framework, c_receipt=c_receipt)
    fixture = _c_file(root, c_fixture, ".c")
    client = work / "client"
    client.mkdir()
    shutil.copy2(fixture, client / "main.c")
    owned = work / "files"
    owned.mkdir()
    binary = client / "consumer"
    dependencies = client / "headers.d"
    command = [
        "/usr/bin/xcrun",
        "clang",
        "-std=c17",
        "-Wall",
        "-Wextra",
        "-Werror",
        "-MD",
        "-MT",
        _C_DEPENDENCY_TARGET,
        "-MF",
        str(dependencies),
        "-mmacosx-version-min=" + minimum_macos,
        "main.c",
        "-F",
        str(install),
        "-framework",
        framework.stem,
        "-Wl,-rpath," + str(install),
        "-o",
        str(binary),
    ]
    run(command, cwd=client, env=env)
    dependency_text = dependencies.read_text(encoding="utf-8")
    paths = _clang_dependencies(dependency_text, client)
    resolved = (install / framework.name / "Headers" / header).resolve(strict=True)
    if resolved not in paths or any(path.is_relative_to(root) for path in paths):
        raise ValueError(
            "The C compiler must consume only the installed SDK header, not SDK sources"
        )
    arguments = c_arguments(client, owned) if c_arguments is not None else (owned,)
    runtime = run([str(binary), *map(str, arguments)], cwd=client, env=env)
    if hashes(files(install), install) != installed:
        raise ValueError("The installed C payload changed during compilation or execution")
    return {
        "compileCommand": command,
        "headerDependencies": dependency_text,
        "resolvedFrameworkHeader": str(resolved),
        "installedFiles": installed,
        "runtimeOutput": runtime.stdout,
    }
