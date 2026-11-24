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
            b'[[artifacts]]\nid = "alpha"\nsymbol_prefix = "alpha_"\n'
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
