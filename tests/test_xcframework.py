import json
import os
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from cargo_build_fixtures import FIXTURE_UNIT, fixture_dependencies

from sdk_tooling import ffi, windows, xcframework
from sdk_tooling.artifacts import files, hashes
from sdk_tooling.consumers import c_framework
from sdk_tooling.xcframework import DEP_INFO_PARSER, XcframeworkBuilder, dep_info_sources


class NativeInputsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        sources = {
            "Cargo.toml": '[workspace]\nmembers=["crates/core","crates/ffi"]\n',
            "Cargo.lock": "version = 4\n",
            "rust-toolchain.toml": '[toolchain]\nchannel="1.99.1"\n',
            "crates/ffi/Cargo.toml": '[package]\nname="fixture-ffi"\n[lib]\nname="fixture"\n',
            "crates/core/Cargo.toml": '[package]\nname="fixture"\n',
            "crates/ffi/module.modulemap": 'framework module CFixture { header "fixture.h" export * }\n',
            "crates/ffi/include/fixture.h": "#include <stdint.h>\n#define FIXTURE_ABI_VERSION 1u\nuint32_t fixture_abi_version(void);\n",
            "crates/ffi/src/lib.rs": "pub fn ffi() {}\n",
            "crates/core/src/lib.rs": "pub fn runtime() {}\n",
            "crates/core/src/tests.rs": "#[test] fn test_only() {}\n",
            "ThirdParty/fixture/LICENSE": "retained notice",
            "native.toml": "# SDK-owned native specification\n",
        }
        for path, content in sources.items():
            self.write(path, content)
        self.builder = XcframeworkBuilder(self.root, symbol_prefix="fixture_", minimum_macos="26.0")
        self.production = [
            self.root / name for name in ("crates/ffi/src/lib.rs", "crates/core/src/lib.rs")
        ]
        self.builder.receipt.parent.mkdir()
        self.builder.receipt.write_text(
            json.dumps(
                {
                    "productionSources": [str(p.relative_to(self.root)) for p in self.production],
                    "buildScriptInputRoots": [],
                    "buildScriptInputDirectories": [],
                }
            )
        )

    def write(self, relative, value):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def package(self, relative, script=None):
        return {
            "manifest_path": str(self.root / relative / "Cargo.toml"),
            "targets": (
                [{"kind": ["custom-build"], "src_path": str(self.root / relative / script)}]
                if script is not None
                else []
            ),
        }

    def accept(self, roots):
        inputs, directories = self.builder._input_snapshot(
            self.production, build_script_roots=roots
        )
        framework = self.builder.artifact / "macos-arm64_x86_64/CFixture.framework"
        (framework / "Resources").mkdir(parents=True, exist_ok=True)
        (framework / "Resources/Info.plist").write_bytes(
            plistlib.dumps({"MinimumOSVersion": "26.0"})
        )
        (framework / "CFixture").write_bytes(b"unexecuted fixture")
        receipt = dict(
            schema=3,
            configuration=self.builder.configuration,
            productionSources=[str(p.relative_to(self.root)) for p in self.production],
            buildScriptInputRoots=roots,
            buildScriptInputDirectories=directories,
            toolInputs=self.builder.tool_inputs(),
            inputs=inputs,
            outputs=hashes(files(self.builder.artifact), self.root),
            compiler="rustc 1.99.1 fixture",
            abiVersion=1,
            abiProbeArchitecture="arm64",
            dependencies=fixture_dependencies(*self.builder.targets),
            cargoBuild={target: [FIXTURE_UNIT] for target in self.builder.targets},
        )
        self.builder.receipt.write_text(json.dumps(receipt))
        return receipt

    def verify_without_tools(self, builder=None):
        with (
            patch("sdk_tooling.xcframework.CargoDependencies") as cargo,
            patch("sdk_tooling.xcframework.run") as run,
            patch("sdk_tooling.xcframework.subprocess.run") as subprocess_run,
        ):
            (builder or self.builder).verify(inspect_native=False)
        cargo.assert_not_called()
        run.assert_not_called()
        subprocess_run.assert_not_called()

    def production_sources(self, builder=None, paths=None):
        with patch(
            "sdk_tooling.xcframework.dep_info_sources",
            return_value=self.production if paths is None else paths,
        ):
            return (builder or self.builder)._production_sources("fixture parser digest")

    def test_test_only_rust_edit_does_not_invalidate_production(self):
        before = self.builder.inputs()
        self.write("crates/core/src/tests.rs", "#[test] fn a_different_test() {}\n")
        self.assertEqual(self.builder.inputs(), before)

    def test_unified_manifest_fingerprints_only_native_settings(self):
        first = (
            '[[native.artifacts]]\nid = "fixture"\n'
            'symbol_prefix = "fixture_"\nminimum_macos = "26.0"\n'
        )
        revised = (
            '[checks]\ncommands = ["test"]\n'
            '[[native.artifacts]]\nsymbol_prefix = "fixture_"\n'
            'id = "fixture"\nminimum_macos = "26.0"\n'
            '[distribution]\nplatforms = ["macos"]\n'
        )
        self.write("sdk-tooling.toml", first)
        self.builder = xcframework.xcframework(self.root)
        receipt = self.accept([])
        windows_inputs = self.builder.build_sources()
        self.assertEqual(self.builder.configuration["specification"], "sdk-tooling.toml")
        self.assertEqual(
            receipt["inputs"]["sdk-tooling.toml#native"], windows_inputs["sdk-tooling.toml#native"]
        )
        self.assertNotIn("sdk-tooling.toml", receipt["inputs"])
        self.assertNotIn("native.toml", windows_inputs)
        self.write("sdk-tooling.toml", revised)
        self.verify_without_tools()
        self.assertEqual(windows_inputs, self.builder.build_sources())

        self.write("sdk-tooling.toml", revised.replace('id = "fixture"', 'id = "moved"'))
        with self.assertRaisesRegex(ValueError, "Production source"):
            self.verify_without_tools()
        self.assertNotEqual(windows_inputs, self.builder.build_sources())

    def test_installed_c_accepts_the_same_manifest_inputs_as_the_producer(self):
        framework = (self.builder.artifact / "macos-arm64_x86_64/CFixture.framework").relative_to(
            self.root
        )

        def stage():
            with tempfile.TemporaryDirectory() as work:
                c_framework._stage_c(self.root, Path(work).resolve(), framework)

        self.accept([])
        stage()
        legacy = self.write("native.toml", "# Edited legacy manifest bytes\n")
        with self.assertRaisesRegex(ValueError, "differ from their receipt"):
            stage()

        specification = (
            '[[native.artifacts]]\nid = "fixture"\n'
            'symbol_prefix = "fixture_"\nminimum_macos = "26.0"\n'
        )
        unified = self.write("sdk-tooling.toml", specification)
        self.builder = xcframework.xcframework(self.root)
        self.accept([])
        stage()
        self.write("sdk-tooling.toml", "[checks]\ncommands = []\n" + specification)
        stage()
        self.write("sdk-tooling.toml", specification.replace('"26.0"', '"27.0"'))
        with self.assertRaisesRegex(ValueError, "differ from their receipt"):
            stage()
        unified.unlink()
        legacy.write_text(specification.replace("native.artifacts", "artifacts"))
        with self.assertRaisesRegex(ValueError, r"requires \[native\] in sdk-tooling.toml"):
            stage()

    def test_verify_platform_requires_the_framework_install_name_in_each_slice(self):
        library = self.root / "CFixture"

        def inspect(_xcrun, tool, *_arguments, env):
            return {"lipo": "x86_64 arm64", "vtool": "  platform MACOS\n     minos 26.0"}[tool]

        def install_name(command, *, env, text):
            return identifiers[command[3]]

        expected = "@rpath/CFixture.framework/Versions/A/CFixture\n"
        identifiers = dict.fromkeys(("arm64", "x86_64"), expected)
        with (
            patch("sdk_tooling.xcframework.run", side_effect=inspect),
            patch("sdk_tooling.xcframework.subprocess.check_output", side_effect=install_name),
        ):
            self.builder.verify_platform(library)
            for architecture in identifiers:
                with self.subTest(architecture=architecture):
                    identifiers[architecture] = expected.rstrip() + " \n"
                    with self.assertRaisesRegex(
                        ValueError, "install name differs on " + architecture
                    ):
                        self.builder.verify_platform(library)
                    identifiers[architecture] = expected

    def test_scoped_notices_and_specification_exclude_unrelated_host_material(self):
        self.write("ThirdParty/Product/LICENSE", "product-only notice")
        self.write("ThirdParty/Core/LICENSE", "core notice")
        self.write("tools/core-native.py", "# host-owned native specification\n")
        builder = XcframeworkBuilder(
            self.root,
            symbol_prefix="fixture_",
            minimum_macos="26.0",
            notice_directory="ThirdParty/Core",
            specification="tools/core-native.py",
        )
        before = builder.inputs(self.production, build_script_roots=[])
        self.assertIn("ThirdParty/Core/LICENSE", before)
        self.assertIn("tools/core-native.py", before)
        self.assertNotIn("ThirdParty/Product/LICENSE", before)
        self.assertNotIn(b"product-only notice", builder.notices())
        self.write("ThirdParty/Product/LICENSE", "changed product notice")
        self.assertEqual(builder.inputs(self.production, build_script_roots=[]), before)
        self.write("tools/core-native.py", "# changed build policy\n")
        self.assertNotEqual(builder.inputs(self.production, build_script_roots=[]), before)

    def test_cargo_version_can_be_declared_locally_or_inherited(self):
        self.write("crates/ffi/Cargo.toml", '[package]\nname="fixture-ffi"\nversion="0.2.0"\n')
        builder = XcframeworkBuilder(self.root, symbol_prefix="fixture_", minimum_macos="26.0")
        self.assertEqual(builder.version, "0.2.0")
        self.write(
            "crates/ffi/Cargo.toml", '[package]\nname="fixture-ffi"\nversion.workspace=true\n'
        )
        self.write("Cargo.toml", '[workspace.package]\nversion="0.3.0"\n')
        builder = XcframeworkBuilder(self.root, symbol_prefix="fixture_", minimum_macos="26.0")
        self.assertEqual(builder.version, "0.3.0")

    def test_scoped_rust_provenance_is_an_input_but_not_a_notice(self):
        provenance = "ThirdParty/Core/Rust/provenance.json"
        self.write(provenance, '{"version": "1.99.1"}\n')
        self.write("ThirdParty/Core/vendor/provenance.json", "upstream notice")
        license_bytes = b"Rust license\r\nOriginal notice\r\n"
        self.write("ThirdParty/Core/Rust/LICENSE-MIT", "").write_bytes(license_bytes)
        product = ffi.FfiProduct(
            self.root, symbol_prefix="fixture_", notice_directory="ThirdParty/Core"
        )

        notices = product.notices()
        self.assertNotIn(provenance.encode(), notices)
        self.assertIn(license_bytes, notices)
        self.assertIn(b"ThirdParty/Core/vendor/provenance.json =====\nupstream notice", notices)
        before = product.build_sources()
        self.assertIn(provenance, before)
        self.write(provenance, '{"version": "1.99.2"}\n')
        self.assertNotEqual(product.build_sources(), before)

    def test_runtime_edit_or_new_module_connection_invalidates(self):
        before = self.builder.inputs()
        self.write("crates/core/src/new.rs", "pub fn new_runtime() {}")
        self.assertEqual(self.builder.inputs(), before)
        self.write("crates/core/src/lib.rs", "mod new; pub use new::new_runtime;\n")
        self.assertNotEqual(self.builder.inputs(), before)

    def test_notices_include_hidden_material_but_ignore_finder_metadata(self):
        before = self.builder.inputs()
        self.write("ThirdParty/.DS_Store", "Finder metadata")
        self.assertEqual(self.builder.inputs(), before)
        self.write("ThirdParty/fixture/.hidden-notice", "upstream notice")
        self.assertNotEqual(self.builder.inputs(), before)
        self.assertIn("ThirdParty/fixture/.hidden-notice", self.builder.inputs())
        before = self.builder.inputs()
        (self.root / "ThirdParty/fixture/LICENSE").unlink()
        self.assertNotEqual(self.builder.inputs(), before)

    def test_receipt_source_cannot_escape_the_sdk(self):
        self.builder.receipt.write_text(
            json.dumps(
                {
                    "productionSources": ["../foreign.rs"],
                    "buildScriptInputRoots": [],
                    "buildScriptInputDirectories": [],
                }
            )
        )
        with self.assertRaisesRegex(ValueError, "escapes"):
            self.builder.inputs()

    def test_dependency_parser_rejects_a_different_snapshot_before_compiling(self):
        with (
            patch("sdk_tooling.xcframework.subprocess.run") as run,
            self.assertRaisesRegex(ValueError, "dependency parser differs from its build input"),
        ):
            list(dep_info_sources([], expected_sha256="0" * 64, cwd=self.root, env=os.environ))
        run.assert_not_called()

    def test_windows_receipt_depends_on_shared_tooling_but_not_xcframework_tools(self):
        tooling = self.root / "tooling"
        package = tooling / "sdk_tooling"
        package.mkdir(parents=True)
        expected_shared = {
            "artifacts.py",
            "c_compiler.py",
            "cargo.py",
            "cargo_build.py",
            "ffi.py",
            "manifest.py",
            "native_plan.py",
        }
        windows_sources = {"windows.py", "workspaces.py"}
        for name in expected_shared | windows_sources | {"xcframework.py"}:
            (package / name).write_text(name, encoding="utf-8")
        parser = tooling / DEP_INFO_PARSER
        parser.parent.mkdir(parents=True)
        parser.write_text("parser", encoding="utf-8")
        product = ffi.FfiProduct(self.root, symbol_prefix="fixture_")
        destination = self.root / ".native-build/windows/win-x64"
        destination.mkdir(parents=True)
        (destination / "fixture.dll").write_bytes(b"fixture DLL")

        with (
            patch.object(ffi, "__file__", str(package / "ffi.py")),
            patch.object(windows, "__file__", str(package / "windows.py")),
            patch.object(xcframework, "__file__", str(package / "xcframework.py")),
        ):
            windows_inputs = windows.tool_inputs()
            mac_inputs = self.builder.tool_inputs()
            self.assertEqual(set(windows_inputs), expected_shared | windows_sources)
            self.assertEqual(
                set(mac_inputs), expected_shared | {"xcframework.py", DEP_INFO_PARSER.as_posix()}
            )
            (destination / "native-build.json").write_text(
                json.dumps(
                    dict(
                        schema=2,
                        target="x86_64-pc-windows-msvc",
                        compiler="rustc " + product.compiler + " fixture",
                        inputs=product.build_sources(),
                        toolInputs=windows_inputs,
                        outputs=hashes([destination / "fixture.dll"], destination),
                        dependencies=fixture_dependencies("x86_64-pc-windows-msvc"),
                        cargoBuild={"x86_64-pc-windows-msvc": [FIXTURE_UNIT]},
                        abiVersions={"fixture_abi_version": 1},
                        abiProbeArchitecture="x64",
                    )
                ),
                encoding="utf-8",
            )
            self.assertEqual(windows.verify_artifact(product, "x64")[0], destination)
            (package / "xcframework.py").write_text("new framework tool", encoding="utf-8")
            parser.write_text("new parser", encoding="utf-8")
            self.assertNotEqual(self.builder.tool_inputs(), mac_inputs)
            self.assertEqual(windows.verify_artifact(product, "x64")[0], destination)
            mac_inputs = self.builder.tool_inputs()
            (package / "workspaces.py").write_text("new cleanup tool", encoding="utf-8")
            self.assertEqual(self.builder.tool_inputs(), mac_inputs)
            with self.assertRaisesRegex(ValueError, "sources or build contract"):
                windows.verify_artifact(product, "x64")
            (package / "workspaces.py").write_text("workspaces.py", encoding="utf-8")
            (package / "cargo.py").write_text("new shared build tool", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "sources or build contract"):
                windows.verify_artifact(product, "x64")

    @unittest.skipUnless(sys.platform == "darwin", "Native framework producers require macOS")
    def test_production_sources_filter_the_real_parser_batch(self):
        first, second = self.production
        generated = self.builder.target_directory / "generated.rs"
        upstream = self.root.parent / "upstream.rs"
        rules = ((first, generated), (second, first, upstream))
        for target, rule in zip(self.builder.targets, rules, strict=True):
            path = self.builder.target_directory / target / "release/libfixture.d"
            path.parent.mkdir(parents=True)
            words = " ".join(str(source).replace(" ", r"\ ") for source in rule)
            path.write_text(f"output: {words}\n")
        digest = self.builder.tool_inputs()[DEP_INFO_PARSER.as_posix()]
        self.assertEqual(
            self.builder._production_sources(digest, os.environ),
            sorted(self.production),
        )

    @unittest.skipUnless(sys.platform == "darwin", "Native framework producers require macOS")
    def test_dep_info_reports_a_missing_file_as_an_io_error(self):
        missing = self.root / "missing rule.d"
        digest = self.builder.tool_inputs()[DEP_INFO_PARSER.as_posix()]
        batch = dep_info_sources([missing], expected_sha256=digest, cwd=self.root, env=os.environ)
        with self.assertRaises(OSError) as failure:
            next(batch)
        self.assertIn(str(missing), str(failure.exception))

    def test_local_build_script_keeps_directory_inputs_conservative(self):
        self.write("crates/core/build.rs", "fn main() {}")
        extra = self.write("crates/core/data/input.txt", "build-time input")
        roots = self.builder._build_script_roots([self.package("crates/core", "build.rs")])
        selected = self.builder.inputs(self.production_sources(), build_script_roots=roots)
        self.assertIn(extra.relative_to(self.root).as_posix(), selected)

    def test_added_build_script_input_invalidates_accepted_sources(self):
        self.write("crates/core/build.rs", "fn main() {}")
        self.write("crates/core/data/input.txt", "build-time input")
        self.builder.receipt.write_text(
            json.dumps(
                {
                    "productionSources": [
                        str(p.relative_to(self.root)) for p in self.production_sources()
                    ],
                    "buildScriptInputRoots": ["crates/core"],
                    "buildScriptInputDirectories": [
                        "crates/core",
                        "crates/core/data",
                        "crates/core/src",
                    ],
                }
            )
        )
        before = self.builder.inputs()
        self.write("crates/core/data/added.txt", "new build-time input")
        self.assertNotEqual(self.builder.inputs(), before)

    def test_selected_script_tree_excludes_an_unrelated_workspace_script(self):
        self.write("crates/core/build.rs", "fn main() {}")
        self.write("crates/windows/Cargo.toml", '[package]\nname="unrelated"\n')
        self.write("crates/windows/build.rs", "fn main() {}")
        self.write("crates/windows/src/ui.rs", "fn old_ui() {}")
        roots = self.builder._build_script_roots(
            [self.package("crates/core", "build.rs"), self.package("crates/ffi")]
        )
        self.accept(roots)
        before = self.builder.build_sources()
        self.write("crates/windows/src/ui.rs", "fn changed_ui() {}")
        self.verify_without_tools()
        self.assertNotEqual(before, self.builder.build_sources())
        self.assertNotIn("crates/windows/src/ui.rs", self.builder.inputs())

    def test_accepted_script_input_addition_deletion_and_edits_fail_verification(self):
        self.write("crates/core/build.rs", "fn main() {}")
        original = self.write("crates/core/data/input.txt", "accepted input")
        self.accept(["crates/core"])
        self.verify_without_tools()
        for operation in ("add", "delete", "edit"):
            with self.subTest(operation=operation):
                if operation == "add":
                    added = self.write("crates/core/data/added.txt", "added input")
                elif operation == "delete":
                    original.unlink()
                else:
                    original.write_text("changed input")
                with self.assertRaisesRegex(ValueError, "Production source"):
                    self.verify_without_tools()
                if operation == "add":
                    added.unlink()
                else:
                    original.write_text("accepted input")
                self.verify_without_tools()

    def test_empty_directory_addition_and_deletion_fail_verification(self):
        self.write("crates/core/build.rs", "fn main() {}")
        empty = self.root / "crates/core/empty"
        empty.mkdir()
        self.accept(["crates/core"])
        before = self.builder.inputs()
        empty.rmdir()
        self.assertEqual(self.builder.inputs(), before)
        with self.assertRaisesRegex(ValueError, "Production source"):
            self.verify_without_tools()
        empty.mkdir()
        self.verify_without_tools()
        added = self.root / "crates/core/new-empty"
        added.mkdir()
        self.assertEqual(self.builder.inputs(), before)
        with self.assertRaisesRegex(ValueError, "Production source"):
            self.verify_without_tools()

    def test_nested_custom_script_keeps_the_package_data_in_its_inventory(self):
        self.write("crates/core/scripts/generate.rs", "fn main() {}")
        self.write("crates/core/data/input.txt", "package input outside scripts")
        roots = self.builder._build_script_roots(
            [self.package("crates/core", "scripts/generate.rs")]
        )
        self.accept(roots)
        self.assertIn("crates/core/data/input.txt", self.builder.inputs())
        self.write("crates/core/data/added.txt", "new package input")
        with self.assertRaisesRegex(ValueError, "Production source"):
            self.verify_without_tools()

    def test_disabled_script_has_no_selected_tree(self):
        self.write("crates/core/build.rs", "disabled by the package manifest")
        roots = self.builder._build_script_roots([self.package("crates/core")])
        self.accept(roots)
        self.write("crates/core/data/unrelated.txt", "not used by a script")
        self.verify_without_tools()
        self.assertEqual(roots, [])

    def test_preexisting_dep_info_keeps_the_selected_script_inventory(self):
        self.write("crates/core/build.rs", "fn main() {}")
        descriptors = [self.package("crates/core", "build.rs")]
        roots = self.builder._build_script_roots(descriptors)
        self.accept(roots)
        reused = XcframeworkBuilder(self.root, symbol_prefix="fixture_", minimum_macos="26.0")
        self.assertEqual(self.production_sources(reused), sorted(self.production))
        self.assertEqual(reused._build_script_roots(descriptors), roots)
        self.verify_without_tools(reused)
        self.write("crates/core/new-input.txt", "new input after a cached build")
        with self.assertRaisesRegex(ValueError, "Production source"):
            self.verify_without_tools(reused)

    def test_directory_added_during_a_cached_build_prevents_publication(self):
        self.write("crates/core/build.rs", "fn main() {}")
        artifacts_before = hashes(files(self.root / "Artifacts"), self.root)
        added = self.root / "crates/core/new-directory"

        def cached_build(*args, **kwargs):
            added.mkdir(exist_ok=True)

        with (
            patch("sdk_tooling.xcframework.sys.platform", "darwin"),
            patch("sdk_tooling.xcframework.run", return_value="rustc 1.99.1 fixture"),
            patch.object(self.builder, "retain_notices"),
            patch("sdk_tooling.xcframework.CargoDependencies") as dependencies,
            patch("sdk_tooling.xcframework.compile_units", side_effect=cached_build) as cargo,
            patch("sdk_tooling.xcframework.dep_info_sources", return_value=self.production),
            patch("sdk_tooling.xcframework.publish") as publish,
        ):
            graph = dependencies.return_value
            graph.provenance.return_value = {}
            graph.workspace_packages = [self.package("crates/core", "build.rs")]
            graph.configure_remaps.side_effect = lambda env: env.update(CARGO_ENCODED_RUSTFLAGS="")
            with self.assertRaisesRegex(ValueError, "directories changed during build"):
                self.builder.build()
        self.assertEqual(cargo.call_count, len(self.builder.targets))
        publish.assert_not_called()
        self.assertEqual(artifacts_before, hashes(files(self.root / "Artifacts"), self.root))

    def test_parser_source_change_after_inspection_prevents_publication(self):
        self.accept([])
        accepted = hashes(files(self.root / "Artifacts"), self.root)
        before = self.builder.tool_inputs()
        parser = DEP_INFO_PARSER.as_posix()
        self.assertIn(parser, before)
        after = before | {parser: "replacement"}
        with (
            patch("sdk_tooling.xcframework.sys.platform", "darwin"),
            patch("sdk_tooling.xcframework.run", return_value="rustc 1.99.1 fixture"),
            patch.object(self.builder, "retain_notices"),
            patch.object(self.builder, "tool_inputs", side_effect=[before, after]),
            patch("sdk_tooling.xcframework.CargoDependencies") as dependencies,
            patch("sdk_tooling.xcframework.dep_info_sources", return_value=self.production),
            patch("sdk_tooling.xcframework.compile_units"),
            patch("sdk_tooling.xcframework.publish") as publish,
        ):
            graph = dependencies.return_value
            graph.provenance.return_value = {}
            graph.workspace_packages = []
            graph.configure_remaps.side_effect = lambda env: env.update(CARGO_ENCODED_RUSTFLAGS="")
            with self.assertRaisesRegex(ValueError, "tooling changed during build"):
                self.builder.build()
        publish.assert_not_called()
        self.assertEqual(accepted, hashes(files(self.root / "Artifacts"), self.root))

    def test_unsafe_or_output_overlapping_input_roots_are_rejected(self):
        for name in (".git", "Artifacts", "target", ".native-build", "crates/core/output"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        for relative in (
            ".",
            "..",
            "../foreign",
            str(self.root),
            "crates/../core",
            "crates//core",
            ".git",
            "Artifacts",
            "target",
            ".native-build",
            "target/native/child",
        ):
            with self.subTest(root=relative), self.assertRaises(ValueError):
                self.builder.inputs(self.production, build_script_roots=[relative])
        builder = XcframeworkBuilder(
            self.root,
            symbol_prefix="fixture_",
            minimum_macos="26.0",
            target_directory="crates/core/output",
        )
        with self.assertRaisesRegex(ValueError, "overlaps"):
            builder.inputs(self.production, build_script_roots=["crates/core"])

    def test_custom_script_cannot_escape_its_package_input_root(self):
        self.write("crates/shared/build.rs", "fn main() {}")
        for script in ("../shared/build.rs", str(self.root / "crates/shared/build.rs")):
            with (
                self.subTest(script=script),
                self.assertRaisesRegex(ValueError, "package input root"),
            ):
                self.builder._build_script_roots([self.package("crates/core", script)])

    def test_case_aliases_of_existing_output_and_metadata_roots_are_rejected(self):
        target = self.root / "target"
        target.mkdir()
        alias = self.root / "TARGET"
        if not alias.exists() or not alias.samefile(target):
            self.skipTest("Requires a case-insensitive source filesystem")
        for name, alias_name in (
            ("target", "TARGET"),
            (".git", ".GIT"),
            ("Artifacts", "aRTIFACTS"),
            (".native-build", ".NATIVE-BUILD"),
        ):
            (self.root / name / "nested").mkdir(parents=True, exist_ok=True)
            for relative in (alias_name, alias_name + "/nested"):
                with self.subTest(root=relative), self.assertRaisesRegex(ValueError, "overlaps"):
                    self.builder.inputs(self.production, build_script_roots=[relative])

    def test_case_alias_of_parent_of_uncreated_output_is_rejected(self):
        source = self.root / "crates/core"
        alias = self.root / "CRATES/CORE"
        if not alias.exists() or not alias.samefile(source):
            self.skipTest("Requires a case-insensitive source filesystem")
        for relative, output in (
            ("crates/core", "CRATES/CORE/uncreated/native"),
            ("CRATES/CORE", "crates/core/uncreated/native"),
        ):
            with self.subTest(root=relative, output=output):
                builder = XcframeworkBuilder(
                    self.root,
                    symbol_prefix="fixture_",
                    minimum_macos="26.0",
                    target_directory=output,
                )
                self.assertFalse(builder.target_directory.exists())
                with self.assertRaisesRegex(ValueError, "overlaps"):
                    builder.inputs(self.production, build_script_roots=[relative])

    def test_distinct_case_variants_on_case_sensitive_filesystem_remain_valid(self):
        (self.root / "target").mkdir()
        distinct = self.root / "TARGET"
        if distinct.exists():
            self.skipTest("Requires a case-sensitive source filesystem")
        distinct.mkdir()
        self.write("TARGET/input.txt", "distinct source directory")
        selected = self.builder.inputs(self.production, build_script_roots=["TARGET"])
        self.assertIn("TARGET/input.txt", selected)

    def test_selected_tree_rejects_file_and_directory_symlinks(self):
        self.write("crates/core/build.rs", "fn main() {}")
        self.accept(["crates/core"])
        outside = Path(self.temporary.name).parent / (self.root.name + "-outside")
        outside.mkdir()
        self.addCleanup(lambda: outside.rmdir())
        target = outside / "input.txt"
        target.write_text("outside input")
        self.addCleanup(lambda: target.unlink())
        for name, destination, directory in (
            ("alias.txt", target, False),
            ("alias", outside, True),
        ):
            link = self.root / "crates/core" / name
            try:
                link.symlink_to(destination, target_is_directory=directory)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows symlink privilege unavailable")
                raise
            with self.subTest(path=name), self.assertRaisesRegex(ValueError, "symlink"):
                self.verify_without_tools()
            link.unlink()

    def test_receipt_without_directory_inventory_requires_rebuild(self):
        receipt = self.accept([])
        for field in ("buildScriptInputRoots", "buildScriptInputDirectories"):
            incomplete = dict(receipt)
            incomplete.pop(field)
            self.builder.receipt.write_text(json.dumps(incomplete))
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "inventory"):
                self.verify_without_tools()
        receipt["schema"] = 2
        self.builder.receipt.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "contract changed"):
            self.verify_without_tools()

    def test_missing_ffi_in_dep_info_is_an_error(self):
        with self.assertRaisesRegex(ValueError, "omits the FFI"):
            self.production_sources(paths=self.production[1:])

    def test_build_configuration_is_derived_from_cargo_and_module_map(self):
        self.assertEqual(self.builder.configuration["library"], "fixture")
        self.assertEqual(self.builder.configuration["module"], "CFixture")
        self.assertEqual(self.builder.public_symbols(), {"_fixture_abi_version"})

    def test_recorded_abi_must_match_the_exported_header_contract(self):
        receipt = self.accept([])
        receipt["abiVersion"] = 2
        self.builder.receipt.write_text(json.dumps(receipt))
        with self.assertRaisesRegex(ValueError, "ABI receipt"):
            self.verify_without_tools()

    def test_windows_abi_checks_the_optional_extension_with_the_portable_version(self):
        self.write(
            "crates/ffi/include/windows/fixture.h",
            "#define FIXTURE_WINDOWS_ABI_VERSION 3U\nuint32_t fixture_windows_abi_version(void);\n",
        )
        self.assertEqual(self.builder.abi_versions(), {"fixture_abi_version": 1})
        self.assertEqual(
            self.builder.abi_versions(windows=True),
            {"fixture_abi_version": 1, "fixture_windows_abi_version": 3},
        )

    @unittest.skipUnless(sys.platform == "darwin", "Requires native Clang and a dylib loader")
    def test_native_abi_probe_rejects_the_wrong_runtime_version(self):
        library = self.root / "CFixture.framework/CFixture"
        library.parent.mkdir()
        source = self.write(
            "fixture.c", "#include <stdint.h>\nuint32_t fixture_abi_version(void) { return 1; }\n"
        )
        subprocess.run(
            [
                "xcrun",
                "clang",
                "-dynamiclib",
                "-install_name",
                "@rpath/CFixture.framework/CFixture",
                str(source),
                "-o",
                str(library),
            ],
            check=True,
        )
        self.builder.probe_abi(library, os.environ)
        self.write(
            "crates/ffi/include/fixture.h",
            "#include <stdint.h>\n#define FIXTURE_ABI_VERSION 2U\nuint32_t fixture_abi_version(void);\n",
        )
        with self.assertRaisesRegex(ValueError, "Native ABI runtime probe failed"):
            self.builder.probe_abi(library, os.environ)

    def test_macos_contract_uses_module_headers_without_windows_only_apis(self):
        before = self.builder.inputs()
        header = self.write(
            "crates/ffi/include/windows/fixture.h", "void fixture_windows_start(void);\n"
        )
        self.assertEqual(self.builder.public_symbols(), {"_fixture_abi_version"})
        self.assertEqual(self.builder.inputs(), before)
        self.assertIn(header.relative_to(self.root).as_posix(), self.builder.build_sources())

    def test_module_map_cannot_read_a_header_outside_the_include_directory(self):
        self.write(
            "crates/ffi/module.modulemap", 'framework module CFixture { header "../Cargo.toml" }'
        )
        with self.assertRaisesRegex(ValueError, "inside the SDK include"):
            self.builder.public_symbols()

    def test_verification_rejects_changed_production_before_native_effects(self):
        receipt = dict(
            schema=3,
            configuration=self.builder.configuration,
            productionSources=[str(p.relative_to(self.root)) for p in self.production],
            buildScriptInputRoots=[],
            buildScriptInputDirectories=[],
            toolInputs=self.builder.tool_inputs(),
            inputs=self.builder.inputs(),
            outputs={},
            compiler="rustc 1.99.1 fixture",
        )
        self.builder.receipt.write_text(json.dumps(receipt))
        self.write("crates/core/src/lib.rs", "pub fn changed_runtime() {}")
        with (
            patch("sdk_tooling.xcframework.subprocess.run") as run,
            self.assertRaisesRegex(ValueError, "Production source"),
        ):
            self.builder.verify()
        run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
