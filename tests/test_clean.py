import os
import subprocess
import tempfile
import unittest
from argparse import Namespace
from pathlib import Path

from sdk_tooling.artifacts import build_lock
from sdk_tooling.clean import clean


class CleanTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.root = self.workspace / "SDK checkout"
        self.root.mkdir()
        self.git("init", "--quiet")
        self.file(".gitignore", b"/build/\n/.native-build/\n/target/\n/.build/\n")
        self.artifact = self.file("Artifacts/framework", b"distribution binary")
        self.receipt = self.file("Artifacts/native-build.json", b"distribution receipt")
        self.git("add", "--all")
        self.git("commit", "--quiet", "-m", "Fixture")

    def git(self, *args):
        subprocess.run(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "user.name=Clean Fixture",
                "-c",
                "user.email=clean@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *args,
            ],
            cwd=self.root,
            check=True,
            capture_output=True,
        )

    def file(self, relative, data=b"generated"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def run_clean(self, *paths):
        clean(Namespace(paths=paths), root=self.root)

    def test_removes_declared_builds_and_leaves_distribution_and_other_paths(self):
        for name in ("target/debug/tool", ".build/package", "build/bindings/package"):
            self.file(name)
        other = self.file("build/other/result")

        for _ in range(2):
            self.run_clean("target", ".build", "build/bindings", ".native-build")
        for name in ("target", ".build", "build/bindings", ".native-build"):
            self.assertFalse((self.root / name).exists(), name)
        self.assertEqual(self.artifact.read_bytes(), b"distribution binary")
        self.assertEqual(self.receipt.read_bytes(), b"distribution receipt")
        self.assertTrue(other.is_file())

    def test_refuses_committed_artifacts_even_after_staged_removal(self):
        first = self.file(".build/debug/item")
        self.file(".gitignore", b"/.build/\n/Artifacts/\n")
        self.git("rm", "--cached", "--", "Artifacts/framework", "Artifacts/native-build.json")
        with self.assertRaisesRegex(ValueError, "tracked"):
            self.run_clean(".build", "Artifacts")
        self.assertTrue(first.is_file())
        self.assertTrue(self.artifact.is_file())
        self.assertTrue(self.receipt.is_file())

    def test_refuses_unignored_paths_local_ignores_and_files(self):
        local_rule = self.root / ".git/info/exclude"
        with local_rule.open("a", encoding="utf-8") as exclude:
            exclude.write("\n/.private/\n")
        secret = self.file(".private/data")
        for name in (".private", "unignored", ".git", "../outside", str(self.workspace)):
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.run_clean(name)
        self.assertTrue(secret.is_file())
        unexpected = self.file("target")
        with self.assertRaisesRegex(ValueError, "unexpected file"):
            self.run_clean("target")
        self.assertTrue(unexpected.is_file())

    def test_refuses_symlink_ancestors(self):
        external = self.workspace / "outside"
        external.mkdir()
        outside_build = external / "bindings"
        outside_build.mkdir()
        marker = outside_build / "marker"
        marker.write_bytes(b"outside")
        try:
            (self.root / "build").symlink_to(external, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows runners can deny symlink creation")
            raise
        with self.assertRaisesRegex(ValueError, "inside the checkout"):
            self.run_clean("build/bindings")
        self.assertEqual(marker.read_bytes(), b"outside")

    def test_takes_all_locks_before_deleting_and_retains_rollback_and_locks(self):
        target = self.file("target/debug/item")
        native_output = self.file(".native-build/packages/package")
        windows_output = self.file("build/windows/win-x64/temp")
        rollback = self.file("build/windows/win-x64/.package.rollback-1/previous.nupkg")
        native_directory = self.root / ".native-build"
        windows_directory = self.root / "build/windows"
        with build_lock(native_directory):
            pass
        with build_lock(windows_directory):
            with self.assertRaisesRegex(RuntimeError, "Another native build owns"):
                self.run_clean("target", ".native-build", "build")
            self.assertTrue(target.is_file())
            self.assertTrue(native_output.is_file())
            self.assertTrue(windows_output.is_file())

        with self.assertRaisesRegex(ValueError, "checkout-relative"):
            self.run_clean("build/windows/win-x64/.package.rollback-1")
        self.run_clean("target", ".native-build", "build")
        for path in (target, native_output, windows_output):
            self.assertFalse(path.exists())
        for path in (native_directory / "native.lock", windows_directory / "native.lock", rollback):
            self.assertTrue(path.is_file())
