"""Build and verify independent macOS XCFrameworks for Rust C ABIs."""

import hashlib
import json
import os
import plistlib
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path, PurePosixPath

from .artifacts import build_lock, file_sha256, files, hashes, publish, write_json
from .cargo import CargoDependencies
from .cargo_build import compile_units, verify_units
from .ffi import DARWIN_TARGETS, FfiProduct, tool_input_hashes
from .native_plan import SPECIFICATION, product_options, sdk_relative_path, select_product

DEP_INFO_PARSER = Path("crates/rustc-dep-info/src/main.rs")


def dep_info_sources(paths, *, expected_sha256, cwd, env):
    """Read compiler dependency rules using the producer's selected Rust compiler."""
    parser = Path(__file__).resolve().parents[1] / DEP_INFO_PARSER
    if parser.is_symlink():
        raise ValueError("The Rust dependency parser must be an ordinary source file")
    source = parser.read_bytes()
    if hashlib.sha256(source).hexdigest() != expected_sha256:
        raise ValueError("The Rust dependency parser differs from its build input")
    with tempfile.TemporaryDirectory(prefix="sdk-dep-info-") as temporary:
        checker = Path(temporary) / "sdk-rustc-dep-info"
        compiler = [
            "rustc",
            "--edition=2024",
            "--crate-name=sdk_rustc_dep_info",
            "-o",
            checker,
            "-",
        ]
        subprocess.run(compiler, input=source, cwd=cwd, env=env, check=True, timeout=120)
        result = subprocess.run([checker, "--files", *paths], capture_output=True, timeout=120)
        if result.returncode == 2:
            raise ValueError(result.stderr.decode().strip())
        if result.returncode == 1 and result.stderr and not result.stdout:
            raise OSError(result.stderr.decode().strip())
        result.check_returncode()
        if source != parser.read_bytes():
            raise ValueError("The Rust dependency parser source changed during inspection")
        if result.stdout:
            yield from map(Path, result.stdout.decode().split("\n"))


def run(*arguments, env=None, cwd=None):
    return subprocess.check_output(list(map(str, arguments)), env=env, cwd=cwd, text=True).strip()


def abi_probe_architecture():
    architecture = os.uname().machine
    if architecture not in {"arm64", "x86_64"}:
        raise ValueError("The macOS ABI probe requires an arm64 or x86_64 process")
    return architecture


class XcframeworkBuilder(FfiProduct):
    def __init__(
        self,
        root,
        ffi="crates/ffi",
        *,
        symbol_prefix,
        minimum_macos,
        receipt="native-build.json",
        target_directory="target/native",
        notice_packages=None,
        notice_directory="ThirdParty",
        specification="native.toml",
    ):
        super().__init__(
            root,
            ffi,
            symbol_prefix=symbol_prefix,
            notice_packages=notice_packages,
            notice_directory=notice_directory,
            specification=specification,
        )
        self.module = re.search(r"\bmodule\s+(\w+)", self.module_map.read_text())[1]
        if not isinstance(minimum_macos, str) or not re.fullmatch(r"\d+\.\d+", minimum_macos):
            raise ValueError("The minimum macOS version must have major.minor form")
        if (
            not isinstance(receipt, str)
            or Path(receipt).name != receipt
            or not receipt.endswith(".json")
        ):
            raise ValueError("A receipt filename is required")
        self.minimum_macos = minimum_macos
        self.artifact = self.root / "Artifacts" / (self.module + ".xcframework")
        self.receipt = self.root / "Artifacts" / receipt
        self.targets = DARWIN_TARGETS
        if not isinstance(target_directory, str):
            raise ValueError("Native target_directory must be a nonempty string")
        self.target_directory = self.root / sdk_relative_path(
            self.root, target_directory, "target_directory"
        )

    @property
    def configuration(self):
        return dict(
            package=self.package,
            library=self.library,
            module=self.module,
            ffi=self.ffi.relative_to(self.root).as_posix(),
            symbolPrefix=self.symbol_prefix,
            minimumMacOS=self.minimum_macos,
            targets=list(self.targets),
            noticePackages=list(self.notice_packages),
            noticeDirectory=self.notice_root.relative_to(self.root).as_posix(),
            specification=self.specification.relative_to(self.root).as_posix(),
        )

    def tool_inputs(self):
        root = Path(__file__).resolve().parents[1]
        return tool_input_hashes(Path(__file__)) | hashes([root / DEP_INFO_PARSER], root)

    def _production_sources(self, parser_sha256, env=None):
        selected = set()
        deps = [
            self.target_directory / target / "release" / ("lib" + self.library + ".d")
            for target in self.targets
        ]
        for path in dep_info_sources(deps, expected_sha256=parser_sha256, cwd=self.root, env=env):
            path = path.resolve()
            if path.is_relative_to(self.root) and not path.is_relative_to(
                self.target_directory.resolve()
            ):
                selected.add(path)
        if self.ffi / "src/lib.rs" not in selected:
            raise ValueError("Production dependency information omits the FFI root")
        return sorted(selected)

    def _input_roots(self, roots):
        if not isinstance(roots, list):
            raise ValueError("Build-script input roots must be a list")
        selected = set()
        outputs = [
            (self.root / name).resolve()
            for name in (".git", "Artifacts", "target", ".native-build")
        ] + [self.target_directory.resolve()]

        def identities(path):
            result = []
            for ancestor in (path, *path.parents):
                try:
                    info = ancestor.stat()
                except FileNotFoundError:
                    result.append(None)
                else:
                    result.append((info.st_dev, info.st_ino))
            return result

        for name in roots:
            if not isinstance(name, str):
                raise ValueError("Build-script input roots must be relative directories")
            relative = PurePosixPath(name)
            if (
                not relative.parts
                or relative.is_absolute()
                or relative.as_posix() != name
                or ".." in relative.parts
                or "\\" in name
                or ":" in name
            ):
                raise ValueError("Build-script input root must be a canonical relative directory")
            path = self.root
            for part in relative.parts:
                path /= part
                if path.is_symlink():
                    raise ValueError("Build-script input root contains a symlink")
            if not path.is_dir():
                raise ValueError("Build-script input root must be an existing directory")
            source_ids = identities(path)
            if source_ids[0] is None:
                raise ValueError("Build-script input root changed during validation")
            for output in outputs:
                # resolve() can retain case aliases. Existing ancestor identities
                # also cover output directories that have not been created yet.
                output_ids = identities(output)
                if (
                    path.is_relative_to(output)
                    or output.is_relative_to(path)
                    or source_ids[0] in output_ids
                    or output_ids[0] is not None
                    and output_ids[0] in source_ids
                ):
                    raise ValueError(
                        "Build-script input root overlaps repository metadata or output"
                    )
            selected.add(path)
        return sorted(selected)

    def _build_script_roots(self, packages):
        roots = []
        for package in packages:
            scripts = [
                Path(target["src_path"])
                for target in package.get("targets", [])
                if "custom-build" in target.get("kind", [])
            ]
            if not scripts:
                continue
            root = Path(package["manifest_path"]).parent
            if not root.is_absolute() or not root.is_relative_to(self.root):
                raise ValueError("Local build-script package escapes the repository")
            relative = root.relative_to(self.root).as_posix()
            self._input_roots([relative])
            for script in scripts:
                if (
                    not script.is_absolute()
                    or not script.is_relative_to(root)
                    or ".." in script.relative_to(root).parts
                ):
                    raise ValueError("Local build script must stay inside its package input root")
                path = root
                for part in script.relative_to(root).parts:
                    path /= part
                    if path.is_symlink():
                        raise ValueError("Local build script contains a symlink")
                if not script.is_file():
                    raise ValueError("Local build script must be an existing ordinary file")
            roots.append(relative)
        return sorted(set(roots))

    def _build_script_inventory(self, roots):
        selected, directories = set(), set()

        def failed(error):
            raise ValueError("Cannot enumerate build-script input tree") from error

        for root in self._input_roots(roots):
            for directory, names, filenames in os.walk(root, followlinks=False, onerror=failed):
                base = Path(directory)
                if base.is_symlink() or not base.is_dir():
                    raise ValueError("Build-script input directory changed while enumerating")
                directories.add(base.relative_to(self.root).as_posix())
                for name in names + filenames:
                    path = base / name
                    if path.is_symlink():
                        raise ValueError("Build-script input tree contains a symlink")
                    if name in names:
                        if not path.is_dir():
                            raise ValueError(
                                "Build-script input tree contains an invalid directory"
                            )
                    elif not path.is_file():
                        raise ValueError("Build-script input tree contains a non-regular file")
                    elif name != ".DS_Store":
                        selected.add(path)
        return sorted(selected), sorted(directories)

    def _input_snapshot(self, production_sources=None, *, build_script_roots=None):
        if production_sources is None:
            previous = json.loads(self.receipt.read_text())
            if "buildScriptInputRoots" not in previous or not isinstance(
                previous.get("buildScriptInputDirectories"), list
            ):
                raise ValueError(
                    "Native receipt lacks build-script inventory; rebuild the artifact"
                )
            production_sources = [self.root / name for name in previous["productionSources"]]
            build_script_roots = previous["buildScriptInputRoots"]
        elif build_script_roots is None:
            raise ValueError("Explicit production sources require build-script input roots")
        for path in production_sources:
            if not path.resolve().is_relative_to(self.root):
                raise ValueError("Production source escapes the SDK")
        script_files, directories = self._build_script_inventory(build_script_roots)
        return (
            hashes(self._mandatory_sources() + list(production_sources) + script_files, self.root),
            directories,
        )

    def inputs(self, production_sources=None, *, build_script_roots=None):
        return self._input_snapshot(production_sources, build_script_roots=build_script_roots)[0]

    def public_symbols(self):
        text = "\n".join(p.read_text() for p in self.headers)
        symbols = set(re.findall(r"\b(" + re.escape(self.symbol_prefix) + r"\w*)\s*\(", text))
        if not symbols:
            raise ValueError("No public C functions match the SDK's declared prefix")
        return {"_" + name for name in symbols}

    def verify_symbols(self, library, env=None):
        for architecture in ("arm64", "x86_64"):
            actual = set(
                run("xcrun", "nm", "-arch", architecture, "-gjU", library, env=env).splitlines()
            )
            if actual != self.public_symbols():
                raise ValueError(
                    "Native exports differ from the C header: "
                    + str(actual ^ self.public_symbols())
                )

    @property
    def _install_name(self):
        return f"@rpath/{self.module}.framework/Versions/A/{self.module}"

    def verify_platform(self, library, env=None):
        architectures = set(run("xcrun", "lipo", "-archs", library, env=env).split())
        if architectures != {"arm64", "x86_64"}:
            raise ValueError("Native framework must contain exactly arm64 and x86_64")
        for architecture in sorted(architectures):
            output = run("xcrun", "vtool", "-arch", architecture, "-show-build", library, env=env)
            if re.findall(r"^\s*platform (\S+)", output, re.M) != ["MACOS"] or re.findall(
                r"^\s*minos (\S+)", output, re.M
            ) != [self.minimum_macos]:
                raise ValueError("Native platform/minimum OS differs from the SDK policy")
            install_name = subprocess.check_output(
                ["xcrun", "otool", "-arch", architecture, "-X", "-D", str(library)],
                env=env,
                text=True,
            )
            if install_name != self._install_name + "\n":
                raise ValueError(f"Native framework install name differs on {architecture}")

    def verify(self, env=None, *, inspect_native=True):
        env = env or os.environ.copy()
        receipt = json.loads(self.receipt.read_text())
        if receipt.get("schema") != 3 or receipt.get("configuration") != self.configuration:
            raise ValueError("Native build contract changed; rebuild with the pinned tooling")
        if (
            receipt.get("toolInputs") != self.tool_inputs()
            or (
                receipt["inputs"],
                receipt.get("buildScriptInputDirectories"),
            )
            != self._input_snapshot()
        ):
            raise ValueError(
                "Production source, build tooling or notices changed; rebuild the native artifact"
            )
        if not receipt["compiler"].startswith("rustc " + self.compiler + " "):
            raise ValueError("Native compiler differs from rust-toolchain.toml")
        verify_units(
            receipt.get("cargoBuild"),
            receipt.get("dependencies"),
            self.targets,
            self.package,
            self.library,
        )
        if receipt["outputs"] != hashes(files(self.artifact), self.root):
            raise ValueError("Native artifact differs from its receipt")
        if (
            type(receipt.get("abiVersion")) is not int
            or receipt["abiVersion"] != self.abi_versions()[self.symbol_prefix + "abi_version"]
            or receipt.get("abiProbeArchitecture") not in {"arm64", "x86_64"}
        ):
            raise ValueError("Native ABI receipt differs from the public headers")
        framework = self.artifact / "macos-arm64_x86_64" / (self.module + ".framework")
        info = plistlib.loads((framework / "Resources/Info.plist").read_bytes())
        if info["MinimumOSVersion"] != self.minimum_macos:
            raise ValueError("Framework metadata differs from the SDK minimum OS")
        if inspect_native:
            self.verify_platform(framework / self.module, env)
            self.verify_symbols(framework / self.module, env)
            subprocess.run(
                ["codesign", "--verify", "--strict", str(framework)], env=env, check=True
            )
            self.probe_abi(framework / self.module, env)
        print(
            "Native artifact matches production sources, toolchain, ABI and retained notices: "
            + self.module
        )

    def probe_abi(self, library, env):
        architecture = abi_probe_architecture()
        framework_directory = library.parent.parent
        probe_env = {name: value for name, value in env.items() if not name.startswith("DYLD_")}
        digest = file_sha256(library)
        with tempfile.TemporaryDirectory(prefix="sdk-abi-") as temporary:
            probe = self.compile_abi_probe(
                Path(temporary) / "probe",
                compiler=["xcrun", "clang"],
                libraries=[library],
                env=probe_env,
                flags=[
                    "-arch",
                    architecture,
                    "-mmacosx-version-min=" + self.minimum_macos,
                    "-Xlinker",
                    "-rpath",
                    "-Xlinker",
                    str(framework_directory),
                ],
            )
            self.run_abi_probe(probe, probe_env)
        if file_sha256(library) != digest:
            raise ValueError("Native framework changed during the ABI probe")

    def build(self, env=None):
        with build_lock(self.root / ".native-build"):
            self._build(env)

    def _build(self, env):
        if sys.platform != "darwin":
            raise ValueError("XCFramework generation requires native macOS tools")
        env = dict(
            env or os.environ,
            MACOSX_DEPLOYMENT_TARGET=self.minimum_macos,
            CARGO_PROFILE_RELEASE_STRIP="none",
            ZERO_AR_DATE="1",
        )
        compiler = run("rustc", "--version", "--verbose", env=env)
        if not compiler.startswith("rustc " + self.compiler + " "):
            raise ValueError("Preinstall the SDK's pinned Rust compiler and Darwin targets")
        tool_inputs = self.tool_inputs()
        self.retain_notices(env)
        graph = CargoDependencies(self.root, [self.package], self.targets, env)
        dependencies = graph.provenance()
        graph.configure_remaps(env)
        script_roots = self._build_script_roots(graph.workspace_packages)
        env["CARGO_TARGET_DIR"] = str(self.target_directory)
        env["CARGO_ENCODED_RUSTFLAGS"] += (
            "\x1f-Clink-arg=-mmacosx-version-min=" + self.minimum_macos
        )
        before = self.build_sources()
        before_directories = self._build_script_inventory(script_roots)[1]
        previous_outputs = hashes(files(self.root / "Artifacts"), self.root)
        # Let the final library link lay out Mach-O with its installed name.
        cargo_build = {}
        for target in self.targets:
            cargo_build[target] = compile_units(
                [
                    "cargo",
                    "rustc",
                    "-p",
                    self.package,
                    "--lib",
                    "--release",
                    "--target",
                    target,
                    "--locked",
                    "--offline",
                    "--message-format=json-render-diagnostics",
                    "--",
                    f"-Clink-arg=-Wl,-install_name,{self._install_name}",
                    "-Clink-arg=-Xlinker",
                    "-Clink-arg=-oso_prefix",
                    "-Clink-arg=-Xlinker",
                    f"-Clink-arg={self.target_directory.resolve()}/",
                ],
                graph=graph,
                target=target,
                library=self.target_directory
                / target
                / "release"
                / ("lib" + self.library + ".dylib"),
                env=env,
            )
        production = self._production_sources(tool_inputs[DEP_INFO_PARSER.as_posix()], env)
        source_inputs, script_directories = self._input_snapshot(
            production, build_script_roots=script_roots
        )
        if script_directories != before_directories:
            raise ValueError("Build-script input directories changed during build")
        if self.tool_inputs() != tool_inputs:
            raise ValueError("Native build tooling changed during build")
        directory = self.root / ".native-build"
        directory.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(dir=directory) as temporary:
            work = Path(temporary)
            stage = work / "Artifacts"
            destination = self.root / "Artifacts"
            if destination.is_symlink():
                raise ValueError("Artifact destination must not be a symlink")
            if destination.exists():
                shutil.copytree(destination, stage, symlinks=True)
            else:
                stage.mkdir()
            framework = work / (self.module + ".framework")
            contents = framework / "Versions/A"
            for name in ("Headers", "Modules", "Resources"):
                (contents / name).mkdir(parents=True)
                (framework / name).symlink_to("Versions/Current/" + name)
            (framework / "Versions/Current").symlink_to("A")
            (framework / self.module).symlink_to("Versions/Current/" + self.module)
            library = contents / self.module
            run(
                "xcrun",
                "lipo",
                "-create",
                *[
                    self.target_directory / target / "release" / ("lib" + self.library + ".dylib")
                    for target in self.targets
                ],
                "-output",
                library,
                env=env,
            )
            run("xcrun", "strip", "-S", library, env=env)
            self.verify_platform(library, env)
            self.verify_symbols(library, env)
            for header in self.headers:
                destination_header = contents / "Headers" / header.relative_to(self.ffi / "include")
                destination_header.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(header, destination_header)
            shutil.copyfile(self.module_map, contents / "Modules/module.modulemap")
            version = self.version
            (contents / "Resources/Info.plist").write_bytes(
                plistlib.dumps(
                    dict(
                        CFBundleIdentifier="com.naviapps.native." + self.module,
                        CFBundleName=self.module,
                        CFBundleExecutable=self.module,
                        CFBundlePackageType="FMWK",
                        CFBundleVersion=version,
                        CFBundleShortVersionString=version,
                        MinimumOSVersion=self.minimum_macos,
                    )
                )
            )
            # Keep verbatim notices without deep framework paths in Git checkouts
            # on Windows. Original files remain independently retained in the SDK.
            (contents / "Resources/ThirdPartyNotices.txt").write_bytes(self.notices())
            # An anonymous local signature seals the complete framework; no key is used.
            run("codesign", "--force", "--sign", "-", "--timestamp=none", framework, env=env)
            output = stage / self.artifact.name
            if output.exists():
                shutil.rmtree(output)
            run(
                "xcodebuild",
                "-create-xcframework",
                "-framework",
                framework,
                "-output",
                output,
                env=env,
            )
            record = dict(
                schema=3,
                configuration=self.configuration,
                compiler=compiler,
                deploymentTarget=self.minimum_macos,
                linkage="dynamic-framework",
                architectures=["arm64", "x86_64"],
                exportedSymbols=sorted(self.public_symbols()),
                abiVersion=self.abi_versions()[self.symbol_prefix + "abi_version"],
                abiProbeArchitecture=abi_probe_architecture(),
                dependencies=dependencies,
                cargoBuild=cargo_build,
                toolInputs=tool_inputs,
                productionSources=[p.relative_to(self.root).as_posix() for p in production],
                buildScriptInputRoots=script_roots,
                buildScriptInputDirectories=script_directories,
                inputs=source_inputs,
                outputs=hashes(files(output), work),
            )
            write_json(stage / self.receipt.name, record)
            if (
                self.dependencies(dict(env), targets=self.targets) != dependencies
                or self.build_sources() != before
                or self.tool_inputs() != tool_inputs
                or hashes(files(destination), self.root) != previous_outputs
                or self._input_snapshot(production, build_script_roots=script_roots)
                != (source_inputs, script_directories)
            ):
                raise ValueError(
                    "SDK source or artifact changed during build; staged output was not installed"
                )
            publish(stage, destination, lambda: self.verify(env))


def xcframeworks(root=None):
    root, products = product_options(root)
    if any("minimum_macos" not in options for options in products.values()):
        raise ValueError("XCFramework artifacts require a minimum macOS version")
    builders = {
        name: XcframeworkBuilder(root, specification=SPECIFICATION, **options)
        for name, options in products.items()
    }
    for key in ("module", "receipt"):
        if len({getattr(product, key) for product in builders.values()}) != len(builders):
            raise ValueError(f"Native artifacts must have distinct {key} values")
    return builders


def xcframework(root=None, kind=None):
    return select_product(xcframeworks(root), kind)
