import errno
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import artifacts
from sdk_tooling.artifacts import build_lock, dep_info_sources, hashes, publish, write_json


class ArtifactTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def file(self, relative, content=b"owned bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def symlink(self, link, target):
        try:
            link.symlink_to(Path(target))
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest(
                    "Windows denied symlink creation (WinError 1314); no privilege changes attempted"
                )
            raise

    def test_symlink_fixture_skips_only_windows_privilege_denial(self):
        error = OSError("symlink privilege unavailable")
        error.winerror = 1314
        with (
            patch.object(Path, "symlink_to", side_effect=error),
            self.assertRaises(unittest.SkipTest),
        ):
            self.symlink(self.root / "link", "owned-target")

    def test_symlink_fixture_does_not_hide_other_setup_errors(self):
        for winerror in (None, 5):
            error = PermissionError("unrelated setup error")
            if winerror is not None:
                error.winerror = winerror
            with (
                self.subTest(winerror=winerror),
                patch.object(Path, "symlink_to", side_effect=error),
            ):
                with self.assertRaises(PermissionError) as raised:
                    self.symlink(self.root / "link", "owned-target")
                self.assertIs(raised.exception, error)

    def test_native_staging_reads_framework_symlink_without_modifying_source(self):
        native = self.file("Native.framework/Versions/A/Native", b"accepted native")
        link = self.root / "Native.framework/Native"
        self.symlink(link, "Versions/A/Native")
        before = native.read_bytes()
        destination = self.root / "runtime/native.dll"
        self.assertEqual(artifacts.stage_native(link, destination), destination)
        self.assertEqual(destination.read_bytes(), before)
        self.assertEqual(native.read_bytes(), before)
        self.assertTrue(link.is_symlink())
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_native_staging_rejects_same_file_and_destination_symlink(self):
        native = self.file("native.dll")
        link = self.root / "link.dll"
        self.symlink(link, native)
        for destination in (native, link):
            with self.subTest(destination=destination), self.assertRaises(ValueError):
                artifacts.stage_native(native, destination)
        self.assertEqual(native.read_bytes(), b"owned bytes")

    def test_bad_native_copy_preserves_previous_staging_and_cleans_temporary(self):
        native = self.file("accepted.dll", b"new accepted")
        destination = self.file("runtime/native.dll", b"previous accepted")

        def corrupt(source, target):
            Path(target).write_bytes(b"bad copy")

        with (
            patch.object(artifacts.shutil, "copyfile", side_effect=corrupt),
            self.assertRaises(ValueError),
        ):
            artifacts.stage_native(native, destination)
        self.assertEqual(destination.read_bytes(), b"previous accepted")
        self.assertEqual(native.read_bytes(), b"new accepted")
        self.assertEqual(list(destination.parent.iterdir()), [destination])

    def test_native_source_change_during_staging_is_rejected(self):
        native = self.file("accepted.dll", b"before")
        destination = self.root / "runtime/native.dll"
        copy = shutil.copyfile

        def changed(source, target):
            copy(source, target)
            native.write_bytes(b"changed during copy")

        with (
            patch.object(artifacts.shutil, "copyfile", side_effect=changed),
            self.assertRaises(ValueError),
        ):
            artifacts.stage_native(native, destination)
        self.assertFalse(destination.exists())

    def test_failed_verification_restores_every_previous_artifact(self):
        stage = self.root / "stage"
        destination = self.root / "Artifacts"
        stage.mkdir()
        destination.mkdir()
        (stage / "new").write_bytes(b"new")
        (destination / "original").write_bytes(b"previous")
        before = hashes(destination.rglob("*"), self.root)
        with self.assertRaisesRegex(RuntimeError, "verification"):
            publish(stage, destination, lambda: (_ for _ in ()).throw(RuntimeError("verification")))
        self.assertEqual(hashes(destination.rglob("*"), self.root), before)
        self.assertEqual((self.root / "rejected-artifacts/new").read_bytes(), b"new")

    def test_install_failure_restores_previous_tree(self):
        stage, destination = self.root / "stage", self.root / "Artifacts"
        stage.mkdir()
        destination.mkdir()
        (destination / "original").write_text("original")
        normal = os.replace

        def replace(source, target):
            if source == stage:
                raise OSError("disk failure")
            normal(source, target)

        with (
            patch("sdk_tooling.artifacts.os.replace", side_effect=replace),
            self.assertRaises(OSError),
        ):
            publish(stage, destination, lambda: None)
        self.assertEqual((destination / "original").read_text(), "original")

    def test_interrupt_during_backup_move_restores_previous_tree(self):
        stage, destination = self.root / "stage", self.root / "Artifacts"
        stage.mkdir()
        destination.mkdir()
        (destination / "original").write_bytes(b"previous")
        normal = os.replace

        def replace(source, target):
            normal(source, target)
            if source == destination:
                raise KeyboardInterrupt()

        with (
            patch("sdk_tooling.artifacts.os.replace", side_effect=replace),
            self.assertRaises(KeyboardInterrupt),
        ):
            publish(stage, destination, lambda: self.fail("Interrupted stage was verified"))
        self.assertEqual((destination / "original").read_bytes(), b"previous")
        self.assertEqual(list(self.root.glob(".Artifacts.rollback-*")), [])

    def test_failed_rollback_retains_previous_tree_after_staging_cleanup(self):
        destination = self.root / "Artifacts"
        destination.mkdir()
        (destination / "original").write_bytes(b"last valid artifact")
        normal = os.replace
        with tempfile.TemporaryDirectory(dir=self.root) as temporary:
            stage = Path(temporary) / "stage"
            stage.mkdir()
            (stage / "new").write_bytes(b"rejected artifact")

            def replace(source, target):
                if source.name == "previous-artifacts" and target == destination:
                    raise PermissionError("rollback rename blocked")
                normal(source, target)

            def verify():
                raise ValueError("invalid staged artifact")

            with (
                patch("sdk_tooling.artifacts.os.replace", side_effect=replace),
                self.assertRaises(PermissionError) as failure,
            ):
                publish(stage, destination, verify)

        candidates = list(self.root.glob(".Artifacts.rollback-*/previous-artifacts"))
        self.assertEqual(len(candidates), 1)
        self.assertEqual((candidates[0] / "original").read_bytes(), b"last valid artifact")
        self.assertIn(str(candidates[0]), "\n".join(failure.exception.__notes__))
        self.assertIsInstance(failure.exception.__context__, ValueError)
        self.assertFalse(destination.exists())

    def test_failed_first_publication_restores_absence(self):
        stage, destination = self.root / "stage", self.root / "Artifacts"
        stage.mkdir()
        with self.assertRaises(KeyboardInterrupt):
            publish(stage, destination, lambda: (_ for _ in ()).throw(KeyboardInterrupt()))
        self.assertFalse(destination.exists())

    def test_success_installs_complete_stage(self):
        stage, destination = self.root / "stage", self.root / "Artifacts"
        stage.mkdir()
        (stage / "receipt").write_text("complete")
        publish(
            stage,
            destination,
            lambda: self.assertEqual((destination / "receipt").read_text(), "complete"),
        )
        self.assertFalse(stage.exists())

    def test_atomic_json_never_exposes_a_partial_record(self):
        destination = self.root / "receipt.json"
        destination.write_text('{"previous": true}')
        normal = json.dump

        def dumping(value, stream, **options):
            stream.write("{")
            stream.flush()
            self.assertEqual(json.loads(destination.read_text()), {"previous": True})
            stream.seek(0)
            normal(value, stream, **options)

        with patch("sdk_tooling.artifacts.json.dump", side_effect=dumping):
            write_json(destination, {"complete": True})
        self.assertEqual(json.loads(destination.read_text()), {"complete": True})

    def test_dependency_parser_rejects_a_different_snapshot_before_compiling(self):
        with (
            patch("sdk_tooling.artifacts.subprocess.run") as run,
            self.assertRaisesRegex(ValueError, "dependency parser differs from its build input"),
        ):
            list(dep_info_sources([], expected_sha256="0" * 64, cwd=self.root, env=os.environ))
        run.assert_not_called()

    def test_json_write_preserves_another_publishers_pending_file(self):
        destination = self.root / "receipt.json"
        pending = self.root / "receipt.json.pending"
        pending.write_text("another publisher")
        with self.assertRaises(FileExistsError):
            write_json(destination, {"complete": True})
        self.assertEqual(pending.read_text(), "another publisher")

    def test_build_lock_preserves_filesystem_errors(self):
        lock_function = "msvcrt.locking" if os.name == "nt" else "fcntl.flock"
        failure = OSError(errno.EIO, "fixture I/O failure")
        with patch(lock_function, side_effect=failure) as lock:
            with self.assertRaises(OSError) as caught:
                with build_lock(self.root / "build"):
                    self.fail("Publication started after the lock failed")
        self.assertIs(caught.exception, failure)
        lock.assert_called_once()

    def test_build_lock_excludes_another_publisher_and_releases_after_failure(self):
        directory = self.root / "build"
        with self.assertRaisesRegex(RuntimeError, "fixture"):
            with build_lock(directory):
                with self.assertRaisesRegex(RuntimeError, "owns this SDK"):
                    with build_lock(directory):
                        self.fail("A second publisher acquired the same SDK")
                raise RuntimeError("fixture")
        with build_lock(directory):
            with build_lock(self.root / "another-sdk"):
                pass


if __name__ == "__main__":
    unittest.main()
