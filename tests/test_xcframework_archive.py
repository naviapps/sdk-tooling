import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch
from zipfile import ZipFile

from sdk_tooling.artifacts import files, hashes
from sdk_tooling.xcframework_archive import archive_xcframeworks


@unittest.skipIf(os.name == "nt", "XCFramework archive fixtures require Unix symlinks")
class XcframeworkArchiveTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve()
        self.sdk = self.workspace / "sdk"
        self.sdk.mkdir()
        self.git("init", "--quiet")
        (self.sdk / ".gitignore").write_text("/.native-build/\n", encoding="utf-8")
        self.source = self.sdk / "Cargo.toml"
        self.source.write_text("[workspace]\n", encoding="utf-8")
        self.artifact = self.sdk / "Artifacts/CFixture.xcframework"
        self.library = self.artifact / "CFixture.framework/Versions/A/CFixture"
        self.library.parent.mkdir(parents=True)
        self.library.write_bytes(b"binary from committed checkout")
        self.library.chmod(0o755)
        self.alias = self.library.parent.parent.parent / "CFixture"
        self.alias.symlink_to("Versions/A/CFixture")
        self.receipt = self.artifact.parent / "native-build.json"
        self.record()
        self.git("add", "--all")
        self.git("commit", "-qm", "Fixture")
        self.revision = self.git("rev-parse", "HEAD")
        self.product = SimpleNamespace(
            root=self.sdk,
            module="CFixture",
            artifact=self.artifact,
            receipt=self.receipt,
            verify=Mock(),
        )
        self.products = {"fixture": self.product}
        patcher = patch("sdk_tooling.xcframework_archive.xcframeworks", return_value=self.products)
        patcher.start()
        self.addCleanup(patcher.stop)

    def git(self, *arguments):
        return subprocess.check_output(
            [
                "git",
                "-c",
                "core.hooksPath=" + os.devnull,
                "-c",
                "user.name=Archive Test",
                "-c",
                "user.email=archive@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=self.sdk,
            text=True,
        ).strip()

    def record(self):
        self.receipt.write_text(
            json.dumps({"outputs": hashes(files(self.artifact), self.sdk)}), encoding="utf-8"
        )

    def test_archives_ignore_timestamps_and_preserve_links_and_modes(self):
        self.library.write_bytes(b"regenerated binary and tracked framework receipt")
        self.record()
        first = archive_xcframeworks(self.sdk, self.workspace / "first", revision=self.revision)
        os.utime(self.library, (1, 1))
        second = archive_xcframeworks(self.sdk, self.workspace / "second")
        self.assertEqual(first.read_bytes(), second.read_bytes())
        record = json.loads(first.read_bytes())
        self.assertEqual((record["schema"], record["revision"]), (1, self.revision))
        module = record["artifacts"]["CFixture"]
        for output in (module, module["receipt"]):
            payload = (first.parent / output["file"]).read_bytes()
            self.assertEqual(output["bytes"], len(payload))
            self.assertEqual(output["sha256"], hashlib.sha256(payload).hexdigest())
        archive_path = first.parent / module["file"]
        self.assertEqual(archive_path.read_bytes(), (second.parent / module["file"]).read_bytes())
        with ZipFile(archive_path) as archive:
            self.assertIsNone(archive.testzip())
            link = archive.getinfo(self.alias.relative_to(self.artifact.parent).as_posix())
            executable = archive.getinfo(self.library.relative_to(self.artifact.parent).as_posix())
            self.assertEqual(archive.namelist()[0], "CFixture.xcframework/")
            self.assertTrue(stat.S_ISLNK(link.external_attr >> 16))
            self.assertEqual(archive.read(link), b"Versions/A/CFixture")
            self.assertEqual(stat.S_IMODE(executable.external_attr >> 16), 0o755)
        self.assertEqual(self.product.verify.call_count, 2)

    def test_rejects_source_edits_and_unowned_artifacts(self):
        destination = self.workspace / "archive"
        self.source.write_text("dirty source", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "must be clean"):
            archive_xcframeworks(self.sdk, destination)
        self.git("restore", self.source)
        (self.artifact.parent / "unowned.zip").write_bytes(b"private SDK metadata")
        with self.assertRaisesRegex(ValueError, "must be clean"):
            archive_xcframeworks(self.sdk, destination)
        self.product.verify.assert_not_called()
        self.assertFalse(destination.exists())

    def test_rejects_an_escaping_symlink_and_drift_from_verified_receipt(self):
        destination = self.workspace / "archive"
        self.alias.unlink()
        self.alias.symlink_to(self.source)
        self.record()
        with self.assertRaisesRegex(ValueError, "link escapes"):
            archive_xcframeworks(self.sdk, destination)
        self.assertFalse(destination.exists())
        self.alias.unlink()
        self.alias.symlink_to("Versions/A/CFixture")
        self.record()
        self.product.verify.side_effect = lambda: self.library.write_bytes(b"drift")
        with self.assertRaisesRegex(ValueError, "changed during archive"):
            archive_xcframeworks(self.sdk, destination)
        self.assertFalse(destination.exists())
