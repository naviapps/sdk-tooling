"""Check reproducible C declarations and module maps using controlled generator output."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import c_headers
from sdk_tooling.__main__ import main


class CHeaderTests(unittest.TestCase):
    def setUp(self):
        directory = self.enterContext(tempfile.TemporaryDirectory(prefix="C headers "))
        self.root = Path(directory).resolve()
        self.enterContext(patch.object(Path, "cwd", return_value=self.root))
        self.tool = self.enterContext(
            patch.object(c_headers.quality, "tool", return_value="pinned-cbindgen")
        )
        self.generator = self.enterContext(patch.object(c_headers.subprocess, "check_output"))
        self.write(
            "native.toml",
            b'[[artifacts]]\nid = "alpha"\nsymbol_prefix = "alpha_"\nminimum_macos = "26.0"\n'
            b'[[artifacts]]\nid = "beta"\nsymbol_prefix = "beta_"\nffi = "crates/beta"\n',
        )
        for directory, module in (("crates/ffi", "CAlpha"), ("crates/beta", "CBeta")):
            umbrella = "umbrella = false\n" if module == "CBeta" else ""
            self.write(
                f"{directory}/Cargo.toml",
                (
                    f'[package]\nname = "{module.lower()}"\n'
                    f'[package.metadata.c-bindings]\nmodule = "{module}"\n{umbrella}'
                ).encode("utf-8"),
            )
            self.write(f"{directory}/cbindgen.toml", b'language = "C"\n')
            self.write(f"{directory}/src/lib.rs", b"")
        self.write("crates/ffi/cbindgen-windows.toml", b'language = "C"\n')
        self.write("crates/ffi/src/windows.rs", b"")

    def write(self, relative, contents):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
        return path

    def test_generates_portable_header_windows_header_and_module_map_for_selected_product(self):
        self.generator.side_effect = [
            b"void alpha_run(void);\n",
            b"void alpha_windows_run(void);\r\n",
        ]
        self.assertEqual(main(["c-headers", "generate", "--product", "alpha"]), 0)
        self.tool.assert_called_once_with(self.root, "cbindgen", "cargo:cbindgen")
        self.assertEqual(
            (self.root / "crates/ffi/include/alpha.h").read_bytes(),
            b"void alpha_run(void);\n",
        )
        self.assertEqual(
            (self.root / "crates/ffi/include/windows/alpha_windows.h").read_bytes(),
            b"void alpha_windows_run(void);\n",
        )
        self.assertEqual(
            (self.root / "crates/ffi/module.modulemap").read_bytes(),
            b'framework module CAlpha {\n  umbrella header "alpha.h"\n  export *\n}\n',
        )
        self.assertFalse((self.root / "crates/beta/include").exists())
        self.assertFalse((self.root / "crates/beta/module.modulemap").exists())

    def test_check_reports_stale_or_missing_output_without_writing(self):
        self.generator.return_value = b"void beta_run(void);\n"
        header = self.write("crates/beta/include/beta.h", b"stale header\n")
        module_map = self.root / "crates/beta/module.modulemap"

        with self.assertRaises(ValueError) as error:
            main(["c-headers", "check", "--product", "beta"])

        message = str(error.exception)
        self.assertIn("crates/beta/include/beta.h", message)
        self.assertIn("crates/beta/module.modulemap", message)
        self.assertIn("c-headers generate", message)
        self.assertEqual(header.read_bytes(), b"stale header\n")
        self.assertFalse(module_map.exists())
        self.assertEqual(main(["c-headers", "generate", "--product", "beta"]), 0)
        self.assertEqual(
            module_map.read_bytes(),
            b'framework module CBeta {\n  header "beta.h"\n  export *\n}\n',
        )
        self.assertEqual(main(["c-headers", "check", "--product", "beta"]), 0)

    def test_generated_mode_stages_headers_and_checks_the_installed_framework(self):
        crate = self.root / "crates/ffi"
        manifest = crate / "Cargo.toml"
        manifest.write_bytes(manifest.read_bytes() + b"generated = true\n")
        portable_header = b"void alpha_run(void);\n"
        windows_header = b"void alpha_windows_run(void);\n"
        module_map = b'framework module CAlpha {\n  umbrella header "alpha.h"\n  export *\n}\n'

        def invoke(command, **_):
            if Path(command[-1]).name == "windows.rs":
                return windows_header
            return portable_header

        self.generator.side_effect = invoke
        self.assertTrue(c_headers.has_generated_products(self.root))
        c_headers.prepare_generated(self.root)
        self.assertFalse((self.root / "crates/beta/include").exists())
        staged = crate
        self.assertEqual((staged / "include/alpha.h").read_bytes(), portable_header)
        self.assertEqual((staged / "include/windows/alpha_windows.h").read_bytes(), windows_header)
        self.assertEqual((staged / "module.modulemap").read_bytes(), module_map)

        framework = "Artifacts/CAlpha.xcframework/macos-arm64_x86_64/CAlpha.framework/Versions/A"
        header_path = self.write(f"{framework}/Headers/alpha.h", portable_header)
        module_path = self.write(f"{framework}/Modules/module.modulemap", module_map)
        (staged / "include/alpha.h").write_bytes(b"stale staged header\n")
        self.assertEqual(main(["c-headers", "check", "--product", "alpha"]), 0)
        self.assertEqual((staged / "include/alpha.h").read_bytes(), portable_header)

        header_path.write_bytes(b"stale header\n")
        module_path.write_bytes(b"stale module map\n")
        (staged / "include/alpha.h").unlink()
        with self.assertRaises(ValueError) as error:
            main(["c-headers", "check", "--product", "alpha"])

        self.assertIn("Headers/alpha.h", str(error.exception))
        self.assertIn("Modules/module.modulemap", str(error.exception))
        self.assertEqual(header_path.read_bytes(), b"stale header\n")
        self.assertEqual(module_path.read_bytes(), b"stale module map\n")
        self.assertFalse((crate / "include/alpha.h").exists())
        self.assertEqual((crate / "module.modulemap").read_bytes(), module_map)

    def test_automatic_generation_skips_legacy_native_products(self):
        self.assertFalse(c_headers.has_generated_products(self.root))
        c_headers.prepare_generated(self.root)
        c_headers.prepare_generated(self.root, verify=True)
        self.tool.assert_not_called()
        self.generator.assert_not_called()

    def test_xcframework_verify_compares_and_stages_generated_headers_once(self):
        crate = self.root / "crates/ffi"
        manifest = crate / "Cargo.toml"
        manifest.write_bytes(manifest.read_bytes() + b"generated = true\n")
        portable = b"void alpha_run(void);\n"
        windows = b"void alpha_windows_run(void);\n"
        self.generator.side_effect = [portable, windows, portable, windows]
        framework = "Artifacts/CAlpha.xcframework/macos-arm64_x86_64/CAlpha.framework/Versions/A"
        installed = self.write(f"{framework}/Headers/alpha.h", b"stale header\n")
        self.write(
            f"{framework}/Modules/module.modulemap",
            b'framework module CAlpha {\n  umbrella header "alpha.h"\n  export *\n}\n',
        )
        command = ["xcframework", "--verify", "--product", "alpha"]

        with patch("sdk_tooling.xcframework.xcframework") as builder:
            with self.assertRaisesRegex(ValueError, "Headers/alpha.h"):
                main(command)
            builder.assert_not_called()
            self.assertFalse((crate / "include").exists())
            self.assertEqual(self.generator.call_count, 2)

            installed.write_bytes(portable)
            self.assertEqual(main(command), 0)
            builder.assert_called_once_with(self.root, "alpha")
            builder.return_value.verify.assert_called_once_with()

        self.assertEqual(self.generator.call_count, 4)
        self.assertEqual((crate / "include/alpha.h").read_bytes(), portable)
        self.assertEqual((crate / "include/windows/alpha_windows.h").read_bytes(), windows)
        self.assertEqual(installed.read_bytes(), portable)

    def test_late_generator_failure_preserves_all_existing_outputs(self):
        header = self.write("crates/ffi/include/alpha.h", b"old header\n")
        module_map = self.write("crates/ffi/module.modulemap", b"old module map\n")
        self.generator.side_effect = [
            b"void alpha_run(void);\n",
            b"void alpha_windows_run(void);\n",
            subprocess.CalledProcessError(1, ["pinned-cbindgen"]),
        ]

        with self.assertRaises(RuntimeError) as error:
            main(["c-headers", "generate"])

        self.assertIsInstance(error.exception.__cause__, subprocess.CalledProcessError)
        self.assertEqual(header.read_bytes(), b"old header\n")
        self.assertEqual(module_map.read_bytes(), b"old module map\n")
        self.assertFalse((self.root / "crates/ffi/include/windows/alpha_windows.h").exists())
        self.assertFalse((self.root / "crates/beta/include").exists())

    def test_nondirectory_output_parent_preserves_all_existing_outputs(self):
        header = self.write("crates/ffi/include/alpha.h", b"old header\n")
        module_map = self.write("crates/ffi/module.modulemap", b"old module map\n")
        obstruction = self.write("crates/ffi/include/windows", b"not a directory\n")
        self.generator.return_value = b"void alpha_run(void);\n"

        with self.assertRaises(ValueError):
            main(["c-headers", "generate", "--product", "alpha"])

        self.assertEqual(header.read_bytes(), b"old header\n")
        self.assertEqual(module_map.read_bytes(), b"old module map\n")
        self.assertEqual(obstruction.read_bytes(), b"not a directory\n")

    def test_dependency_parsing_uses_locked_workspace_metadata(self):
        config = self.write(
            "crates/beta/cbindgen.toml", b'language = "C"\n[parse]\nparse_deps = true\n'
        )
        metadata = b'{"version": 1, "packages": []}\n'
        metadata_files = []

        def invoke(command, *, cwd):
            self.assertEqual(cwd, self.root)
            command = list(map(str, command))
            if command[0] == "cargo":
                self.assertEqual(
                    command,
                    ["cargo", "metadata", "--locked", "--all-features", "--format-version", "1"],
                )
                return metadata
            self.assertEqual(
                command[:-1],
                ["pinned-cbindgen", "--config", str(config), "--crate", "cbeta", "--metadata"],
            )
            path = Path(command[-1])
            self.assertEqual(path.read_bytes(), metadata)
            metadata_files.append(path)
            return b"void beta_run(void);\n"

        self.generator.side_effect = invoke

        self.assertEqual(main(["c-headers", "generate", "--product", "beta"]), 0)

        self.assertEqual(self.generator.call_count, 2)
        self.assertEqual(len(metadata_files), 1)
        self.assertFalse(metadata_files[0].exists())
