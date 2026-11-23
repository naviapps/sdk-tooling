import hashlib
import json
import os
import shutil
import tempfile
import unittest
from contextlib import nullcontext
from functools import partial
from itertools import product
from pathlib import Path
from unittest.mock import Mock, patch

from sdk_tooling import artifacts, windows_delivery

RECEIPT = "windows-evidence.json"
PACKAGE = "Example.SDK.0.1.0.nupkg"
publish_x64 = partial(windows_delivery.publish_x64, receipt_name=RECEIPT)


class WindowsDeliveryTests(unittest.TestCase):
    def test_locked_fetch_covers_build_and_notice_graphs(self):
        targets = (
            "x86_64-pc-windows-msvc",
            "aarch64-apple-darwin",
            "x86_64-apple-darwin",
            "aarch64-pc-windows-msvc",
        )
        args = windows_delivery.locked_fetch_args()
        self.assertEqual(args[:3], ("cargo", "fetch", "--locked"))
        self.assertEqual(args[3::2], ("--target",) * len(targets))
        self.assertEqual(args[4::2], targets)
        self.assertEqual(
            windows_delivery.locked_fetch_args(notice_targets=(windows_delivery.TARGET,)),
            ("cargo", "fetch", "--locked", "--target", windows_delivery.TARGET),
        )

    def test_captured_source_requires_commit_and_fingerprint(self):
        commit, digest = "a" * 40, "b" * 64
        values = {"VM_RUNNER_SOURCE_COMMIT": commit, "VM_RUNNER_SOURCE_FINGERPRINT": digest}
        with patch.dict(os.environ, values, clear=True):
            self.assertEqual(windows_delivery.acceptance_source(Path.cwd()), (commit, digest))
            del os.environ["VM_RUNNER_SOURCE_FINGERPRINT"]
            with self.assertRaisesRegex(ValueError, "workspace SHA-256"):
                windows_delivery.acceptance_source(Path.cwd())

    def test_uncommitted_checkout_cannot_supply_acceptance_source(self):
        with (
            patch.dict(os.environ, {}, clear=True),
            patch.object(windows_delivery.subprocess, "check_output", return_value=" M Cargo.lock"),
            self.assertRaisesRegex(ValueError, "clean committed checkout"),
        ):
            windows_delivery.acceptance_source(Path.cwd())

    def test_x64_evidence_checks_destinations_before_replacing_previous_package(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package = root / PACKAGE
            package.write_bytes(b"new package")
            receipt = {
                "version": "0.1.0",
                "packages": {package.name: hashlib.sha256(package.read_bytes()).hexdigest()},
            }
            output = root / "output"
            external = root / "external"
            local = output / "win-x64" / "0.1.0"
            local.mkdir(parents=True)
            (local / package.name).write_bytes(b"previous package")
            occupied = external / "win-x64" / "0.1.0" / "user-file.txt"
            occupied.parent.mkdir(parents=True)
            occupied.write_text("owner bytes", encoding="utf-8")
            tooling = Mock(build_lock=nullcontext)

            with self.assertRaisesRegex(ValueError, "contains unrelated files"):
                publish_x64(
                    package, receipt, output_dir=output, artifact_root=external, tooling=tooling
                )

            tooling.publish.assert_not_called()
            self.assertEqual((local / package.name).read_bytes(), b"previous package")
            self.assertEqual(occupied.read_text(encoding="utf-8"), "owner bytes")

    def test_x64_evidence_keeps_previous_outputs_when_external_publication_fails(self):
        for failure, had_local in product(("stage", "rename", "verify"), (False, True)):
            with (
                self.subTest(failure=failure, had_local=had_local),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                package, receipt, output, artifact_root, local, external = x64_evidence(root)
                if had_local:
                    write_previous_evidence(local, package.name)
                write_previous_evidence(external, package.name)
                replace = os.replace

                def fail_external_replace(source, destination):
                    destination = Path(destination)
                    writes_external_receipt = (
                        destination.name == RECEIPT and destination.is_relative_to(external.parent)
                    )
                    installs_external = (
                        destination == external and Path(source).name == external.name
                    )
                    if (failure == "stage" and writes_external_receipt) or (
                        failure == "rename" and installs_external
                    ):
                        raise PermissionError("external evidence is locked")
                    replace(source, destination)
                    if failure == "verify" and installs_external:
                        (destination / package.name).write_bytes(b"changed during replacement")

                error_type, message = (
                    (ValueError, "differs from the accepted package and receipt")
                    if failure == "verify"
                    else (PermissionError, "external evidence is locked")
                )

                with (
                    patch.object(artifacts.os, "replace", side_effect=fail_external_replace),
                    self.assertRaisesRegex(error_type, message),
                ):
                    publish_x64(
                        package,
                        receipt,
                        output_dir=output,
                        artifact_root=artifact_root,
                        tooling=artifacts,
                    )

                if had_local:
                    assert_previous_evidence(self, local, package.name)
                else:
                    self.assertFalse(local.exists())
                assert_previous_evidence(self, external, package.name)

    def test_x64_evidence_preserves_an_external_file_that_appears_during_commit(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, receipt, output, artifact_root, local, external = x64_evidence(root)
            write_previous_evidence(local, package.name)
            occupied = external / "user-file.txt"
            publish = artifacts.publish

            def occupy_after_local_replacement(stage, destination, verify):
                def occupy_then_verify():
                    occupied.parent.mkdir(parents=True, exist_ok=True)
                    occupied.write_bytes(b"owner bytes")
                    verify()

                publish(stage, destination, occupy_then_verify if destination == local else verify)

            with (
                patch.object(artifacts, "publish", side_effect=occupy_after_local_replacement),
                self.assertRaisesRegex(ValueError, "contains unrelated files"),
            ):
                publish_x64(
                    package,
                    receipt,
                    output_dir=output,
                    artifact_root=artifact_root,
                    tooling=artifacts,
                )

            self.assertEqual({path.name for path in external.iterdir()}, {occupied.name})
            self.assertEqual(occupied.read_bytes(), b"owner bytes")
            assert_previous_evidence(self, local, package.name)

    def test_x64_evidence_keeps_both_verified_outputs_if_external_cleanup_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            package, receipt, output, artifact_root, local, external = x64_evidence(root)
            write_previous_evidence(local, package.name)
            write_previous_evidence(external, package.name)
            rmtree = shutil.rmtree

            def fail_external_cleanup(path, *args, **kwargs):
                path = Path(path)
                if path.parent == external.parent and path.name.startswith(".0.1.0.rollback-"):
                    raise PermissionError("previous external evidence is locked")
                return rmtree(path, *args, **kwargs)

            with (
                patch.object(artifacts.shutil, "rmtree", side_effect=fail_external_cleanup),
                self.assertRaisesRegex(PermissionError, "previous external evidence is locked"),
            ):
                publish_x64(
                    package,
                    receipt,
                    output_dir=output,
                    artifact_root=artifact_root,
                    tooling=artifacts,
                )

            for destination in (local, external):
                self.assertEqual((destination / package.name).read_bytes(), b"new package")
                self.assertEqual(json.loads((destination / RECEIPT).read_bytes()), receipt)
            (recovery,) = external.parent.glob(".0.1.0.rollback-*")
            assert_previous_evidence(self, recovery / "previous-artifacts", package.name)

    def test_publishes_package_set_with_optional_legacy_directory_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source, destination = root / "candidate", root / "published"
            source.mkdir()
            packages = {}
            for name in ("Example.Core.0.1.0.nupkg", "Example.Windows.0.1.0.nupkg"):
                data = name.encode()
                (source / name).write_bytes(data)
                packages[name] = hashlib.sha256(data).hexdigest()
            old_packages = destination / "packages"
            old_packages.mkdir(parents=True)
            (old_packages / name).write_bytes(b"previous package")
            owner = old_packages / "notes.txt"
            owner.write_bytes(b"user data")
            publish = partial(
                windows_delivery.publish_package_evidence,
                source,
                {"packages": packages},
                receipt_name=RECEIPT,
                destination=destination,
                legacy_packages_directory=True,
            )

            with self.assertRaisesRegex(ValueError, "contains unrelated files"):
                publish()
            self.assertEqual(owner.read_bytes(), b"user data")
            owner.unlink()
            publish()
            self.assertFalse(old_packages.exists())
            windows_delivery.verify_packages(destination, packages, receipt_name=RECEIPT)
            (source / name).write_bytes(b"changed")
            with self.assertRaisesRegex(
                ValueError, "differs from the accepted package and receipt"
            ):
                publish()
            windows_delivery.verify_packages(destination, packages, receipt_name=RECEIPT)

    def test_package_set_rejects_path_aliases_and_non_sha256_digests(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            destination = root / "destination"
            for packages in (
                {"../Example.nupkg": "0" * 64},
                {"CON.nupkg": "0" * 64},
                {"Example.nupkg": "0" * 64, "example.nupkg": "1" * 64},
                {"Example.nupkg": "unknown"},
            ):
                with (
                    self.subTest(packages=packages),
                    self.assertRaisesRegex(ValueError, "package names, SHA-256 digests"),
                ):
                    windows_delivery.publish_package_evidence(
                        root,
                        {"packages": packages},
                        receipt_name=RECEIPT,
                        destination=destination,
                    )
                self.assertFalse(destination.exists())


def x64_evidence(root):
    package = root / PACKAGE
    package.write_bytes(b"new package")
    receipt = {
        "version": "0.1.0",
        "packages": {package.name: hashlib.sha256(package.read_bytes()).hexdigest()},
    }
    output = root / "output"
    artifacts = root / "external"
    relative = Path("win-x64") / receipt["version"]
    return package, receipt, output, artifacts, output / relative, artifacts / relative


def write_previous_evidence(directory, package_name):
    directory.mkdir(parents=True)
    (directory / package_name).write_bytes(b"previous package")
    (directory / RECEIPT).write_bytes(b"previous receipt")


def assert_previous_evidence(test, directory, package_name):
    test.assertEqual((directory / package_name).read_bytes(), b"previous package")
    test.assertEqual((directory / RECEIPT).read_bytes(), b"previous receipt")
