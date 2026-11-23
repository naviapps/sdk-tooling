"""Build and run a Rust client from a completely delivered Git revision."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import tomllib
from pathlib import Path

from .. import vm_environment, windows
from .workspace import _environment, _git, _run

_REPOSITORY_URL = re.compile(r"https://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)\.git")
_REVISION = re.compile(r"[a-f0-9]{40}")
PUBLIC_REGISTRY = "registry+https://github.com/rust-lang/crates.io-index"


def _prepare_git_delivery(source, work, env, git_url, revision, tree, *, name="sdk.git"):
    source = Path(source).resolve(strict=True)
    if not (source.is_dir() or source.is_file()):
        raise ValueError("Git delivery requires a local repository or bundle")
    if source.is_dir() and _git(source, "rev-parse", "--is-shallow-repository", env=env) == "true":
        raise ValueError("A shallow Git source needs a complete local mirror or bundle")
    mirror = work / name
    _git(work, "clone", "--bare", "--no-local", "--", str(source), str(mirror), env=env)
    if _git(mirror, "rev-parse", "--is-shallow-repository", env=env) != "false":
        raise ValueError("The local Git delivery must contain complete history")
    _git(mirror, "fsck", "--full", "--no-reflogs", env=env)
    delivered = _git(mirror, "rev-parse", revision + "^{commit}", revision + "^{tree}", env=env)
    actual_revision, actual_tree = delivered.splitlines()
    if actual_revision != revision or (tree is not None and actual_tree != tree):
        raise ValueError("The local Git delivery differs from the clean producer commit/tree")
    with Path(env["GIT_CONFIG_GLOBAL"]).open("a", encoding="utf-8") as configuration:
        configuration.write(f'[url "{mirror.as_uri()}"]\n  insteadOf = {git_url}\n')
    return mirror


def _repository(url):
    match = _REPOSITORY_URL.fullmatch(url) if isinstance(url, str) else None
    if not match or any(part in {".", ".."} for part in match[1].split("/")):
        raise ValueError("The Rust Git dependency needs a canonical GitHub repository URL")
    return match[1]


def _extra_dependencies(spec, git_url, packages):
    if spec is None:
        return {}
    if not isinstance(spec, dict):
        raise ValueError("Specify additional SDK Git dependencies by package name")
    repositories = {}
    for name, pin in spec.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or name in packages
            or not isinstance(pin, dict)
            or set(pin) != {"git", "rev"}
            or not isinstance(pin["rev"], str)
            or not _REVISION.fullmatch(pin["rev"])
        ):
            raise ValueError(
                "The additional SDK Git dependency needs a unique package and full revision"
            )
        repository = _repository(pin["git"]).casefold()
        if repository == _repository(git_url).casefold():
            raise ValueError("The additional SDK Git dependency duplicates the producer repository")
        if repositories.setdefault(repository, pin) != pin:
            raise ValueError("Each additional SDK Git repository needs one exact URL and revision")
    return spec


def _dependency_pins(dependencies):
    return {_repository(pin["git"]): pin for pin in dependencies.values()}


def _local_dependency_sources(arguments, repositories):
    if len(repositories) == len(arguments) == 1:
        repository = next(iter(repositories))
        argument = arguments[0]
        if not argument.startswith(repository + "="):
            return {repository: Path(argument)}
    sources = {}
    for argument in arguments:
        repository, separator, path = argument.partition("=")
        if not separator or not path or repository not in repositories or repository in sources:
            raise ValueError("Supply each SDK Git source once as OWNER/REPO=PATH")
        sources[repository] = Path(path)
    if sources.keys() != repositories.keys():
        raise ValueError("Supply each SDK Git source once as OWNER/REPO=PATH")
    return sources


def _registry_dependencies(spec, git_packages):
    if spec is None:
        return {}
    if not isinstance(spec, dict) or len(spec) > 1:
        raise ValueError("Specify at most one pinned public registry dependency")
    for name, pin in spec.items():
        if (
            not isinstance(name, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", name)
            or name in git_packages
            or not isinstance(pin, dict)
            or set(pin) != {"version", "features"}
            or not isinstance(pin["version"], str)
            or not re.fullmatch(r"=[0-9]+\.[0-9]+\.[0-9]+", pin["version"])
            or not isinstance(pin["features"], list)
            or any(
                not isinstance(feature, str) or not re.fullmatch(r"[A-Za-z0-9_+.-]+", feature)
                for feature in pin["features"]
            )
            or len(set(pin["features"])) != len(pin["features"])
        ):
            raise ValueError(
                "The public registry dependency needs an exact version and explicit features"
            )
    return spec


def _unique_members(pairs):
    row = dict(pairs)
    if len(row) != len(pairs):
        raise ValueError("Duplicate key in CI source manifest")
    return row


def _ci_dependency_bundles(pins, verifier):
    manifest = vm_environment.source_manifest()
    if manifest is None:
        raise ValueError(
            "Supply the additional SDK Git bundles through CI or --git-dependency-source"
        )
    if manifest.is_symlink() or not manifest.is_file():
        raise ValueError("The CI source manifest must be an ordinary absolute file")
    rows = json.loads(manifest.read_text(encoding="utf-8"), object_pairs_hook=_unique_members)
    if not isinstance(rows, list):
        raise ValueError("The CI source manifest must be a list of source bundles")
    return {
        repository: _ci_dependency_bundle(manifest, rows, repository, pin, verifier)
        for repository, pin in pins.items()
    }


def _ci_dependency_bundle(manifest, rows, repository, pin, verifier):
    def field(value):
        # The expected identities and generated members are ASCII and nonempty.
        # Invalid strings cannot match them; keep their rows in the manifest order.
        if not isinstance(value, str) or "\0" in value:
            return ""
        return value.encode("utf-8", "backslashreplace").decode("utf-8")

    url = pin["git"]
    fields = [repository, repository.casefold(), url, url.casefold(), pin["rev"]]
    for row in rows:
        row = row if isinstance(row, dict) else {}
        repo, location, revision, bundle = map(
            field, (row.get(key) for key in ("repository", "url", "revision", "bundle"))
        )
        fields.extend((repo, repo.casefold(), location, location.casefold(), revision, bundle))
    response = verifier("select-ci-bundle", fields, selects=True)
    if not re.fullmatch(r"[0-9]+", response) or (index := int(response)) >= len(rows):
        raise ValueError("Unexpected additional SDK Git bundle selection")
    member = f"{index}.bundle"
    bundle = manifest.parent / member
    if bundle.is_symlink() or not bundle.is_file():
        raise ValueError("The additional SDK Git bundle must be an ordinary manifest member")
    checksum = rows[index].get("sha256")
    if not isinstance(checksum, str) or not re.fullmatch(r"[a-f0-9]{64}", checksum):
        raise ValueError("Invalid additional SDK Git bundle checksum")
    return bundle, checksum


def _rust_manifest(
    template,
    git_url,
    packages,
    revision,
    *,
    dependencies=None,
    optional=(),
    registry_dependencies=None,
):
    if template.count("@SDK_REVISION@") != len(packages):
        raise ValueError("The Rust fixture must parameterize each SDK dependency")
    text = template.replace("@SDK_REVISION@", revision)
    manifest = tomllib.loads(text)
    if "workspace" not in manifest or manifest.get("dependencies") != {
        **{
            name: {
                "git": git_url,
                "rev": revision,
                **({"optional": True} if name in optional else {}),
            }
            for name in packages
        },
        **(dependencies or {}),
        **(registry_dependencies or {}),
    }:
        raise ValueError(
            "The Rust fixture needs its own workspace and the exact public Git packages and registry pins"
        )
    return text


def _git_identity(url, revision):
    return f"git+{url}?rev={revision}#{revision}"


def _prepare_identity_verifier(work, rustc, env, target=None, *, compiler_verified=False):
    """Compile the immutable tooling source before creating the client's Cargo home."""
    if not compiler_verified:
        version = _run([rustc, "-vV"], cwd=work, env=env).stdout.splitlines()
        if f"release: {env['RUSTUP_TOOLCHAIN']}" not in version:
            raise ValueError("The prepared Rust compiler differs from the SDK pin")
    source = Path(__file__).resolve().parents[2] / "crates/cargo-consumer-identity/src/main.rs"
    if not source.is_file() or source.is_symlink():
        raise ValueError("Missing ordinary installed-consumer verifier source")
    material = source.read_bytes()
    snapshot = work / "cargo-consumer-identity.rs"
    with snapshot.open("xb") as stream:
        stream.write(material)
    executable = work / (
        "cargo-consumer-identity.exe" if os.name == "nt" else "cargo-consumer-identity"
    )
    _run(
        [
            rustc,
            "--edition=2024",
            "--crate-name",
            "sdk_cargo_consumer_identity",
            *(["--target", target, "-Ctarget-feature=+crt-static"] if target else []),
            "-o",
            str(executable),
            str(snapshot),
        ],
        cwd=work,
        env=env,
    )
    if source.read_bytes() != material or snapshot.read_bytes() != material:
        raise ValueError("The installed-consumer verifier source changed during compilation")
    if target:
        architecture = next(key for key, value in windows.MSVC_TARGETS.items() if value == target)
        windows.require_pe_machine(executable, architecture)

    def request(operation, fields, *, selects=False):
        if any("\0" in field for field in fields):
            raise ValueError("Invalid Cargo package evidence record")
        try:
            result = subprocess.run(
                [str(executable), operation],
                cwd=work,
                env=env,
                input="\0".join(fields).encode("utf-8"),
                capture_output=True,
                check=False,
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise ValueError("Cannot run the installed-consumer identity verifier") from None
        # The verifier reserves exit code 2 for its validated policy diagnostics.
        if result.returncode == 2 and result.stderr and not result.stdout:
            raise ValueError(
                result.stderr.decode("utf-8", errors="replace").strip()
                or "Cargo package evidence verification failed"
            )
        if (
            result.returncode
            or result.stderr
            or bool(result.stdout) != selects
            or b"\0" in result.stdout
        ):
            raise ValueError("Unexpected response from the installed-consumer identity verifier")
        try:
            return result.stdout.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError(
                "Unexpected response from the installed-consumer identity verifier"
            ) from None

    return request


def _verify_packages(
    verifier, entries, packages, expected, label, *, dependencies=None, optional=(), registry=None
):
    records = [
        (
            "git",
            name,
            source,
            "optional" if name in optional and name not in (dependencies or {}) else "",
        )
        for name, source in (dict.fromkeys(packages, expected) | (dependencies or {})).items()
    ]
    records += [
        ("registry", name, PUBLIC_REGISTRY, pin["version"][1:])
        for name, pin in (registry or {}).items()
    ]
    records += [
        ("package", entry["name"], entry.get("source") or "", entry.get("version") or "")
        for entry in entries
    ]
    verifier(label, [field for row in records for field in row])


def _check_rust_metadata(
    metadata,
    client,
    cargo_home,
    verifier,
    packages,
    expected,
    *,
    dependencies=None,
    optional=(),
    registry_dependencies=None,
):
    _verify_packages(
        verifier,
        metadata["packages"],
        packages,
        expected,
        "Cargo metadata",
        dependencies=dependencies,
        optional=optional,
        registry=registry_dependencies,
    )
    for package in metadata["packages"]:
        path = Path(package["manifest_path"]).resolve(strict=True)
        if not (path.is_relative_to(client) or path.is_relative_to(cargo_home)):
            raise ValueError("A Rust dependency escaped the fresh client/cache tree")


def _run_windows_rust_client(
    run, command, *, cwd, env, metadata, architecture, runtime_dir, verifier
):
    root = metadata["resolve"]["root"]
    package = next(package for package in metadata["packages"] if package.get("id") == root)
    binaries = [target["name"] for target in package["targets"] if "bin" in target["kind"]]
    binary = verifier("select-binary", [package.get("default_run") or "", *binaries], selects=True)

    result = run(
        [*command, "--bin", binary, "--message-format=json-render-diagnostics"], cwd=cwd, env=env
    )
    fields = [root, binary]
    for line in result.stdout.splitlines():
        if (
            line.startswith("{")
            and (message := json.loads(line)).get("reason") == "compiler-artifact"
        ):
            target = message["target"]
            fields.extend(
                (
                    message["package_id"],
                    target["name"],
                    "bin" if "bin" in target["kind"] else "",
                    message.get("executable") or "",
                )
            )
    executable = Path(verifier("select-executable", fields, selects=True))
    output = (Path(env["CARGO_TARGET_DIR"]) / windows.MSVC_TARGETS[architecture]).resolve()
    if (
        not executable.is_absolute()
        or executable.suffix.lower() != ".exe"
        or executable.is_symlink()
        or not executable.is_file()
        or not executable.resolve().is_relative_to(output)
    ):
        raise ValueError("The Windows Rust fixture must be inside the fresh target directory")
    windows.require_pe_machine(executable, architecture)
    return run([str(executable), str(runtime_dir)], cwd=cwd, env=env).stdout


def _run_rust(
    root,
    work,
    git_url,
    packages,
    revision,
    tree,
    delivery,
    resolved,
    runner=None,
    *,
    dependencies=None,
    dependency_sources=None,
    modes=None,
    mode_completed=None,
    registry_dependencies=None,
    windows_architecture=None,
):
    run = runner or _run
    env = _environment(work, rust=True, windows_architecture=windows_architecture)
    windows_host = None
    rustc = "rustc"
    if windows_architecture:
        windows_host = windows.host_architecture()
        if windows_host != windows_architecture and (windows_host, windows_architecture) != (
            "arm64",
            "x64",
        ):
            raise ValueError("Select the native Windows Rust target or x64 on ARM64")
        env, _ = windows.native_msvc_environment(
            windows_host, environment=env, target_architecture=windows_architecture
        )
        rustc = shutil.which(rustc, path=env["PATH"])
        if not rustc:
            raise ValueError("Select the SDK's Rust compiler before running the Rust consumer")
        rustc = str(Path(rustc).absolute())
        env["RUSTC"] = rustc
    env["RUSTUP_TOOLCHAIN"] = tomllib.loads(
        (root / "rust-toolchain.toml").read_text(encoding="utf-8")
    )["toolchain"]["channel"]
    cargo = shutil.which("cargo", path=env["PATH"])
    if not cargo:
        raise ValueError("Select the SDK's Rust toolchain before running the Rust consumer")
    host_target = None
    target = windows.MSVC_TARGETS.get(windows_architecture)
    if modes or target:
        env["RUSTUP_AUTO_INSTALL"] = "0"
        version = run([rustc, "-vV"], cwd=work, env=env).stdout.splitlines()
        if f"release: {env['RUSTUP_TOOLCHAIN']}" not in version:
            raise ValueError("The prepared Rust compiler differs from the SDK pin")
        host_target = next(
            (line.removeprefix("host: ") for line in version if line.startswith("host: ")), None
        )
        if not host_target or (target and host_target != windows.MSVC_TARGETS[windows_host]):
            raise ValueError("The Rust compiler host must match the native Windows host")
    if target and target != host_target:
        windows.require_rust_target_stdlib(
            run(
                [rustc, "--print", "target-libdir", "--target", target], cwd=work, env=env
            ).stdout.strip(),
            target,
        )
    verifier = _prepare_identity_verifier(
        work, rustc, env.copy(), target, compiler_verified=bool(modes or target)
    )
    cargo_home = work / "cargo-home"
    cargo_home.mkdir()
    env.update(
        CARGO_HOME=str(cargo_home),
        CARGO_TARGET_DIR=str(work / "target"),
        CARGO_NET_GIT_FETCH_WITH_CLI="true",
    )
    _prepare_git_delivery(delivery, work, env, git_url, revision, tree)
    if dependencies:
        pins = _dependency_pins(dependencies)
        sources = (
            _ci_dependency_bundles(pins, verifier)
            if dependency_sources is None
            else {repository: (path, None) for repository, path in dependency_sources.items()}
        )
        used = [Path(delivery)]
        for index, (repository, pin) in enumerate(sorted(pins.items())):
            source, checksum = sources[repository]
            if any(source.samefile(path) for path in used):
                raise ValueError("Each SDK Git repository requires a distinct delivery")
            used.append(source)
            if checksum is not None:
                snapshot = work / source.name
                shutil.copyfile(source, snapshot)
                with snapshot.open("rb") as stream:
                    if hashlib.file_digest(stream, "sha256").hexdigest() != checksum:
                        raise ValueError("The additional SDK Git bundle checksum differs")
                source = snapshot
            mirror = _prepare_git_delivery(
                source, work, env, pin["git"], pin["rev"], None, name=f"dependency-{index}.git"
            )
            if (
                dependency_sources is None
                and _git(mirror, "rev-parse", "HEAD^{commit}", env=env) != pin["rev"]
            ):
                raise ValueError(
                    "The CI source bundle HEAD differs from its advertised SDK Git pin"
                )
    fixture = root / "fixtures/consumers/rust"
    client = work / "client"
    client.mkdir()
    shutil.copytree(fixture / "src", client / "src")
    template = (fixture / "Cargo.toml.in").read_text(encoding="utf-8")
    (client / "Cargo.toml").write_text(
        _rust_manifest(
            template,
            git_url,
            packages,
            revision,
            dependencies=dependencies,
            registry_dependencies=registry_dependencies,
            optional={
                name
                for name in packages
                if modes and any(name not in active for _, active in modes.values())
            },
        ),
        encoding="utf-8",
    )
    target_flags = ["--target", target] if target else []
    run([cargo, "generate-lockfile"], cwd=client, env=env)
    run([cargo, "fetch", "--locked", *target_flags], cwd=client, env=env)
    expected = _git_identity(git_url, revision)
    extra_identities = {
        name: _git_identity(pin["git"], pin["rev"]) for name, pin in (dependencies or {}).items()
    }
    lock = (client / "Cargo.lock").read_bytes()
    locked_packages = tomllib.loads(lock.decode())["package"]
    _verify_packages(
        verifier,
        locked_packages,
        packages,
        expected,
        "Cargo.lock",
        dependencies=extra_identities,
        registry=registry_dependencies,
    )
    owned = work / "files"
    owned.mkdir()
    outputs = {}
    platform = target or host_target
    host_evidence = {"windows_host": windows_host} if windows_host else {}
    for mode, (features, active) in (modes or {None: ((), packages)}).items():
        flags = ["--no-default-features"] if mode else []
        if features:
            flags.extend(("--features", ",".join(features)))
        metadata = json.loads(
            run(
                [
                    cargo,
                    "metadata",
                    "--locked",
                    "--offline",
                    "--format-version",
                    "1",
                    *(["--filter-platform", platform] if platform else []),
                    *flags,
                ],
                cwd=client,
                env=env,
            ).stdout
        )
        _check_rust_metadata(
            metadata,
            client,
            cargo_home,
            verifier,
            packages,
            expected,
            dependencies=extra_identities,
            optional=set(packages) - set(active),
            registry_dependencies=registry_dependencies,
        )
        if mode:
            resolved(tree, expected, lock, metadata, mode=mode, **host_evidence)
            files_for_mode = owned / mode
            files_for_mode.mkdir()
        else:
            resolved(tree, expected, lock, metadata, **host_evidence)
            files_for_mode = owned
        locked = ["--locked", "--offline", *target_flags, *flags]
        if windows_architecture:
            output = _run_windows_rust_client(
                run,
                [cargo, "build", *locked],
                cwd=client,
                env=env,
                metadata=metadata,
                architecture=windows_architecture,
                runtime_dir=files_for_mode,
                verifier=verifier,
            )
        else:
            output = run(
                [cargo, "run", *locked, "--", str(files_for_mode)], cwd=client, env=env
            ).stdout
        if mode and mode_completed:
            mode_completed(mode, output)
        outputs[mode] = output
    return outputs if modes else outputs[None]
