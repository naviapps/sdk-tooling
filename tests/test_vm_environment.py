import unittest
from pathlib import Path

from sdk_tooling import vm_environment as vm


class VMEnvironmentTests(unittest.TestCase):
    def test_runner_variables_accept_canonical_and_legacy_names(self):
        self.assertEqual(vm.Variable.ARTIFACTS_DIR.legacy_name, "NAVI_CI_ARTIFACTS")
        self.assertEqual(vm.Variable.SOURCE_MANIFEST.legacy_name, "NAVI_CI_SOURCES")
        for variable in vm.Variable:
            with self.subTest(variable=variable):
                legacy = {variable.legacy_name: "selected"}
                canonical = {str(variable): "selected"}
                self.assertEqual(vm.value(variable, legacy), "selected")
                self.assertEqual(vm.value(variable, canonical), "selected")
                self.assertEqual(vm.value(variable, canonical | legacy), "selected")
                with self.assertRaisesRegex(ValueError, f"Conflicting {variable}") as error:
                    vm.value(variable, canonical | {variable.legacy_name: "other"})
                self.assertNotIn("other", str(error.exception))
                self.assertNotIn("selected", str(error.exception))

    def test_child_environment_replaces_or_removes_both_aliases(self):
        variable = vm.Variable.ARTIFACTS_DIR
        env = {str(variable): "new", variable.legacy_name: "old", "OTHER": "retained"}
        path = Path.cwd() / "results"
        vm.set_value(env, variable, path)
        self.assertEqual(env, {str(variable): str(path), "OTHER": "retained"})
        self.assertEqual(vm.artifacts_dir(env), path)

        env[variable.legacy_name] = str(path)
        vm.set_value(env, variable, None)
        self.assertEqual(env, {"OTHER": "retained"})

    def test_paths_are_absolute(self):
        for read, variable in (
            (vm.artifacts_dir, vm.Variable.ARTIFACTS_DIR),
            (vm.source_manifest, vm.Variable.SOURCE_MANIFEST),
        ):
            with self.subTest(variable=variable):
                path = Path.cwd() / "selected"
                self.assertEqual(read({variable.legacy_name: str(path)}), path)
                self.assertIsNone(read({str(variable): ""}))
                with self.assertRaisesRegex(ValueError, "absolute path"):
                    read({str(variable): "relative"})

    def test_captured_source_fields_require_their_matching_digest(self):
        archive = {"VM_RUNNER_SOURCE_COMMIT": "a" * 40}
        self.assertEqual(vm.source_identity(archive, require_fingerprint=False), ("a" * 40, None))
        with self.assertRaisesRegex(ValueError, "workspace SHA-256"):
            vm.source_identity(archive)
        path = Path.cwd() / ".navi-ci-source-files.json"
        with self.assertRaisesRegex(ValueError, "manifest path and SHA-256"):
            vm.source_files({"VM_RUNNER_SOURCE_FILES": str(path)})

        env = {
            **archive,
            "NAVI_CI_SOURCE_FINGERPRINT": "b" * 64,
            "VM_RUNNER_SOURCE_FILES": str(path),
            "NAVI_CI_SOURCE_FILES_SHA256": "c" * 64,
        }
        self.assertEqual(vm.source_identity(env), ("a" * 40, "b" * 64))
        self.assertEqual(vm.source_files(env), (path, "c" * 64))
