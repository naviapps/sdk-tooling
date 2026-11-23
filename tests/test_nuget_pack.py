"""Owned package bytes and injected MSBuild; no native execution is claimed."""

import contextlib
import hashlib
import io
import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from sdk_tooling import dotnet, nuget
from sdk_tooling.artifacts import build_lock


class NativePackageTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-native-package-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.project = self.file(
            "bindings/src/Example.csproj",
            b"""<Project><PropertyGroup>
          <TargetFramework>net10.0</TargetFramework><Description>Owned test SDK</Description>
          </PropertyGroup><ItemGroup><PackageReference Include="Example.Dependency" Version="[2.0.0]" />
          </ItemGroup></Project>""",
        )
        self.config = self.file("bindings/NuGet.Config", b"<configuration />")
        self.file("bindings/src/Example.cs", b"// Owned managed fixture")
        self.selected = {
            "README.md": self.file("README.md"),
            "LICENSE": self.file("LICENSE"),
            "ThirdParty/Upstream/NOTICE": self.file(
                "ThirdParty/Upstream/NOTICE", b"verbatim\r\nnotice\x00"
            ),
        }
        artifact = self.root / "Artifacts/Example.xcframework"
        self.library = self.file(
            "Artifacts/Example.xcframework/macos-arm64_x86_64/Example.framework/Example"
        )
        self.mac_outputs = {
            self.library.relative_to(self.root).as_posix(): hashlib.sha256(
                self.library.read_bytes()
            ).hexdigest()
        }
        self.mac_record = json.dumps({"outputs": self.mac_outputs}).encode()
        self.native = SimpleNamespace(
            root=self.root,
            module="Example",
            version="1.2.3",
            library="example",
            artifact=artifact,
            receipt=self.file("Artifacts/native.json", self.mac_record),
            build_sources=Mock(return_value={"production.rs": "accepted"}),
            verify=Mock(),
        )
        self.output = self.root / ".native-build/packages/Example.1.2.3.nupkg"
        self.file(str(self.output.relative_to(self.root)), b"previous accepted package")
        self.previous_record = self.file(
            str(self.output.relative_to(self.root)) + ".json", b"previous receipt"
        )
        self.mutate = lambda: None
        self.dependency_range = "[2.0.0]"
        self.managed_outputs = {
            f"lib/net10.0/Example.{extension}": self.root / f"out/managed Example.{extension}"
            for extension in ("dll", "xml")
        }
        self.evaluate = patch.object(
            dotnet, "run_command", side_effect=self.evaluate_project
        ).start()
        self.build = patch.object(dotnet, "build_managed", side_effect=self.build_managed).start()
        self.pack = patch.object(dotnet, "pack_project", side_effect=self.pack_project).start()
        self.addCleanup(patch.stopall)

    def file(self, relative, data=b"owned bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def build_managed(self, *args, **options):
        for member, source in self.managed_outputs.items():
            self.file(source.relative_to(self.root), f"managed {member}".encode())

    def evaluate_project(self, command, **options):
        self.assertIn("-getProperty:TargetFramework,PackageId", command)
        output = {
            "Properties": {"TargetFramework": "net10.0", "PackageId": "Example"},
            "Items": {
                group + "Output": [
                    {"FinalOutputPath": str(source), "TargetPath": Path(member).name}
                ]
                for group, (member, source) in zip(
                    ("BuiltProjectOutputGroup", "DocumentationProjectOutputGroup"),
                    self.managed_outputs.items(),
                    strict=True,
                )
            },
        }
        output["Items"]["PackageReference"] = [
            {"Identity": "Example.Dependency", "Version": "[2.0.0]"}
        ]
        return SimpleNamespace(stdout=json.dumps(output))

    def pack_project(self, *args, **options):
        properties = options["properties"]
        self.assertNotIn("NuspecFile", properties)
        self.assertEqual(properties["Version"], "1.2.3")
        self.assertTrue(options["no_build"])
        runtimes = Path(properties["SdkNativeRuntimeAssets"])
        with zipfile.ZipFile(options["output"] / self.output.name, "w") as archive:
            archive.writestr(
                "Example.nuspec",
                f"""<package><metadata><authors>Owned Author</authors>
                <description>Owned test SDK</description><dependencies><group targetFramework="net10.0">
                <dependency id="Example.Dependency" version="{self.dependency_range}" />
                </group></dependencies></metadata></package>""",
            )
            for member, source in (self.selected | self.managed_outputs).items():
                archive.write(source, member)
            for source in runtimes.rglob("*"):
                if source.is_file():
                    archive.write(source, source.relative_to(runtimes.parent).as_posix())
        self.mutate()

    def invoke(self, **options):
        arguments = dict(
            project=self.project,
            package_files=self.selected,
            nuget_config=self.config,
            runtimes=["osx-arm64"],
            environment={"DOTNET_BIN": "owned-dotnet"},
        )
        arguments.update(options)
        with contextlib.redirect_stdout(io.StringIO()):
            return nuget.pack_native(self.native, **arguments)

    def assert_previous(self):
        self.assertEqual(self.output.read_bytes(), b"previous accepted package")
        self.assertEqual(self.previous_record.read_bytes(), b"previous receipt")

    def test_exact_files_runtimes_metadata_and_private_feeds_are_preserved(self):
        result = self.invoke(
            runtimes=["osx-x64", "osx-arm64", "osx-arm64"], sources=["owned local feed"]
        )
        self.assertEqual(result, self.output)
        self.native.verify.assert_called_once()
        with zipfile.ZipFile(result) as archive:
            expected = {
                "runtimes/osx-arm64/native/libexample.dylib",
                "runtimes/osx-x64/native/libexample.dylib",
            }
            self.assertEqual({n for n in archive.namelist() if n.startswith("runtimes/")}, expected)
            for member in expected:
                self.assertEqual(archive.read(member), self.library.read_bytes())
            for member, source in self.selected.items():
                self.assertEqual(archive.read(member), source.read_bytes())
            metadata = ET.fromstring(archive.read("Example.nuspec"))
            self.assertEqual(metadata.findtext("metadata/authors"), "Owned Author")
            self.assertEqual(metadata.findtext("metadata/description"), "Owned test SDK")
            dependency = metadata.find("metadata/dependencies/group/dependency")
            self.assertEqual(dependency.attrib, {"id": "Example.Dependency", "version": "[2.0.0]"})
        self.assertEqual(self.build.call_args.kwargs["sources"], ["owned local feed"])
        self.assertTrue(self.build.call_args.kwargs["verify_style"])
        receipt = json.loads(self.previous_record.read_text())
        self.assertEqual(receipt["sha256"], hashlib.sha256(result.read_bytes()).hexdigest())
        self.assertEqual(receipt["runtimes"], ["osx-arm64", "osx-x64"])
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(
                receipt["files"],
                {
                    member: hashlib.sha256(archive.read(member)).hexdigest()
                    for member in set(archive.namelist()) - {"Example.nuspec"}
                },
            )
        digest = hashlib.sha256(self.native.receipt.read_bytes()).hexdigest()
        self.assertEqual(receipt["nativeReceipts"], {"osx-arm64": digest, "osx-x64": digest})
        self.assertEqual(
            set(receipt["packagingToolInputs"]), {"artifacts.py", "dotnet.py", "nuget.py"}
        )

    def test_windows_runtime_uses_accepted_dll_and_its_own_receipt(self):
        dll, receipt, accepted = self.windows_runtime("win-arm64", b"opaque DLL fixture")
        with patch(
            "sdk_tooling.windows.verify_artifact", return_value=(dll.parent, accepted)
        ) as verify:
            result = self.invoke(runtimes=["win-arm64"])
        verify.assert_called_once_with(self.native, architecture="arm64")
        self.native.verify.assert_not_called()
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(
                archive.read("runtimes/win-arm64/native/example.dll"), dll.read_bytes()
            )
        self.assertEqual(
            json.loads(self.previous_record.read_text())["nativeReceipts"],
            {"win-arm64": hashlib.sha256(receipt.read_bytes()).hexdigest()},
        )

    def windows_runtime(self, runtime, data=b"owned bytes"):
        directory = ".native-build/windows/" + runtime
        dll = self.file(directory + "/example.dll", data)
        accepted = {"outputs": {dll.name: hashlib.sha256(data).hexdigest()}}
        receipt = self.file(directory + "/native-build.json", json.dumps(accepted).encode())
        return dll, receipt, accepted

    def test_native_library_replaced_between_verification_and_staging_is_rejected(self):
        dll, _, accepted = self.windows_runtime("win-x64")
        for runtime, path in (("osx-arm64", self.library), ("win-x64", dll)):
            stage_file = nuget.stage_file

            def replace_before_stage(source, destination):
                path.write_bytes(b"newer bytes")
                return stage_file(source, destination)

            with (
                self.subTest(runtime=runtime),
                patch.object(nuget, "stage_file", side_effect=replace_before_stage),
                patch("sdk_tooling.windows.verify_artifact", return_value=(dll.parent, accepted)),
            ):
                self.pack.reset_mock()
                with self.assertRaisesRegex(
                    ValueError, "Native library differs from its verified receipt"
                ):
                    self.invoke(runtimes=[runtime])
                self.pack.assert_not_called()
                self.assert_previous()

    def test_native_receipt_replaced_while_verifying_is_rejected(self):
        dll, receipt, accepted = self.windows_runtime("win-x64")

        def changed_windows(*args, **options):
            receipt.write_bytes(receipt.read_bytes() + b" ")
            return dll.parent, accepted

        def changed_mac(*args, **options):
            self.native.receipt.write_bytes(self.mac_record + b" ")

        self.native.verify.side_effect = changed_mac
        with patch("sdk_tooling.windows.verify_artifact", side_effect=changed_windows):
            for runtime in ("osx-arm64", "win-x64"):
                with (
                    self.subTest(runtime=runtime),
                    self.assertRaisesRegex(ValueError, "Native receipt changed"),
                ):
                    self.invoke(runtimes=[runtime])
                self.assert_previous()
        self.build.assert_not_called()

    def test_versioned_framework_uses_the_terminal_library_receipt_hash(self):
        framework = self.library.parent
        terminal = self.file((framework / "Versions/A/Example").relative_to(self.root))
        alternate = self.file(
            (framework / "Versions/A/Alternate").relative_to(self.root), b"other verified file"
        )
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        self.library.unlink()
        self.library.symlink_to(Path("Versions/Current/Example"))
        outputs = {
            path.relative_to(self.root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in (terminal, alternate)
        }
        outputs[self.library.relative_to(self.root).as_posix()] = hashlib.sha256(
            f"symlink:{os.readlink(self.library)}".encode()
        ).hexdigest()
        self.native.receipt.write_text(json.dumps({"outputs": outputs}))
        result = self.invoke()
        with zipfile.ZipFile(result) as package:
            self.assertEqual(
                package.read("runtimes/osx-arm64/native/libexample.dylib"), terminal.read_bytes()
            )
        actual = self.output.read_bytes()
        record = self.previous_record.read_bytes()

        def retarget(*args, **options):
            self.library.unlink()
            self.library.symlink_to(Path("Versions/Current/Alternate"))

        self.native.verify.side_effect = retarget
        with self.assertRaisesRegex(ValueError, "Native library changed during verification"):
            self.invoke()
        self.assertEqual(self.output.read_bytes(), actual)
        self.assertEqual(self.previous_record.read_bytes(), record)

    def test_package_binds_runtime_bytes_even_if_the_live_library_matches_the_replacement(self):
        audit = nuget.audit_package
        accepted = self.library.read_bytes()
        replacement = b"newer bytes"

        def replace_staged_library(*args, **options):
            staged = Path(options["properties"]["SdkNativeRuntimeAssets"])
            (staged / "osx-arm64/native/libexample.dylib").write_bytes(replacement)
            self.library.write_bytes(replacement)
            self.pack_project(*args, **options)

        def restore_after_audit(*args, **options):
            try:
                return audit(*args, **options)
            finally:
                self.library.write_bytes(accepted)

        self.pack.side_effect = replace_staged_library
        with (
            patch.object(nuget, "audit_package", side_effect=restore_after_audit),
            self.assertRaisesRegex(ValueError, "Packaged bytes differ from the accepted input"),
        ):
            self.invoke()
        self.assert_previous()

    def test_native_verification_requires_an_available_producer_lock(self):
        with (
            build_lock(self.root / ".native-build"),
            self.assertRaisesRegex(RuntimeError, "Another native build"),
        ):
            self.invoke()
        self.native.verify.assert_not_called()
        self.build.assert_not_called()
        self.assert_previous()

    def test_oversized_native_receipt_is_rejected_before_verification(self):
        with (
            patch.object(nuget, "_MAX_NATIVE_RECEIPT_BYTES", len(self.mac_record) - 1),
            self.assertRaisesRegex(ValueError, "Native receipt exceeds the size limit"),
        ):
            self.invoke()
        self.native.verify.assert_not_called()
        self.build.assert_not_called()
        self.assert_previous()

    def test_source_receipt_or_added_managed_input_change_preserves_previous_package(self):
        actions = [
            lambda: setattr(
                self.native.build_sources, "return_value", {"production.rs": "changed"}
            ),
            lambda: self.native.receipt.write_bytes(b"changed receipt"),
            lambda: self.file("bindings/src/Added.cs", b"new managed input"),
            lambda: self.config.write_bytes(b"changed feed selection"),
        ]
        for action in actions:
            with self.subTest(action=actions.index(action)):
                self.output.write_bytes(b"previous accepted package")
                self.previous_record.write_bytes(b"previous receipt")
                self.native.build_sources.return_value = {"production.rs": "accepted"}
                self.native.receipt.write_bytes(self.mac_record)
                (self.root / "bindings/src/Added.cs").unlink(missing_ok=True)
                self.config.write_bytes(b"<configuration />")
                self.mutate = action
                with self.assertRaisesRegex(ValueError, "changed"):
                    self.invoke(runtimes=["osx-arm64", "osx-x64"])
                self.assert_previous()

    def test_selected_input_mutation_is_not_published(self):
        self.mutate = lambda: self.library.write_bytes(b"changed native library")
        with self.assertRaisesRegex(ValueError, "bytes differ"):
            self.invoke(runtimes=["osx-arm64", "osx-x64"])
        self.assert_previous()

    def test_generated_trees_are_skipped_but_explicit_inputs_still_detect_drift(self):
        generated = self.file("bindings/src/Feature/obj/Debug/Generated.cs")
        owned = self.file("bindings/src/Case/Obj/Owned.cs")
        self.mutate = lambda: generated.write_bytes(b"new generated bytes")
        self.invoke()
        receipt = json.loads(self.previous_record.read_text())
        self.assertIn(owned.relative_to(self.root).as_posix(), receipt["managedInputs"])
        self.assertNotIn(generated.relative_to(self.root).as_posix(), receipt["managedInputs"])

        accepted_package = self.output.read_bytes()
        accepted_receipt = self.previous_record.read_bytes()
        self.mutate = lambda: generated.write_bytes(b"changed explicit input")
        with self.assertRaisesRegex(ValueError, "packaging inputs changed"):
            self.invoke(input_files=[generated])
        self.assertEqual(self.output.read_bytes(), accepted_package)
        self.assertEqual(self.previous_record.read_bytes(), accepted_receipt)

    def test_unsafe_or_reserved_members_fail_before_build(self):
        for member in (
            "../escape",
            "/absolute",
            "ThirdParty\\alias",
            "lib/net10.0/Example.dll",
            "runtimes/win-arm64/native/example.dll",
        ):
            with (
                self.subTest(member=member),
                self.assertRaisesRegex(ValueError, "canonical relative path"),
            ):
                self.invoke(package_files={**self.selected, member: self.library})
        self.build.assert_not_called()
        self.assert_previous()

    def test_changed_packaged_dependency_is_rejected(self):
        self.dependency_range = "2.0.0"
        with self.assertRaisesRegex(ValueError, "Packaged dependencies differ"):
            self.invoke()
        self.assert_previous()

    def test_another_publisher_is_not_overwritten(self):
        with (
            build_lock(self.output.parent),
            self.assertRaisesRegex(RuntimeError, "Another native build"),
        ):
            self.invoke()
        self.build.assert_not_called()
        self.assert_previous()

    def test_receipt_preparation_failure_preserves_the_accepted_pair(self):
        with (
            patch.object(nuget, "write_json", side_effect=OSError("fixture write failed")),
            self.assertRaisesRegex(OSError, "fixture write failed"),
        ):
            self.invoke()
        self.assert_previous()
        self.assertEqual(list(self.output.parent.glob(".*.rollback-*")), [])

    def test_receipt_replacement_failure_restores_the_accepted_pair(self):
        replace, failed = os.replace, False

        def fail_receipt_once(source, target):
            nonlocal failed
            if target == self.previous_record and not failed:
                failed = True
                raise PermissionError("fixture receipt replacement failed")
            return replace(source, target)

        with (
            patch.object(os, "replace", side_effect=fail_receipt_once),
            self.assertRaisesRegex(PermissionError, "fixture receipt replacement failed"),
        ):
            self.invoke()
        self.assert_previous()
        self.assertEqual(list(self.output.parent.glob(".*.rollback-*")), [])

    def test_interruption_after_receipt_replacement_restores_the_accepted_pair(self):
        replace, interrupted = os.replace, False

        def interrupt_receipt_once(source, target):
            nonlocal interrupted
            replace(source, target)
            if target == self.previous_record and not interrupted:
                interrupted = True
                raise KeyboardInterrupt()

        with (
            patch.object(os, "replace", side_effect=interrupt_receipt_once),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.invoke()
        self.assert_previous()

    def test_first_publication_failure_restores_absent_package_and_receipt(self):
        self.output.unlink()
        self.previous_record.unlink()
        replace = os.replace

        def fail_receipt(source, target):
            if target == self.previous_record:
                raise PermissionError("fixture receipt replacement failed")
            return replace(source, target)

        with (
            patch.object(os, "replace", side_effect=fail_receipt),
            self.assertRaises(PermissionError),
        ):
            self.invoke()
        self.assertFalse(self.output.exists())
        self.assertFalse(self.previous_record.exists())

    def test_failed_rollback_retains_accepted_files_outside_temporary_staging(self):
        replace, failed = os.replace, False

        def fail_publication_and_rollback(source, target):
            nonlocal failed
            if target == self.previous_record and not failed:
                failed = True
                raise OSError("fixture receipt replacement failed")
            if target == self.output and failed:
                raise PermissionError("fixture rollback blocked")
            return replace(source, target)

        with (
            patch.object(os, "replace", side_effect=fail_publication_and_rollback),
            self.assertRaisesRegex(PermissionError, "fixture rollback blocked") as raised,
        ):
            self.invoke()
        [recovery] = self.output.parent.glob(".*.rollback-*")
        self.assertEqual((recovery / self.output.name).read_bytes(), b"previous accepted package")
        self.assertEqual((recovery / self.previous_record.name).read_bytes(), b"previous receipt")
        self.assertIn(str(recovery), "\n".join(raised.exception.__notes__))
        self.assertEqual(list(self.output.parent.glob("pack-*")), [])

    def test_package_changed_after_inspection_is_not_published(self):
        audit = nuget.audit_package

        def audit_then_change(package, **options):
            digest = audit(package, **options)
            with package.open("ab") as stream:
                stream.write(b"changed after inspection")
            return digest

        with (
            patch.object(nuget, "audit_package", side_effect=audit_then_change),
            self.assertRaisesRegex(ValueError, "changed after inspection"),
        ):
            self.invoke()
        self.assert_previous()

    def test_symlinked_staged_package_cannot_replace_the_accepted_pair(self):
        pack = self.pack_project
        retained = self.root / "retained-package.nupkg"

        def pack_as_symlink(*args, **options):
            pack(*args, **options)
            archive = options["output"] / self.output.name
            shutil.copyfile(archive, retained)
            archive.unlink()
            try:
                archive.symlink_to(retained)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows denied symlink creation; no privilege changes attempted")
                raise

        self.pack.side_effect = pack_as_symlink
        with self.assertRaisesRegex(ValueError, "ordinary staged package"):
            self.invoke()
        self.assert_previous()
        self.assertTrue(retained.is_file())


if __name__ == "__main__":
    unittest.main()
