"""Offline fixtures: no Cargo builds, real Git commands or network access."""

import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from sdk_tooling import cargo as cargo_module
from sdk_tooling.cargo import CargoDependencies, retain_runtime_notices, runtime_notices

SOURCE = "registry+https://github.com/rust-lang/crates.io-index"
TARGETS = ("aarch64-apple-darwin", "x86_64-apple-darwin", "aarch64-pc-windows-msvc")


def sha(data):
    return hashlib.sha256(data).hexdigest()


def symlink_or_skip(test, path, target, *, target_is_directory=False):
    try:
        path.symlink_to(target, target_is_directory=target_is_directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            test.skipTest("Windows symlink privilege unavailable (WinError 1314)")
        raise


class SymlinkFixtureTests(unittest.TestCase):
    def test_windows_missing_symlink_privilege_is_a_skip(self):
        error = OSError("fixture privilege error")
        error.winerror = 1314
        path = Mock()
        path.symlink_to.side_effect = error
        with (
            patch.object(os, "name", "nt"),
            self.assertRaisesRegex(unittest.SkipTest, "WinError 1314"),
        ):
            symlink_or_skip(self, path, "target")
        path.symlink_to.assert_called_once_with("target", target_is_directory=False)

    def test_other_symlink_errors_propagate(self):
        for platform, winerror in (("nt", 5), ("nt", None), ("posix", 1314)):
            with self.subTest(platform=platform, winerror=winerror):
                error = OSError("fixture unrelated error")
                error.winerror = winerror
                path = Mock()
                path.symlink_to.side_effect = error
                with patch.object(os, "name", platform), self.assertRaises(OSError) as raised:
                    symlink_or_skip(self, path, "target", target_is_directory=True)
                self.assertIs(raised.exception, error)


class CargoTests(unittest.TestCase):
    def test_registry_notice_collection_is_read_only(self):
        notices = self.dependencies().registry_notices()
        self.assertEqual(set(notices), {"public-dep-1.2.3"})
        self.assertIn("LICENSE-MIT", notices["public-dep-1.2.3"]["files"])
        self.assertFalse((self.root / "ThirdParty").exists())

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "sdk"
        self.root.mkdir()
        (self.root / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/ffi", "crates/core"]\n'
        )
        (self.root / "rust-toolchain.toml").write_text('[toolchain]\nchannel = "1.99.1"\n')
        self.cache = self.base / "cargo/registry"
        self.env = {
            "PATH": "/fake/bin",
            "CARGO_HOME": str(self.cache.parent),
            "GH_TOKEN": "management-secret",
        }
        self.descriptors = {}
        self.locked = []
        self.members = []
        self.edges = {target: {} for target in TARGETS}
        self.ffi = self.workspace("sample-ffi", "ffi")
        self.core = self.workspace("sample-core", "core")
        self.dev = self.workspace("dev-only", "dev")
        self.registry = self.registry_package("public-dep")
        for target in TARGETS:
            self.edges[target][self.ffi["id"]] = [
                (self.core["id"], None),
                (self.registry["id"], None),
                (self.dev["id"], "dev"),
            ]
        self.sysroot = self.base / "installed compiler"
        self.sysroot.mkdir()
        self.git_root = None
        self.git_dirty = b""
        self.calls = []
        self.command_patch = patch(
            "sdk_tooling.cargo.subprocess.check_output", side_effect=self.command
        )
        self.command_patch.start()
        self.addCleanup(self.command_patch.stop)

    def workspace(self, name, directory):
        path = self.root / "crates" / directory
        (path / "src").mkdir(parents=True)
        (path / "Cargo.toml").write_text(f'[package]\nname = "{name}"\nversion = "0.1.0"\n')
        (path / "src/lib.rs").write_text("pub fn fixture() {}\n")
        package = {
            "id": "path+" + path.as_uri() + "#0.1.0",
            "name": name,
            "version": "0.1.0",
            "manifest_path": str(path / "Cargo.toml"),
            "source": None,
            "targets": [],
        }
        self.descriptors[package["id"]] = package
        self.members.append(package["id"])
        self.locked.append({"name": name, "version": "0.1.0"})
        return package

    def registry_package(self, name, files=None, extra_member=None):
        directory = name + "-1.2.3"
        source = self.cache / "src/index-id" / directory
        archive = self.cache / "cache/index-id" / (directory + ".crate")
        source.mkdir(parents=True)
        archive.parent.mkdir(parents=True, exist_ok=True)
        files = files or {
            "Cargo.toml": f'[package]\nname = "{name}"\nversion = "1.2.3"\n'.encode(),
            "src/lib.rs": b"pub fn upstream() {}\n",
            "LICENSE-MIT": b"Exact upstream license\n",
        }
        with tarfile.open(archive, "w:gz") as packed:
            for relative, data in sorted(files.items()):
                member = tarfile.TarInfo(directory + "/" + relative)
                member.size = len(data)
                packed.addfile(member, io.BytesIO(data))
                path = source / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(data)
            if extra_member is not None:
                packed.addfile(extra_member)
        (source / ".cargo-checksum.json").write_text('{"files":{},"package":"editable"}')
        (source / ".cargo-ok").write_text("ok")
        package = {
            "id": SOURCE + "#" + name + "@1.2.3",
            "name": name,
            "version": "1.2.3",
            "manifest_path": str(source / "Cargo.toml"),
            "source": SOURCE,
            "targets": [],
        }
        self.descriptors[package["id"]] = package
        self.locked.append(
            {
                "name": name,
                "version": "1.2.3",
                "source": SOURCE,
                "checksum": sha(archive.read_bytes()),
            }
        )
        return package

    def add_git(self, *, root_crate=False):
        self.git_root = self.base / "private checkout"
        crate = self.git_root if root_crate else self.git_root / "crates/private"
        (crate / "src").mkdir(parents=True)
        if not root_crate:
            (self.git_root / "Cargo.toml").write_text('[workspace]\nmembers = ["crates/private"]\n')
        (crate / "Cargo.toml").write_text('[package]\nname = "private-dep"\nversion = "0.3.0"\n')
        (crate / "src/lib.rs").write_text("pub fn private_source() {}\n")
        (crate / "build.rs").write_text("fn main() {}\n")
        self.git_sha = "a" * 40
        source = (
            "git+https://github.com/naviapps/LicenseKit.git?rev="
            + self.git_sha
            + "#"
            + self.git_sha
        )
        package = {
            "id": source + "#private-dep@0.3.0",
            "name": "private-dep",
            "version": "0.3.0",
            "source": source,
            "manifest_path": str(crate / "Cargo.toml"),
            "targets": [],
        }
        self.descriptors[package["id"]] = package
        self.locked.append({"name": "private-dep", "version": "0.3.0", "source": source})
        for target in TARGETS:
            self.edges[target][self.ffi["id"]].append((package["id"], None))
        self.refresh_git_tree()
        return package

    def refresh_git_tree(self):
        entries = []
        for path in sorted(self.git_root.rglob("*")):
            if path.is_symlink() or path.is_file():
                data = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
                mode = b"120000" if path.is_symlink() else b"100644"
                blob = hashlib.sha1(b"blob " + str(len(data)).encode() + b"\0" + data).hexdigest()
                entries.append(
                    mode
                    + b" blob "
                    + blob.encode()
                    + b"\t"
                    + path.relative_to(self.git_root).as_posix().encode()
                )
        self.git_tree = b"\0".join(entries) + b"\0"

    def command(self, arguments, **options):
        self.calls.append((arguments, options))
        self.assertNotIn("GH_TOKEN", options["env"])
        self.assertEqual(options["env"]["CARGO_NET_OFFLINE"], "true")
        if arguments[0] == "cargo":
            self.assertEqual(
                arguments[1:7],
                ["metadata", "--locked", "--offline", "--format-version", "1", "--filter-platform"],
            )
            target = arguments[-1]
            nodes = [
                {
                    "id": package["id"],
                    "features": ["selected"],
                    "deps": [
                        {"pkg": identity, "dep_kinds": [{"kind": kind, "target": None}]}
                        for identity, kind in self.edges[target].get(package["id"], [])
                    ],
                }
                for package in self.descriptors.values()
            ]
            return json.dumps(
                {
                    "workspace_root": str(self.root),
                    "workspace_members": self.members,
                    "packages": list(self.descriptors.values()),
                    "resolve": {"nodes": nodes},
                }
            ).encode()
        if arguments == ["rustc", "--print", "sysroot"]:
            return str(self.sysroot).encode()
        if arguments[0] == "git":
            if arguments[1:] == ["rev-parse", "--show-toplevel"]:
                return str(self.git_root).encode()
            if arguments[1:] == ["rev-parse", "HEAD"]:
                return self.git_sha.encode()
            if arguments[1] == "status":
                return self.git_dirty
            if arguments[1] == "ls-tree":
                return self.git_tree
        self.fail("Unexpected native command in fixture: " + repr(arguments))

    def dependencies(self, targets=TARGETS):
        sections = []
        for item in self.locked:
            sections.append(
                "[[package]]\n"
                + "\n".join(key + " = " + json.dumps(value) for key, value in item.items())
            )
        (self.root / "Cargo.lock").write_text("version = 4\n\n" + "\n\n".join(sections))
        return CargoDependencies(self.root, ["sample-ffi"], targets, self.env)

    def select(self, package, target=None, kind=None):
        for triple in (target,) if target else TARGETS:
            self.edges[triple][self.ffi["id"]].append((package["id"], kind))

    def test_selected_graph_unions_platform_build_dependencies_and_excludes_dev(self):
        build = self.workspace("build-helper", "build-helper")
        windows = self.registry_package("windows-dep")
        self.select(build, kind="build")
        self.select(windows, target=TARGETS[2])
        dependencies = self.dependencies()
        self.assertEqual(
            [p["name"] for p in dependencies.workspace_packages],
            ["build-helper", "sample-core", "sample-ffi"],
        )
        self.assertEqual(
            [p["name"] for p in dependencies.packages],
            ["build-helper", "public-dep", "sample-core", "sample-ffi", "windows-dep"],
        )
        self.assertEqual(len(self.calls), 3)
        records = dependencies.provenance()["packages"]
        self.assertEqual(
            next(p for p in records if p["name"] == "windows-dep")["features"],
            {TARGETS[2]: ["selected"]},
        )

    def test_single_ffi_does_not_reach_an_independent_workspace_ffi(self):
        other = self.workspace("other-ffi", "other")
        extra = self.registry_package("other-only")
        for target in TARGETS:
            self.edges[target][other["id"]] = [(extra["id"], None)]
        dependencies = self.dependencies()
        self.assertNotIn("other-only", [p["name"] for p in dependencies.packages])
        self.assertNotIn("other-ffi", [p["name"] for p in dependencies.workspace_packages])

    def test_provenance_contains_workspace_build_inputs_without_local_absolute_paths(self):
        (self.root / "crates/core/build.rs").write_text("fn main() {}\n")
        (self.root / ".cargo").mkdir()
        (self.root / ".cargo/config.toml").write_text("[build]\nincremental = false\n")
        dependencies = self.dependencies()
        provenance = dependencies.provenance()
        self.assertEqual(provenance, dependencies.provenance())
        self.assertNotIn(str(self.base), json.dumps(provenance, sort_keys=True))
        self.assertIn(".cargo/config.toml", provenance["workspaceInputs"])
        core = next(p for p in provenance["packages"] if p["name"] == "sample-core")
        self.assertIn("crates/core/build.rs", core["inputs"])
        (self.root / "crates/core/build.rs").write_text("fn main() { changed(); }\n")
        self.assertNotEqual(provenance, dependencies.provenance())

    def test_workspace_records_leave_production_rust_inputs_to_compiler_dep_info(self):
        dependencies = self.dependencies()
        before = dependencies.provenance()
        core = next(p for p in before["packages"] if p["name"] == "sample-core")
        self.assertEqual(set(core["inputs"]), {"Cargo.toml", "crates/core/Cargo.toml"})
        (self.root / "crates/core/src/tests.rs").write_text("#[test] fn test_only() {}\n")
        self.assertEqual(before, dependencies.provenance())
        # Actual production .rs fingerprints also belong to NativeBuilder's
        # compiler dep-info, not this Cargo metadata record.
        (self.root / "crates/core/src/lib.rs").write_text("pub fn production_edit() {}\n")
        self.assertEqual(before, dependencies.provenance())

    def test_workspace_custom_build_target_is_fingerprinted(self):
        script = self.root / "crates/core/scripts/build-custom.rs"
        script.parent.mkdir()
        script.write_text("fn main() {}\n")
        self.core["targets"] = [{"kind": ["custom-build"], "src_path": str(script)}]
        dependencies = self.dependencies()
        before = dependencies.provenance()
        core = next(p for p in before["packages"] if p["name"] == "sample-core")
        self.assertIn("crates/core/scripts/build-custom.rs", core["inputs"])
        script.write_text("fn main() { changed(); }\n")
        self.assertNotEqual(before, dependencies.provenance())

    def test_disabled_workspace_build_script_is_not_a_build_input(self):
        manifest = self.root / "crates/core/Cargo.toml"
        manifest.write_text(manifest.read_text() + "build = false\n")
        script = manifest.parent / "build.rs"
        script.write_text("unused build script")
        dependencies = self.dependencies()
        before = dependencies.provenance()
        script.write_text("unused edit")
        self.assertEqual(before, dependencies.provenance())

    def test_workspace_build_script_cannot_escape(self):
        outside = self.base / "outside-build.rs"
        outside.write_text("fn main() {}\n")
        self.core["targets"] = [{"kind": ["custom-build"], "src_path": str(outside)}]
        with self.assertRaisesRegex(RuntimeError, "escapes"):
            self.dependencies().provenance()

    def test_workspace_build_script_cannot_follow_a_link(self):
        outside = self.base / "outside-build.rs"
        outside.write_text("fn main() {}\n")
        linked = self.root / "crates/core/build.rs"
        symlink_or_skip(self, linked, outside)
        self.core["targets"] = [{"kind": ["custom-build"], "src_path": str(linked)}]
        with self.assertRaisesRegex(RuntimeError, "contains a link"):
            self.dependencies().provenance()

    def test_archive_checksum_is_authoritative_over_editable_cache_manifest(self):
        dependencies = self.dependencies()
        archive = self.cache / "cache/index-id/public-dep-1.2.3.crate"
        archive.write_bytes(archive.read_bytes() + b"changed")
        with self.assertRaisesRegex(RuntimeError, "locked checksum"):
            dependencies.provenance()

    def test_changed_cached_source_bytes_are_rejected(self):
        dependencies = self.dependencies()
        (Path(self.registry["manifest_path"]).parent / "src/lib.rs").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "verified locked archive"):
            dependencies.provenance()

    def test_missing_archive_source_file_is_rejected(self):
        dependencies = self.dependencies()
        (Path(self.registry["manifest_path"]).parent / "LICENSE-MIT").unlink()
        with self.assertRaisesRegex(RuntimeError, "verified locked archive"):
            dependencies.provenance()

    def test_extra_cached_build_source_is_rejected(self):
        dependencies = self.dependencies()
        (Path(self.registry["manifest_path"]).parent / "build.rs").write_text("fn main() {}")
        with self.assertRaisesRegex(RuntimeError, "verified locked archive"):
            dependencies.provenance()

    def test_archive_traversal_is_rejected_before_notice_writes(self):
        member = tarfile.TarInfo("unsafe-dep-1.2.3/../../escape")
        malicious = self.registry_package("unsafe-dep", extra_member=member)
        self.select(malicious)
        dependencies = self.dependencies()
        with self.assertRaisesRegex(RuntimeError, "Unsafe relative"):
            dependencies.retain_notices()
        self.assertFalse((self.root / "ThirdParty").exists())

    def test_archive_links_are_rejected(self):
        member = tarfile.TarInfo("linked-dep-1.2.3/alias")
        member.type, member.linkname = tarfile.SYMTYPE, "LICENSE-MIT"
        malicious = self.registry_package("linked-dep", extra_member=member)
        self.select(malicious)
        with self.assertRaisesRegex(RuntimeError, "link or unsupported"):
            self.dependencies().provenance()

    def test_git_requires_exact_lock_identity_clean_head_and_source_bytes(self):
        self.add_git()
        dependencies = self.dependencies()
        provenance = dependencies.provenance()
        private = next(p for p in provenance["packages"] if p["name"] == "private-dep")
        self.assertIn("Cargo.toml", private["inputs"])
        self.assertIn("crates/private/build.rs", private["inputs"])
        self.assertNotIn(str(self.base), json.dumps(private))
        self.git_dirty = b" M crates/private/src/lib.rs\0"
        with self.assertRaisesRegex(RuntimeError, "must be clean"):
            dependencies.provenance()

    def test_git_unreported_dirty_bytes_are_compared_to_commit_objects(self):
        self.add_git()
        dependencies = self.dependencies()
        (self.git_root / "crates/private/src/lib.rs").write_text("edited but status omitted it")
        with self.assertRaisesRegex(RuntimeError, "bytes differ"):
            dependencies.provenance()

    def assert_git_cargo_marker_preserves_source_inventory(self, *, root_crate):
        self.add_git(root_crate=root_crate)
        dependencies = self.dependencies()
        before = dependencies.provenance()
        (self.git_root / ".cargo-ok").write_bytes(b"ok")
        self.git_dirty = b"?? .cargo-ok\0"
        self.assertEqual(dependencies.provenance(), before)
        self.assertTrue(
            any(
                arguments == ["git", "status", "--porcelain=v1", "--untracked-files=all", "-z"]
                for arguments, _ in self.calls
            )
        )

    def test_git_subcrate_accepts_only_cargo_root_marker_without_new_source_inputs(self):
        self.assert_git_cargo_marker_preserves_source_inventory(root_crate=False)

    def test_git_root_crate_accepts_only_cargo_root_marker_without_new_source_inputs(self):
        self.assert_git_cargo_marker_preserves_source_inventory(root_crate=True)

    def test_git_other_untracked_or_modified_paths_are_not_cargo_markers(self):
        self.add_git()
        dependencies = self.dependencies()
        for report in (
            b"?? other-untracked\0",
            b"?? .cargo-ok\0?? other-untracked\0",
            b"?? crates/private/.cargo-ok\0",
            b" M .cargo-ok\0",
        ):
            with self.subTest(report=report):
                self.git_dirty = report
                with self.assertRaisesRegex(RuntimeError, "must be clean"):
                    dependencies.provenance()

    def test_git_cargo_root_marker_does_not_hide_dirty_source(self):
        self.add_git()
        dependencies = self.dependencies()
        (self.git_root / ".cargo-ok").write_bytes(b"ok")
        (self.git_root / "crates/private/src/lib.rs").write_bytes(b"changed source\n")
        self.git_dirty = b"?? .cargo-ok\0 M crates/private/src/lib.rs\0"
        with self.assertRaisesRegex(RuntimeError, "must be clean"):
            dependencies.provenance()

    def test_git_dependency_ignores_tracked_framework_symlinks_and_unrelated_sdk_files(self):
        self.add_git()
        framework = (
            self.git_root
            / "Artifacts/CLicenseKit.xcframework/macos-arm64_x86_64/CLicenseKit.framework"
        )
        (framework / "Versions/A/Headers").mkdir(parents=True)
        (framework / "Versions/A/Headers/license.h").write_text("// prebuilt framework header\n")
        (framework / "Versions/A/CLicenseKit").write_bytes(b"prebuilt Mach-O fixture")
        unrelated = self.git_root / "crates/unrelated/src/lib.rs"
        unrelated.parent.mkdir(parents=True)
        unrelated.write_text("// independent SDK crate\n")
        self.refresh_git_tree()
        dependencies = self.dependencies()
        read = cargo_module._read

        def selected_reads_only(path):
            if "Artifacts" in path.parts or "unrelated" in path.parts:
                raise AssertionError("Unselected SDK source/artifact was read")
            return read(path)

        with patch("sdk_tooling.cargo._read", side_effect=selected_reads_only):
            result = dependencies.provenance()
        private = next(p for p in result["packages"] if p["name"] == "private-dep")
        self.assertIn("crates/private/src/lib.rs", private["inputs"])
        self.assertIn("Cargo.toml", private["inputs"])
        self.assertFalse(
            any("Artifacts" in path or "unrelated" in path for path in private["inputs"])
        )
        with self.subTest(fixture="tracked framework symlinks"):
            symlink_or_skip(self, framework / "Versions/Current", "A", target_is_directory=True)
            symlink_or_skip(
                self, framework / "Headers", "Versions/Current/Headers", target_is_directory=True
            )
            symlink_or_skip(self, framework / "CLicenseKit", "Versions/Current/CLicenseKit")
            self.refresh_git_tree()
            with patch("sdk_tooling.cargo._read", side_effect=selected_reads_only):
                self.assertEqual(dependencies.provenance(), result)

    def test_git_selected_source_symlink_is_rejected_without_following_its_target(self):
        self.add_git()
        outside = self.base / "outside.rs"
        outside.write_text("private outside bytes")
        path = self.git_root / "crates/private/src/lib.rs"
        path.unlink()
        symlink_or_skip(self, path, outside)
        self.refresh_git_tree()
        with self.assertRaisesRegex(RuntimeError, "link|regular file"):
            self.dependencies().provenance()

    def test_git_selected_source_directory_link_is_rejected(self):
        self.add_git()
        outside = self.base / "outside-source"
        outside.mkdir()
        (outside / "other.rs").write_text("outside")
        symlink_or_skip(
            self, self.git_root / "crates/private/src/alias", outside, target_is_directory=True
        )
        self.refresh_git_tree()
        with self.assertRaisesRegex(RuntimeError, "contains a link"):
            self.dependencies().provenance()

    def test_git_missing_tracked_selected_source_cannot_pass_as_a_smaller_inventory(self):
        self.add_git()
        dependencies = self.dependencies()
        dependencies.provenance()
        (self.git_root / "crates/private/src/lib.rs").unlink()
        with self.assertRaisesRegex(RuntimeError, "regular file"):
            dependencies.provenance()

    def test_postbuild_provenance_rechecks_the_same_git_dependency_instance(self):
        self.add_git()
        dependencies = self.dependencies()
        dependencies.provenance()
        dependencies.configure_remaps({})
        (self.git_root / "crates/private/build.rs").write_text("changed after validation")
        with self.assertRaisesRegex(RuntimeError, "bytes differ"):
            dependencies.provenance()

    def test_git_wrong_revision_is_rejected(self):
        self.add_git()
        dependencies = self.dependencies()
        self.git_sha = "b" * 40
        with self.assertRaisesRegex(RuntimeError, "locked revision"):
            dependencies.provenance()

    def test_lock_identity_drift_is_rejected(self):
        self.add_git()
        self.locked[-1]["source"] += "changed"
        with self.assertRaisesRegex(RuntimeError, "exact Cargo.lock"):
            self.dependencies().provenance()

    def test_external_path_patch_is_not_treated_as_a_workspace_dependency(self):
        package = copy.deepcopy(self.core)
        package.update(
            id="external-path",
            name="patched-dep",
            manifest_path=str(self.base / "external/Cargo.toml"),
        )
        self.descriptors[package["id"]] = package
        self.select(package)
        with self.assertRaisesRegex(RuntimeError, "External path patches"):
            self.dependencies().provenance()

    def test_remaps_preserve_rustflags_and_cover_private_git_registry_sysroot(self):
        self.add_git()
        dependencies = self.dependencies()
        env = {"RUSTFLAGS": '-C debuginfo=1 --cfg feature="example"'}
        original = env["RUSTFLAGS"]
        dependencies.configure_remaps(env)
        flags = env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f")
        self.assertEqual(flags[:3], ["-C", "debuginfo=1", "--cfg"])
        self.assertIn('feature="example"', flags)
        self.assertEqual(env["RUSTFLAGS"], original)
        for path in (
            self.root,
            self.sysroot,
            self.git_root,
            Path(self.registry["manifest_path"]).parent,
        ):
            self.assertTrue(
                any(flag.startswith("--remap-path-prefix=" + str(path) + "=") for flag in flags)
            )
        dependencies.configure_remaps(env)
        self.assertEqual(env["CARGO_ENCODED_RUSTFLAGS"].split("\x1f"), flags)

    def test_encoded_flags_keep_precedence_over_plain_flags(self):
        dependencies = self.dependencies()
        env = {"CARGO_ENCODED_RUSTFLAGS": "-C\x1flto=thin", "RUSTFLAGS": "--unused-original"}
        dependencies.configure_remaps(env)
        self.assertTrue(env["CARGO_ENCODED_RUSTFLAGS"].startswith("-C\x1flto=thin\x1f"))
        self.assertEqual(env["RUSTFLAGS"], "--unused-original")
        env = {"CARGO_ENCODED_RUSTFLAGS": "", "RUSTFLAGS": "--unused-original"}
        dependencies.configure_remaps(env)
        self.assertNotIn("--unused-original", env["CARGO_ENCODED_RUSTFLAGS"])

    def test_notices_preserve_manual_material_and_only_prune_owned_registry_dirs(self):
        manual = self.root / "ThirdParty/LicenseKit"
        manual.mkdir(parents=True)
        (manual / "LICENSE").write_bytes(b"Original private upstream notice")
        rust = self.root / "ThirdParty/Rust"
        rust.mkdir()
        (rust / "LICENSE-MIT").write_bytes(b"Original retained Rust notice")
        dependencies = self.dependencies()
        records = dependencies.retain_notices()
        self.assertEqual(records[0]["files"]["LICENSE-MIT"], sha(b"Exact upstream license\n"))
        for target in TARGETS:
            self.edges[target][self.ffi["id"]] = [(self.core["id"], None)]
        self.assertEqual(self.dependencies().retain_notices(), [])
        self.assertFalse((self.root / "ThirdParty/public-dep-1.2.3").exists())
        self.assertEqual((manual / "LICENSE").read_bytes(), b"Original private upstream notice")
        self.assertEqual((rust / "LICENSE-MIT").read_bytes(), b"Original retained Rust notice")

    def test_notice_index_is_byte_stable_with_windows_text_defaults(self):
        dependencies = self.dependencies()
        dependencies.retain_notices()
        manifest = self.root / "ThirdParty/registry-notices.json"
        original = manifest.read_bytes()
        native_open = io.open

        def windows_open(
            file,
            mode="r",
            buffering=-1,
            encoding=None,
            errors=None,
            newline=None,
            closefd=True,
            opener=None,
        ):
            if "b" not in mode and any(flag in mode for flag in "wax") and newline is None:
                newline = "\r\n"
            return native_open(file, mode, buffering, encoding, errors, newline, closefd, opener)

        with patch("io.open", side_effect=windows_open):
            records = dependencies.retain_notices()
        self.assertEqual(manifest.read_bytes(), original)
        self.assertNotIn(b"\r", original)
        self.assertTrue(original.endswith(b"\n"))
        self.assertEqual(json.loads(original.decode("utf-8")), records)
        self.assertEqual(
            (self.root / "ThirdParty/public-dep-1.2.3/LICENSE-MIT").read_bytes(),
            b"Exact upstream license\n",
        )

    def test_manual_crate_directory_collision_is_not_adopted_or_overwritten(self):
        manual = self.root / "ThirdParty/public-dep-1.2.3"
        manual.mkdir(parents=True)
        (manual / "LICENSE-MIT").write_text("manual original")
        with self.assertRaisesRegex(RuntimeError, "manual upstream"):
            self.dependencies().retain_notices()
        self.assertEqual((manual / "LICENSE-MIT").read_text(), "manual original")

    def test_scoped_registry_retention_cannot_replace_other_notice_owners(self):
        self.dependencies().retain_notices()
        outer = (self.root / "ThirdParty/registry-notices.json").read_bytes()
        selected = "ThirdParty/HostCore"
        self.dependencies().retain_notices(notice_directory=selected)
        self.assertEqual((self.root / "ThirdParty/registry-notices.json").read_bytes(), outer)
        self.assertEqual(
            (self.root / selected / "public-dep-1.2.3/LICENSE-MIT").read_bytes(),
            b"Exact upstream license\n",
        )
        for target in TARGETS:
            self.edges[target][self.ffi["id"]] = [(self.core["id"], None)]
        self.dependencies().retain_notices(notice_directory=selected)
        self.assertFalse((self.root / selected / "public-dep-1.2.3").exists())
        self.assertTrue((self.root / "ThirdParty/public-dep-1.2.3/LICENSE-MIT").is_file())

    def test_scoped_notice_root_rejects_parent_paths_and_symlinked_ancestors(self):
        for path in ("../foreign", "/absolute", "ThirdParty/../foreign"):
            with self.subTest(path=path), self.assertRaisesRegex(RuntimeError, "Unsafe relative"):
                self.dependencies().retain_notices(notice_directory=path)
        foreign = self.base / "foreign"
        foreign.mkdir()
        symlink_or_skip(self, self.root / "ThirdParty", foreign, target_is_directory=True)
        with self.assertRaisesRegex(RuntimeError, "symlink"):
            self.dependencies().retain_notices(notice_directory="ThirdParty/HostCore")
        self.assertEqual(list(foreign.iterdir()), [])

    def test_changed_generated_notice_is_not_overwritten_or_pruned(self):
        dependencies = self.dependencies()
        dependencies.retain_notices()
        generated = self.root / "ThirdParty/public-dep-1.2.3/LICENSE-MIT"
        generated.write_text("manual addition to generated material")
        manifest = (self.root / "ThirdParty/registry-notices.json").read_bytes()
        with self.assertRaisesRegex(RuntimeError, "ownership changed"):
            dependencies.retain_notices()
        self.assertEqual(generated.read_text(), "manual addition to generated material")
        self.assertEqual((self.root / "ThirdParty/registry-notices.json").read_bytes(), manifest)

    def test_invalid_previous_ownership_path_cannot_prune_manual_notices(self):
        third_party = self.root / "ThirdParty"
        third_party.mkdir()
        record = {"directory": "../LicenseKit", "source": SOURCE, "checksum": "a" * 64}
        (third_party / "registry-notices.json").write_text(json.dumps([record]))
        with self.assertRaisesRegex(RuntimeError, "Invalid generated"):
            self.dependencies().retain_notices()

    def test_registry_manifest_without_file_hashes_preserves_existing_notice(self):
        destination = self.root / "ThirdParty/public-dep-1.2.3"
        destination.mkdir(parents=True)
        (destination / "LICENSE-MIT").write_bytes(b"Exact upstream license\n")
        record = dict(self.locked[-1], directory="public-dep-1.2.3")
        record.pop("name")
        record.pop("version")
        manifest = destination.parent / "registry-notices.json"
        original = json.dumps([record]).encode("utf-8")
        manifest.write_bytes(original)
        with self.assertRaisesRegex(RuntimeError, "Invalid generated notice file hashes"):
            self.dependencies().retain_notices()
        self.assertEqual(manifest.read_bytes(), original)
        self.assertEqual((destination / "LICENSE-MIT").read_bytes(), b"Exact upstream license\n")

    def test_sqlite_and_nested_license_file_are_retained_from_verified_sources(self):
        files = {
            "Cargo.toml": b'[package]\nname = "libsqlite3-sys"\nversion = "1.2.3"\nlicense-file = "legal/LICENSE"\n',
            "legal/LICENSE": b"upstream crate MIT license",
            "sqlite3/sqlite3.h": b"/* SQLite author disclaims copyright. */\nint sqlite_fixture;",
        }
        sqlite = self.registry_package("libsqlite3-sys", files)
        self.select(sqlite)
        self.dependencies().retain_notices()
        output = self.root / "ThirdParty/libsqlite3-sys-1.2.3"
        self.assertEqual((output / "legal/LICENSE").read_bytes(), files["legal/LICENSE"])
        self.assertIn(b"disclaims copyright", (output / "sqlite3/NOTICE.txt").read_bytes())

    def test_failed_notice_publication_restores_owned_directories_and_manifest(self):
        dependencies = self.dependencies()
        dependencies.retain_notices()
        third_party = self.root / "ThirdParty"
        previous = {
            p.relative_to(third_party).as_posix(): p.read_bytes()
            for p in third_party.rglob("*")
            if p.is_file()
        }
        replace = os.replace

        def fail_manifest(source, destination):
            if Path(destination) == third_party / "registry-notices.json":
                raise OSError("injected manifest publication failure")
            return replace(source, destination)

        with patch("sdk_tooling.cargo.os.replace", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                dependencies.retain_notices()
        actual = {
            p.relative_to(third_party).as_posix(): p.read_bytes()
            for p in third_party.rglob("*")
            if p.is_file()
        }
        self.assertEqual(previous, actual)

    def test_interrupt_after_notice_backup_move_restores_old_tree(self):
        dependencies = self.dependencies()
        dependencies.retain_notices()
        third_party = self.root / "ThirdParty"
        original = third_party / "public-dep-1.2.3/LICENSE-MIT"
        expected = original.read_bytes()
        replace = os.replace

        def interrupt(source, destination):
            replace(source, destination)
            if Path(source) == original.parent:
                raise KeyboardInterrupt()

        with patch("sdk_tooling.cargo.os.replace", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                dependencies.retain_notices()
        self.assertEqual(original.read_bytes(), expected)

    def test_interrupt_after_notice_manifest_commit_keeps_new_tree(self):
        self.dependencies().retain_notices()
        third_party = self.root / "ThirdParty"
        for target in TARGETS:
            self.edges[target][self.ffi["id"]] = [(self.core["id"], None)]
        manifest = third_party / "registry-notices.json"
        replace = os.replace

        def interrupt(source, destination):
            replace(source, destination)
            if Path(destination) == manifest:
                raise KeyboardInterrupt()

        with patch("sdk_tooling.cargo.os.replace", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.dependencies().retain_notices()
        self.assertEqual(json.loads(manifest.read_bytes()), [])
        self.assertFalse((third_party / "public-dep-1.2.3").exists())
        self.assertEqual(list(third_party.glob(".cargo-notices-*")), [])

    def test_runtime_notice_validation_is_explicit_and_does_not_invent_missing_copies(self):
        with self.assertRaisesRegex(RuntimeError, "Missing retained Rust provenance"):
            runtime_notices(self.root, self.env)
        rust = self.root / "ThirdParty/Rust"
        rust.mkdir(parents=True)
        files = {}
        for name in ("LICENSE-APACHE", "LICENSE-MIT", "COPYRIGHT", "COPYRIGHT-library.html"):
            data = ("Pinned installed compiler notice " + name).encode()
            (rust / name).write_bytes(data)
            files[name] = {"bytes": len(data), "sha256": sha(data)}
        (rust / "provenance.json").write_text(json.dumps({"version": "1.99.1", "files": files}))
        self.assertEqual(
            runtime_notices(self.root, self.env), {"version": "1.99.1", "files": files}
        )
        (rust / "LICENSE-MIT").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "differs from retained provenance"):
            runtime_notices(self.root, self.env)
        self.assertEqual(self.calls, [])

    def test_command_failures_report_only_the_operation_and_safe_classification(self):
        private_argv = ["cargo", "fixture-private-argument"]
        private_streams = {"output": b"fixture-private-stdout", "stderr": b"fixture-private-stderr"}
        for failure, classification in (
            (subprocess.TimeoutExpired(private_argv, 120, **private_streams), "timed out"),
            (
                subprocess.CalledProcessError(128, private_argv, **private_streams),
                "exited with status 128",
            ),
            (
                FileNotFoundError(2, "fixture-private-launch-error", "fixture-private-path"),
                "could not start or communicate with the subprocess",
            ),
            (subprocess.SubprocessError("fixture-private-process-error"), "subprocess failed"),
        ):
            stdout, stderr = io.StringIO(), io.StringIO()
            with (
                self.subTest(classification=classification),
                patch("sdk_tooling.cargo.subprocess.check_output", side_effect=failure) as command,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
                self.assertRaises(RuntimeError) as error,
            ):
                CargoDependencies(self.root, ["sample-ffi"], TARGETS, self.env)
            self.assertEqual(
                str(error.exception),
                "Offline Cargo dependency inspection failed (Cargo metadata): " + classification,
            )
            self.assertIsNone(error.exception.__cause__)
            self.assertIsNone(error.exception.__context__)
            self.assertEqual((stdout.getvalue(), stderr.getvalue()), ("", ""))
            command.assert_called_once()
            self.assertEqual(command.call_args.kwargs["cwd"], self.root)
            self.assertEqual(command.call_args.kwargs["timeout"], 120)
            self.assertEqual(command.call_args.kwargs["stderr"], subprocess.PIPE)

    def test_git_status_timeout_identifies_the_worktree_check_and_stops_inspection(self):
        self.add_git()
        dependencies = self.dependencies()

        def command(arguments, **options):
            if arguments[:2] == ["git", "status"]:
                raise subprocess.TimeoutExpired(arguments, 120, stderr=b"fixture-private-path")
            return self.command(arguments, **options)

        with (
            patch("sdk_tooling.cargo.subprocess.check_output", side_effect=command) as run,
            self.assertRaises(RuntimeError) as error,
        ):
            dependencies.provenance()
        self.assertEqual(
            str(error.exception),
            "Offline Cargo dependency inspection failed (Git worktree status): timed out",
        )
        self.assertEqual(
            [call.args[0][1] for call in run.call_args_list], ["rev-parse", "rev-parse", "status"]
        )


class RuntimeRetentionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name).resolve()
        self.root = self.base / "sdk"
        self.root.mkdir()
        (self.root / "rust-toolchain.toml").write_text('[toolchain]\nchannel="1.99.1"\n')
        self.destination = self.root / "ThirdParty/Rust"
        self.env = {"PATH": "/not-a-real-tool-path"}
        self.revision = "a" * 40
        self.files = {
            name: ("Public Rust 1.99.1 notice: " + name).encode()
            for name in ("LICENSE-APACHE", "LICENSE-MIT", "COPYRIGHT", "COPYRIGHT-library.html")
        }
        self.source = self.base / "public-rust-source"
        self.source.mkdir()
        (self.source / "version").write_text("1.99.1\n")
        (self.source / "git-commit-hash").write_text(self.revision + "\n")
        for name, data in self.files.items():
            (self.source / name).write_bytes(data)
        self.commands = patch(
            "sdk_tooling.cargo.subprocess.check_output",
            side_effect=AssertionError("Unexpected native command"),
        )
        self.command = self.commands.start()
        self.addCleanup(self.commands.stop)

    def retain(self, source=None):
        return retain_runtime_notices(
            self.root, self.env, self.source if source is None else source
        )

    def test_scoped_runtime_retention_and_verification_preserve_other_owners(self):
        self.retain()
        original = self.tree()
        relative = "ThirdParty/HostCore"
        retained = retain_runtime_notices(
            self.root, self.env, self.source, notice_directory=relative
        )
        self.assertEqual(
            runtime_notices(self.root, {}, notice_directory=relative),
            {"version": "1.99.1", "files": retained["files"]},
        )
        self.assertEqual(self.tree(), original)
        (self.root / relative / "Rust/LICENSE-MIT").write_bytes(b"changed")
        with self.assertRaisesRegex(RuntimeError, "differs"):
            runtime_notices(self.root, {}, notice_directory=relative)

    def tree(self):
        return {
            p.relative_to(self.destination).as_posix(): p.read_bytes()
            for p in self.destination.rglob("*")
            if p.is_file()
        }

    def source_manifest(self):
        manifest = {
            "version": "1.99.1",
            "provenance": {
                "repository": "https://github.com/rust-lang/rust",
                "revision": self.revision,
                "sourceArchiveURL": "https://static.rust-lang.org/dist/rustc-1.99.1-fixture.tar.xz",
                "sourceArchiveSHA256": "b" * 64,
            },
            "files": {
                name: {"bytes": len(data), "sha256": sha(data)} for name, data in self.files.items()
            },
        }
        (self.source / "provenance.json").write_text(json.dumps(manifest))
        return manifest

    def installed(self, *, version="1.99.1", missing=None, component_version="1.99.1"):
        sysroot = self.base / "installed-rust"
        docs = sysroot / "share/doc/rust"
        docs.mkdir(parents=True)
        for name, data in self.files.items():
            if name != missing:
                (docs / name).write_bytes(data)
        component = sysroot / "lib/rustlib/multirust-channel-manifest.toml"
        component.parent.mkdir(parents=True)
        component.write_text(
            '[pkg.rustc]\nversion="' + component_version + ' (aaaaaaaaa 2026-10-13)"\n'
        )

        def command(argv, **options):
            self.assertEqual(options["cwd"], self.root)
            self.assertEqual(options["env"]["RUSTUP_AUTO_INSTALL"], "0")
            if argv == ["rustc", "--version", "--verbose"]:
                return (
                    "rustc "
                    + version
                    + " (aaaaaaaaa 2026-10-13)\nrelease: "
                    + version
                    + "\ncommit-hash: "
                    + self.revision
                    + "\nhost: fixture-host\n"
                ).encode()
            self.assertEqual(argv, ["rustc", "--print", "sysroot"])
            return str(sysroot).encode()

        self.command.side_effect = command
        return sysroot

    def test_explicit_public_tree_retains_four_exact_files_and_portable_provenance(self):
        result = self.retain()
        self.assertEqual(result["version"], "1.99.1")
        self.assertEqual(result["provenance"]["kind"], "public-release-tree")
        self.assertEqual(result["provenance"]["revision"], self.revision)
        self.assertEqual(result["provenance"]["sourceFiles"], {name: name for name in self.files})
        self.assertEqual(json.loads((self.destination / "provenance.json").read_text()), result)
        for name, data in self.files.items():
            self.assertEqual((self.destination / name).read_bytes(), data)
        self.assertNotIn(str(self.base), json.dumps(result))
        self.command.assert_not_called()

    def test_environment_source_and_explicit_argument_precedence(self):
        self.env["SDK_TOOLING_RUST_NOTICE_SOURCE"] = str(self.source)
        result = retain_runtime_notices(self.root, self.env)
        self.assertEqual(result["files"]["COPYRIGHT"]["sha256"], sha(self.files["COPYRIGHT"]))
        self.env["SDK_TOOLING_RUST_NOTICE_SOURCE"] = str(self.base / "missing-source")
        self.assertEqual(self.retain()["provenance"]["kind"], "retained-manifest")
        self.command.assert_not_called()

    def test_public_source_manifest_validates_version_hashes_revision_and_archive_identity(self):
        manifest = self.source_manifest()
        result = self.retain()
        self.assertEqual(result["provenance"]["kind"], "public-source-manifest")
        self.assertEqual(result["provenance"]["sourceArchiveSHA256"], "b" * 64)
        self.assertEqual(
            result["provenance"]["sourceManifestSHA256"],
            sha((self.source / "provenance.json").read_bytes()),
        )
        self.assertEqual(result["files"], manifest["files"])

    def test_source_manifest_corrupt_file_fails_without_sdk_writes(self):
        self.source_manifest()
        (self.source / "LICENSE-MIT").write_text("changed")
        with self.assertRaisesRegex(RuntimeError, "differs from its explicit provenance"):
            self.retain()
        self.assertFalse((self.root / "ThirdParty").exists())

    def test_source_manifest_cannot_claim_another_sdk_as_its_public_origin(self):
        manifest = self.source_manifest()
        manifest["provenance"]["repository"] = "naviapps/configuration-storage"
        (self.source / "provenance.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "lacks public Rust"):
            self.retain()
        self.assertFalse(self.destination.exists())

    def test_version_mismatch_has_no_source_or_compiler_fallback(self):
        (self.source / "version").write_text("1.99.0\n")
        with self.assertRaisesRegex(RuntimeError, "version differs from the SDK pin"):
            self.retain()
        self.assertFalse(self.destination.exists())
        self.command.assert_not_called()

    def test_incomplete_static_source_does_not_generate_or_substitute_missing_notice(self):
        (self.source / "COPYRIGHT-library.html").unlink()
        with self.assertRaisesRegex(RuntimeError, "source is incomplete"):
            self.retain()
        self.assertFalse(self.destination.exists())
        self.command.assert_not_called()

    def test_valid_retained_notices_need_no_rust_source_docs_or_working_compiler(self):
        self.retain()
        before = self.tree()
        result = retain_runtime_notices(self.root, {"RUSTC": "/missing/rustc"})
        self.assertEqual(result["provenance"]["kind"], "retained-manifest")
        self.assertEqual(
            runtime_notices(self.root, {}), {"version": result["version"], "files": result["files"]}
        )
        self.assertEqual(before, self.tree())
        self.command.assert_not_called()

    def test_explicit_mismatching_source_cannot_silently_upgrade_existing_copy(self):
        self.retain()
        before = self.tree()
        (self.source / "version").write_text("1.99.0\n")
        with self.assertRaisesRegex(RuntimeError, "version differs"):
            self.retain()
        self.assertEqual(before, self.tree())

    def test_same_version_different_source_notice_hashes_preserve_existing_copy(self):
        self.retain()
        before = self.tree()
        (self.source / "COPYRIGHT-library.html").write_text(
            "different source bytes, same release stamp"
        )
        with self.assertRaisesRegex(RuntimeError, "no upgrade performed"):
            self.retain()
        self.assertEqual(before, self.tree())

    def test_retained_corruption_is_not_silently_repaired_from_available_source(self):
        self.retain()
        (self.destination / "LICENSE-MIT").write_text("changed retained original")
        with self.assertRaisesRegex(RuntimeError, "differs from retained provenance"):
            self.retain()
        self.assertEqual(
            (self.destination / "LICENSE-MIT").read_text(), "changed retained original"
        )

    def test_sibling_sdk_copy_is_rejected_even_with_valid_retention_provenance(self):
        self.retain()
        other = self.base / "another-sdk"
        other.mkdir()
        (other / "rust-toolchain.toml").write_text('[toolchain]\nchannel="1.99.1"\n')
        with self.assertRaisesRegex(RuntimeError, "not an upstream source"):
            retain_runtime_notices(other, self.env, source=self.destination)
        self.assertFalse((other / "ThirdParty").exists())

    def test_existing_unmanifested_equal_files_are_adopted_without_replacing_manual_material(self):
        self.destination.mkdir(parents=True)
        (self.destination / "LICENSE-MIT").write_bytes(self.files["LICENSE-MIT"])
        (self.destination / "README-original.txt").write_text("manual explanation")
        self.retain()
        self.assertEqual(
            (self.destination / "README-original.txt").read_text(), "manual explanation"
        )
        self.assertEqual((self.destination / "LICENSE-MIT").read_bytes(), self.files["LICENSE-MIT"])

    def test_existing_unmanifested_different_file_is_preserved(self):
        self.destination.mkdir(parents=True)
        (self.destination / "COPYRIGHT").write_text("manual original")
        with self.assertRaisesRegex(RuntimeError, "preserving originals"):
            self.retain()
        self.assertEqual((self.destination / "COPYRIGHT").read_text(), "manual original")
        self.assertFalse((self.destination / "provenance.json").exists())

    def test_pinned_installed_compiler_docs_and_component_manifest_are_retained(self):
        self.installed()
        result = retain_runtime_notices(self.root, self.env)
        self.assertEqual(result["provenance"]["kind"], "installed-compiler-docs")
        self.assertIn("sourceToolchainManifestSHA256", result["provenance"])
        self.assertEqual(
            result["provenance"]["sourceFiles"]["COPYRIGHT"], "share/doc/rust/COPYRIGHT"
        )
        self.assertNotIn(str(self.base), json.dumps(result))
        self.assertEqual(self.command.call_count, 2)

    def test_missing_installed_documentation_has_no_network_or_sibling_fallback(self):
        self.installed(missing="COPYRIGHT-library.html")
        with self.assertRaisesRegex(RuntimeError, "source is incomplete"):
            retain_runtime_notices(self.root, self.env)
        self.assertFalse(self.destination.exists())
        self.assertEqual(self.command.call_count, 2)

    def test_installed_version_mismatch_is_explicit(self):
        self.installed(version="1.99.0")
        with self.assertRaisesRegex(RuntimeError, "installed Rust compiler differs"):
            retain_runtime_notices(self.root, self.env)
        self.assertEqual(self.command.call_count, 1)
        self.assertFalse(self.destination.exists())

    def test_installed_component_version_mismatch_is_not_ignored(self):
        self.installed(component_version="1.99.0")
        with self.assertRaisesRegex(RuntimeError, "component manifest differs"):
            retain_runtime_notices(self.root, self.env)
        self.assertFalse(self.destination.exists())

    def test_manifest_relative_source_file_cannot_escape_its_public_tree(self):
        manifest = self.source_manifest()
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "LICENSE-MIT").write_bytes(self.files["LICENSE-MIT"])
        symlink_or_skip(self, self.source / "redirect", outside, target_is_directory=True)
        manifest["files"]["LICENSE-MIT"]["path"] = "redirect/LICENSE-MIT"
        (self.source / "provenance.json").write_text(json.dumps(manifest))
        with self.assertRaisesRegex(RuntimeError, "escapes its selected tree"):
            self.retain()
        self.assertFalse(self.destination.exists())

    def test_interrupt_after_runtime_notice_link_rolls_back_its_file(self):
        self.destination.mkdir(parents=True)
        (self.destination / "README-original.txt").write_text("manual original")
        before = self.tree()
        link = os.link

        def interrupt(source, target):
            link(source, target)
            raise KeyboardInterrupt()

        with patch("sdk_tooling.cargo.os.link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.retain()
        self.assertEqual(before, self.tree())
        self.retain()
        self.assertTrue((self.destination / "provenance.json").is_file())

    def test_interrupt_after_runtime_provenance_link_keeps_complete_tree(self):
        link = os.link

        def interrupt(source, target):
            link(source, target)
            if Path(target).name == "provenance.json":
                raise KeyboardInterrupt()

        with patch("sdk_tooling.cargo.os.link", side_effect=interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.retain()
        committed = self.tree()
        self.assertIn("provenance.json", committed)
        self.retain()
        self.assertEqual(committed, self.tree())

    def test_failed_manifest_publication_rolls_back_only_new_files(self):
        self.destination.mkdir(parents=True)
        (self.destination / "LICENSE-MIT").write_bytes(self.files["LICENSE-MIT"])
        (self.destination / "README-original.txt").write_text("manual original")
        before = self.tree()
        link = os.link

        def fail_manifest(source, target):
            if Path(target).name == "provenance.json":
                raise OSError("injected publish failure")
            return link(source, target)

        with patch("sdk_tooling.cargo.os.link", side_effect=fail_manifest):
            with self.assertRaises(OSError):
                self.retain()
        self.assertEqual(before, self.tree())


if __name__ == "__main__":
    unittest.main()
