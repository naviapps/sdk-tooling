"""Shared Rust C ABI product metadata, dependencies and build input provenance."""

import os
import re
import subprocess
import tomllib
from pathlib import Path

from .artifacts import files, hashes
from .c_compiler import compile_c
from .cargo import CargoDependencies, notice_root, runtime_notices

DARWIN_TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin")
NOTICE_TARGETS = (*DARWIN_TARGETS, "aarch64-pc-windows-msvc")


def tool_input_hashes(*platform_sources):
    """Record shared build inputs and only the relevant platform implementation."""
    package = Path(__file__).resolve().parent
    shared_sources = (
        Path(__file__),
        package / "artifacts.py",
        package / "c_compiler.py",
        package / "cargo.py",
        package / "cargo_build.py",
        package / "native_plan.py",
    )
    return hashes([*shared_sources, *platform_sources], package)


class FfiProduct:
    def __init__(
        self,
        root,
        ffi="crates/ffi",
        *,
        symbol_prefix,
        notice_packages=None,
        notice_directory="ThirdParty",
        specification="native.toml",
    ):
        self.root = Path(root).resolve()
        self.ffi = self.root / ffi
        if not self.ffi.resolve().is_relative_to(self.root):
            raise ValueError("FFI source must be inside its SDK")
        self.manifest = tomllib.loads((self.ffi / "Cargo.toml").read_text())
        self.package = self.manifest["package"]["name"]
        self.notice_packages = tuple(notice_packages or [self.package])
        self.notice_root = notice_root(self.root, notice_directory)
        self.specification = self.root / specification
        if not self.specification.resolve().is_relative_to(self.root):
            raise ValueError("Native specification must be inside its repository")
        self.library = self.manifest.get("lib", {}).get("name", self.package.replace("-", "_"))
        self.module_map = self.ffi / "module.modulemap"
        if not re.fullmatch(r"[A-Za-z_]\w*", symbol_prefix):
            raise ValueError("A C identifier prefix is required")
        self.symbol_prefix = symbol_prefix
        self.compiler = tomllib.loads((self.root / "rust-toolchain.toml").read_text())["toolchain"][
            "channel"
        ]

    @property
    def version(self):
        version = self.manifest["package"]["version"]
        if isinstance(version, dict) and version.get("workspace") is True:
            version = tomllib.loads((self.root / "Cargo.toml").read_text())["workspace"]["package"][
                "version"
            ]
        if not isinstance(version, str) or not version:
            raise ValueError("The FFI package must declare or inherit its Cargo version")
        return version

    @property
    def headers(self):
        root = self.ffi / "include"
        names = re.findall(r'\bheader\s+"([^"\n]+)"', self.module_map.read_text())
        if not names:
            raise ValueError("The C module map must declare its public headers")
        paths = [root / name for name in names]
        if any(not p.resolve().is_relative_to(root.resolve()) or not p.is_file() for p in paths):
            raise ValueError("Public headers must be files inside the SDK include directory")
        return paths

    def abi_headers(self, *, windows=False):
        if not windows:
            return self.headers
        return sorted((self.ffi / "include").rglob("*.h"))

    def abi_versions(self, *, windows=False):
        """Read the public header's expected version for each ABI entry point."""
        text = "\n".join(
            path.read_text(encoding="utf-8") for path in self.abi_headers(windows=windows)
        )
        portable = self.symbol_prefix + "abi_version"
        windows_getter = self.symbol_prefix + "windows_abi_version"
        getters = [portable]
        if windows and re.search(r"\b" + re.escape(windows_getter) + r"\s*\(", text):
            getters.append(windows_getter)
        versions = {}
        for getter in getters:
            macro = getter.upper()
            values = re.findall(
                rf"^[ \t]*#[ \t]*define[ \t]+{re.escape(macro)}[ \t]+"
                r"([1-9][0-9]{0,9})[uU]?[ \t]*$",
                text,
                flags=re.MULTILINE,
            )
            if len(values) != 1 or int(values[0]) > 0xFFFF_FFFF:
                raise ValueError(f"Declare one positive uint32 ABI version in {macro}")
            versions[getter] = int(values[0])
        return versions

    def compile_abi_probe(self, output, *, compiler, libraries, env, windows=False, flags=()):
        """Link a C caller against the named artifact and compile its ABI assertions."""
        include = self.ffi / "include"
        lines = ["#include <inttypes.h>", "#include <stdio.h>"]
        for header in self.abi_headers(windows=windows):
            name = header.relative_to(include).as_posix()
            if any(character in name for character in ('"', "\\", "\r", "\n")):
                raise ValueError("Invalid public ABI header name")
            lines.append(f'#include "{name}"')
        lines += ["", "int main(void) {"]
        for index, (getter, expected) in enumerate(self.abi_versions(windows=windows).items()):
            macro = getter.upper()
            # The one legacy consumer supplies its independently pinned value and a
            # historical header without the macro. All current SDKs require it above.
            lines += [
                f"#ifdef {macro}",
                f'_Static_assert({macro} == {expected}u, "Unexpected ABI macro: {macro}");',
                "#endif",
                f"    uint32_t (*get_abi_{index})(void) = &{getter};",
                f"    uint32_t abi_{index} = get_abi_{index}();",
                f"    if (abi_{index} != {expected}u) {{",
                f'        fprintf(stderr, "{getter}: expected {expected}, found %" PRIu32 "\\n", abi_{index});',
                "        return 1;",
                "    }",
            ]
        lines += ["    return 0;", "}"]
        source = output.with_suffix(".c")
        source.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return compile_c(
            source,
            output=output,
            compiler=compiler,
            include_dirs=[include],
            libraries=libraries,
            style="msvc" if windows else "clang",
            cwd=output.parent,
            env=env,
            flags=flags,
        )

    def run_abi_probe(self, executable, env):
        try:
            subprocess.run(
                [str(executable)], cwd=executable.parent, env=env, check=True, timeout=30
            )
        except subprocess.CalledProcessError as error:
            raise ValueError(f"Native ABI runtime probe failed for {self.library}") from error

    def hashes(self, paths, relative_to=None):
        return hashes(paths, relative_to or self.root)

    def _mandatory_sources(self):
        paths = [self.root / name for name in ("Cargo.toml", "Cargo.lock", "rust-toolchain.toml")]
        paths += list((self.root / "crates").rglob("Cargo.toml"))
        paths += list((self.root / "crates").rglob("build.rs"))
        paths += self.headers + [self.module_map]
        paths += [p for p in files(self.notice_root) if p.name != ".DS_Store"]
        for path in (self.root / "LICENSE", self.specification):
            if path.is_file():
                paths.append(path)
        return paths

    def notices(self):
        paths = [
            p
            for p in files(self.notice_root)
            if p.name not in (".DS_Store", "registry-notices.json")
        ]
        if (self.root / "LICENSE").is_file():
            paths.insert(0, self.root / "LICENSE")
        return b"\n\n".join(
            ("===== " + p.relative_to(self.root).as_posix() + " =====\n").encode() + p.read_bytes()
            for p in paths
        )

    def build_sources(self):
        # Capture the conservative input tree for build-time drift checks and
        # Windows receipts. XCFramework receipts use recorded production inputs.
        return hashes(
            self._mandatory_sources()
            + [p for p in files(self.root / "crates") if p.name != ".DS_Store"],
            self.root,
        )

    def retain_notices(self, env=None):
        env = env or os.environ.copy()
        relative = self.notice_root.relative_to(self.root).as_posix()
        runtime_notices(self.root, env, notice_directory=relative)
        CargoDependencies(self.root, self.notice_packages, NOTICE_TARGETS, env).retain_notices(
            notice_directory=relative
        )

    def dependencies(self, env, *, targets):
        graph = CargoDependencies(self.root, [self.package], targets, env)
        result = graph.provenance()
        graph.configure_remaps(env)
        return result
