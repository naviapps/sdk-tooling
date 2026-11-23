"""Binding checks with owned files and an opt-in .NET consumer run."""

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
import warnings
import xml.etree.ElementTree as ET
import zipfile
from contextlib import chdir
from pathlib import Path
from unittest import mock

from sdk_tooling import bindings, workspaces


class BindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-bindings-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.env = {"PATH": str(self.root / "tools"), "SDK_NATIVE_LIBRARY": "accepted-native"}
        self.calls = []
        guard = mock.patch.object(
            bindings.subprocess,
            "run",
            side_effect=AssertionError("Unit tests must inject external commands"),
        )
        guard.start()
        self.addCleanup(guard.stop)

    def run_command(self, argv, **options):
        self.calls.append((argv, options))
        self.assertTrue(options["check"])
        self.assertNotIn("shell", options)
        if argv[1:3] == ["restore", str(Path(options["cwd"]) / "Consumer.csproj")]:
            self.restore_consumer(Path(options["cwd"]), options["env"])
        return subprocess.CompletedProcess(argv, 0)

    def restore_consumer(self, work, env):
        feed, cache = work / "feed", Path(env["NUGET_PACKAGES"])
        libraries = {}
        for package in sorted(feed.glob("*.nupkg")):
            name, *version_parts = package.stem.rsplit(".", 3)
            version = ".".join(version_parts)
            relative = f"{name.lower()}/{version}"
            installed = cache / relative
            installed.mkdir(parents=True)
            (installed / f"{name.lower()}.{version}.nupkg").write_bytes(package.read_bytes())
            libraries[f"{name}/{version}"] = {"type": "package", "path": relative}
        assets = {
            "project": {"restore": {"sources": {str(feed): {}}, "packagesPath": str(cache)}},
            "packageFolders": {str(cache): {}},
            "libraries": libraries,
        }
        path = work / "obj/project.assets.json"
        path.parent.mkdir()
        path.write_text(json.dumps(assets), encoding="utf-8")

    def file(self, relative, content=b"owned bytes"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    def package(self, members=None, name="Example.1.0.0.nupkg"):
        package = self.root / name
        package.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(package, "w") as archive:
            for member, content in (
                members or {"Example.nuspec": b"<package><metadata /></package>"}
            ).items():
                archive.writestr(member, content)
        return package

    def symlink(self, link, target):
        try:
            link.symlink_to(Path(target))
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                self.skipTest(
                    "Windows denied symlink creation (WinError 1314); no privilege changes attempted"
                )
            raise

    def raw_member_package(self, member):
        # Patch both ZIP filename headers after writing so the host's writer
        # cannot normalize a malformed backslash name before the reader sees it.
        canonical = member.replace("\\", "/").encode("utf-8")
        package = self.package({canonical.decode("utf-8"): b"native"})
        data = package.read_bytes()
        self.assertEqual(data.count(canonical), 2)
        package.write_bytes(data.replace(canonical, member.encode("utf-8")))
        return package

    def test_symlink_fixture_skips_only_windows_privilege_denial(self):
        error = OSError("symlink privilege unavailable")
        error.winerror = 1314
        with (
            mock.patch.object(Path, "symlink_to", side_effect=error),
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
                mock.patch.object(Path, "symlink_to", side_effect=error),
            ):
                with self.assertRaises(PermissionError) as raised:
                    self.symlink(self.root / "link", "owned-target")
                self.assertIs(raised.exception, error)

    def test_clang_preserves_standard_literal_paths_and_link_flags(self):
        source = self.file("source with spaces/caller $(literal).c")
        library = self.file("Framework Name.framework/Native")
        output = self.root / "work/abi"
        bindings.compile_c(
            source,
            output=output,
            compiler=("xcrun", "clang"),
            env=self.env,
            cwd=self.root,
            include_dirs=(source.parent,),
            libraries=(library,),
            standard="c17",
            flags=("-fsanitize=address", "-Wl,-rpath," + str(library.parent)),
            runner=self.run_command,
        )
        argv, options = self.calls[0]
        self.assertEqual(argv[:2], ["xcrun", "clang"])
        self.assertIn("-std=c17", argv)
        self.assertIn("-Werror", argv)
        self.assertIn(str(source), argv)
        self.assertIn(str(library), argv)
        self.assertIn("-fsanitize=address", argv)
        self.assertEqual(argv[argv.index("-o") + 1], str(output))
        self.assertEqual(options["env"], self.env)

    def test_msvc_uses_utf8_and_retains_c11_import_library_and_caller_flags(self):
        source = self.file("caller.c")
        library = self.file("native.dll.lib")
        output = self.root / "work/abi.exe"
        bindings.compile_c(
            source,
            output=output,
            compiler=("cl.exe",),
            env=self.env,
            cwd=self.root,
            include_dirs=(source.parent,),
            libraries=(library,),
            standard="c11",
            style="msvc",
            flags=("/MT", "/D_CRT_SECURE_NO_WARNINGS"),
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        for value in (
            "/utf-8",
            "/std:c11",
            "/W4",
            "/WX",
            "/MT",
            "/D_CRT_SECURE_NO_WARNINGS",
            str(library),
            "/Fe:" + str(output),
            "/Fo:" + str(output.with_suffix(".obj")),
        ):
            self.assertIn(value, argv)

    def test_relative_compiler_output_is_owned_by_command_working_directory(self):
        bindings.compile_c(
            "caller.c",
            output="out/abi",
            compiler=("clang",),
            env=self.env,
            cwd=self.root,
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        self.assertEqual(
            Path(argv[argv.index("-o") + 1]).resolve(), (self.root / "out/abi").resolve()
        )
        self.assertTrue((self.root / "out").is_dir())

    def test_compiler_style_and_shell_strings_are_rejected_before_execution(self):
        for compiler, style in ((("clang",), "unknown"), ("xcrun clang", "clang")):
            with self.subTest(compiler=compiler, style=style), self.assertRaises(ValueError):
                bindings.compile_c(
                    "owned.c",
                    output=self.root / "out/abi",
                    compiler=compiler,
                    style=style,
                    env=self.env,
                    cwd=self.root,
                    runner=self.run_command,
                )
        self.assertFalse(self.calls)
        self.assertFalse((self.root / "out").exists())

    def test_managed_explicit_restore_and_fixture_arguments(self):
        project = self.file("managed/Test.csproj")
        config = self.file("NuGet.Config")
        library = self.file("native library.dylib")
        bindings.run_managed(
            "dotnet",
            project,
            env=self.env,
            cwd=self.root,
            nuget_config=config,
            sources=(self.root / "local feed",),
            properties={"CoreProject": "core & fixture.csproj"},
            arguments=("--native-library", library),
            runner=self.run_command,
        )
        restore, run = [call[0] for call in self.calls]
        self.assertEqual(restore[1], "restore")
        self.assertEqual(restore[restore.index("--configfile") + 1], str(config))
        self.assertEqual(restore[restore.index("--source") + 1], str(self.root / "local feed"))
        self.assertIn("-p:CoreProject=core & fixture.csproj", restore)
        self.assertIn("--no-restore", run)
        self.assertEqual(run[run.index("--") + 1 :], ["--native-library", str(library)])
        self.assertEqual(self.env["SDK_NATIVE_LIBRARY"], "accepted-native")

    def test_managed_implicit_restore_preserves_runtime_and_core_selection(self):
        bindings.run_managed(
            "dotnet",
            "Provider.csproj",
            env=self.env,
            cwd=self.root,
            runtime="win-arm64",
            properties={"CoreProject": "Pinned.csproj"},
            arguments=("--native",),
            runner=self.run_command,
        )
        self.assertEqual(len(self.calls), 1)
        argv, _ = self.calls[0]
        self.assertNotIn("--no-restore", argv)
        self.assertIn("-p:RuntimeIdentifier=win-arm64", argv)
        self.assertIn("-p:CoreProject=Pinned.csproj", argv)
        self.assertEqual(argv[-2:], ["--", "--native"])

    def test_restore_failure_prevents_run_or_pack(self):
        def failed(argv, **options):
            self.run_command(argv, **options)
            raise subprocess.CalledProcessError(23, argv)

        for operation in (bindings.run_managed, bindings.pack_project):
            self.calls.clear()
            kwargs = (
                {"output": self.root / "packages"} if operation is bindings.pack_project else {}
            )
            with (
                self.subTest(operation=operation.__name__),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                operation(
                    "dotnet",
                    "Owned.csproj",
                    env=self.env,
                    cwd=self.root,
                    nuget_config=self.root / "NuGet.Config",
                    runner=failed,
                    **kwargs,
                )
            self.assertEqual(len(self.calls), 1)
            self.assertEqual(self.calls[0][0][1], "restore")

    def test_pack_keeps_explicit_version_runtime_and_external_targets(self):
        properties = {
            "Version": "1.2.0-owned",
            "Runtime": "win-arm64",
            "CustomAfterMicrosoftCommonTargets": "core & vendor.targets",
        }
        bindings.pack_project(
            "dotnet",
            "Owned.csproj",
            output=self.root / "feed",
            env=self.env,
            cwd=self.root,
            nuget_config=self.root / "NuGet.Config",
            properties=properties,
            runner=self.run_command,
        )
        self.assertEqual([call[0][1] for call in self.calls], ["restore", "pack"])
        argv, _ = self.calls[-1]
        self.assertIn("--no-restore", argv)
        for name, value in properties.items():
            self.assertIn("-p:" + name + "=" + value, argv)
        self.assertEqual(argv[argv.index("--output") + 1], str(self.root / "feed"))

    def test_relative_package_output_is_owned_by_command_working_directory(self):
        bindings.pack_project(
            "dotnet",
            "Core.csproj",
            output="packages",
            env=self.env,
            cwd=self.root,
            runner=self.run_command,
        )
        argv, _ = self.calls[0]
        self.assertEqual(
            Path(argv[argv.index("--output") + 1]).resolve(), (self.root / "packages").resolve()
        )
        self.assertTrue((self.root / "packages").is_dir())

    def test_pack_can_keep_project_implicit_restore_policy(self):
        bindings.pack_project(
            "dotnet",
            "Core.csproj",
            output=self.root / "packages",
            env=self.env,
            cwd=self.root,
            properties={"PublishRepositoryUrl": "false"},
            runner=self.run_command,
        )
        self.assertEqual(len(self.calls), 1)
        self.assertNotIn("--no-restore", self.calls[0][0])
        self.assertIn("-p:PublishRepositoryUrl=false", self.calls[0][0])

    def test_native_staging_reads_framework_symlink_without_modifying_source(self):
        native = self.file("Native.framework/Versions/A/Native", b"accepted native")
        link = self.root / "Native.framework/Native"
        self.symlink(link, "Versions/A/Native")
        before = native.read_bytes()
        destination = self.root / "runtime/native.dll"
        self.assertEqual(bindings.stage_native(link, destination), destination)
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
                bindings.stage_native(native, destination)
        self.assertEqual(native.read_bytes(), b"owned bytes")

    def test_bad_native_copy_preserves_previous_staging_and_cleans_temporary(self):
        native = self.file("accepted.dll", b"new accepted")
        destination = self.file("runtime/native.dll", b"previous accepted")

        def corrupt(source, target):
            Path(target).write_bytes(b"bad copy")

        with (
            mock.patch.object(bindings.shutil, "copyfile", side_effect=corrupt),
            self.assertRaises(ValueError),
        ):
            bindings.stage_native(native, destination)
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
            mock.patch.object(bindings.shutil, "copyfile", side_effect=changed),
            self.assertRaises(ValueError),
        ):
            bindings.stage_native(native, destination)
        self.assertFalse(destination.exists())

    def test_package_audit_checks_selected_bytes_and_retained_notices(self):
        native = self.file("accepted.dll", b"native")
        notice = self.file("LicenseKit/LICENSE", b"upstream text")
        member = "runtimes/win-arm64/native/accepted.dll"
        package = self.package(
            {
                "Example.nuspec": b'<package xmlns="urn:nuget"><metadata><id>Example</id></metadata></package>',
                member: native.read_bytes(),
                "licenses/LicenseKit/LICENSE": notice.read_bytes(),
            }
        )
        result = bindings.audit_package(
            package,
            runtime_files={member: native},
            expected_files={"licenses/LicenseKit/LICENSE": notice},
        )
        self.assertEqual(result, hashlib.sha256(package.read_bytes()).hexdigest())

    def test_package_audit_rejects_wrong_native_bytes_or_selected_runtime_set(self):
        native = self.file("accepted.dll", b"native")
        member = "runtimes/win-arm64/native/accepted.dll"
        for members in (
            {member: b"different"},
            {member: b"native", "runtimes/win-x64/native/accepted.dll": b"native"},
            {"runtimes/win-x64/native/accepted.dll": b"native"},
        ):
            with self.subTest(members=list(members)), self.assertRaises(ValueError):
                bindings.audit_package(self.package(members), runtime_files={member: native})

    def test_package_audit_requires_both_selected_sources_for_a_runtime_member(self):
        native = self.file("accepted.dll", b"native")
        different = self.file("different.dll", b"other bytes")
        member = "runtimes/win-arm64/native/accepted.dll"
        package = self.package({member: native.read_bytes()})
        for expected, runtime in ((native, different), (different, native)):
            with (
                self.subTest(expected=expected.name),
                self.assertRaisesRegex(ValueError, "Packaged bytes differ from the accepted input"),
            ):
                bindings.audit_package(
                    package, expected_files={member: expected}, runtime_files={member: runtime}
                )

    def test_package_audit_rejects_missing_or_changed_required_notice(self):
        notice = self.file("retained/LICENSE", b"correct")
        for members in ({"LICENSE": b"correct"}, {"licenses/LICENSE": b"wrong"}):
            with self.subTest(members=list(members)), self.assertRaises(ValueError):
                bindings.audit_package(
                    self.package(members), expected_files={"licenses/LICENSE": notice}
                )

    def test_package_audit_rejects_sources_debug_and_ownership_metadata(self):
        for name in (
            "lib/Example.PDB",
            "build/code.cs",
            "src/code.rs",
            "tools/run.py",
            "licenses/registry-notices.json",
        ):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bindings.audit_package(self.package({name: b"not distributable"}))

    def test_private_content_failure_does_not_echo_suspect_bytes(self):
        samples = (
            b"/Users/Someone/private-work",
            b"C:\\Users\\Someone\\private-work",
            b".codex/private",
            b"-----BEGIN PRIVATE KEY secret",
        )
        for data in samples:
            with self.subTest(data=data):
                with self.assertRaises(ValueError) as error:
                    bindings.audit_package(self.package({"lib/Example.dll": data}))
                self.assertNotIn(data.decode(), str(error.exception))
                self.assertIn("lib/Example.dll", str(error.exception))

    def test_package_audit_keeps_policy_and_exact_input_comparison_across_chunks(self):
        name = "runtimes/osx-arm64/native/example.dylib"
        clean = b"normal bytes C:\\Users\\" + b"x" * 4096 + b"\0outside"
        native = self.file("input", clean)
        same = self.file("second-input", clean)
        selections = {"runtime_files": {name: native}, "expected_files": {name: same}}
        with mock.patch.object(bindings, "_PACKAGE_CHUNK_BYTES", 17):
            package = self.package({name: clean})
            self.assertEqual(
                bindings.audit_package(package, **selections),
                hashlib.sha256(package.read_bytes()).hexdigest(),
            )
            for different in (clean[:-1], clean + b"!", clean[:-1] + b"!"):
                same.write_bytes(different)
                with self.assertRaisesRegex(ValueError, "Packaged bytes differ"):
                    bindings.audit_package(package, **selections)
            for private in (
                b"/UsErS/path",
                b".CoDeX\\session",
                b"-----BeGiN OpEnSsH PrIvAtE KeY",
                b"D:\\uSeRs\\" + b"x" * 4096 + b"\\secret",
                b"D:\\uSeRs\\\0invalid E:\\Users\\y\\secret",
            ):
                # The split prefix, arbitrarily long username, and private
                # result after a selected source mismatch must all be seen.
                with self.assertRaisesRegex(ValueError, "Private path/material"):
                    bindings.audit_package(self.package({name: b"-" * 16 + private}), **selections)

    def test_package_audit_does_not_skip_crc_after_a_private_marker(self):
        name = "lib/Example.dll"
        data = b"/Users/private/" + b"-" * (32 * 1024)
        package = self.package({name: data})
        archive = package.read_bytes()
        tail = archive.index(data) + len(data) - 1
        package.write_bytes(archive[:tail] + b"!" + archive[tail + 1 :])
        with (
            mock.patch.object(bindings, "_PACKAGE_CHUNK_BYTES", 17),
            self.assertRaisesRegex(zipfile.BadZipFile, "Bad CRC"),
        ):
            bindings.audit_package(package)

    def test_package_audit_handles_custom_and_empty_marker_sets(self):
        member = "lib/Example.dll"
        package = self.package({member: b"-" * 8 + b"test-custom-marker"})
        with mock.patch.object(bindings, "_PACKAGE_CHUNK_BYTES", 9):
            bindings.audit_package(package, forbidden_markers=())
            with self.assertRaisesRegex(ValueError, "Private path/material"):
                bindings.audit_package(package, forbidden_markers=(b"TEST-CUSTOM-MARKER",))
            with self.assertRaisesRegex(ValueError, "Private path/material"):
                bindings.audit_package(self.package({member: b""}), forbidden_markers=(b"",))

    def test_nuspec_policy_is_explicit_and_namespace_aware(self):
        package = self.package(
            {
                "Example.nuspec": b'<package xmlns="urn:nuget"><metadata><projectUrl>https://example.invalid</projectUrl></metadata></package>'
            }
        )
        with self.assertRaises(ValueError):
            bindings.audit_package(package)
        bindings.audit_package(package, forbidden_nuspec_elements=("repository",))
        package = self.package(
            {
                "Example.nuspec": b'<package><metadata><repository url="private" /></metadata></package>'
            }
        )
        with self.assertRaises(ValueError):
            bindings.audit_package(package, forbidden_nuspec_elements=("repository",))

    def test_ambiguous_or_escaping_package_members_are_rejected(self):
        for name in ("../native.dll", "/native.dll", "runtime\\native.dll"):
            with self.subTest(name=name), self.assertRaises(ValueError):
                bindings.audit_package(self.raw_member_package(name))
        package = self.package({"LICENSE": b"first"})
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(package, "a") as archive:
                archive.writestr("LICENSE", b"second")
        with self.assertRaises(ValueError):
            bindings.audit_package(package)

    def test_package_audit_rejects_members_that_alias_on_consumer_filesystems(self):
        for name in ("lib/./sdk.dll", "lib//sdk.dll", "C:/sdk.dll", "lib/sdk.dll."):
            with self.subTest(name=name), self.assertRaisesRegex(ValueError, "Invalid package"):
                bindings.audit_package(self.raw_member_package(name))
        package = self.package({"lib/SDK.dll": b"first", "lib/sdk.dll": b"second"})
        with self.assertRaisesRegex(ValueError, "aliased package member"):
            bindings.audit_package(package)

    def test_raw_zip_backslashes_rejected_after_windows_name_normalization(self):
        package = self.raw_member_package("runtime\\native.dll")
        with mock.patch.object(zipfile.os, "sep", "\\"):
            with zipfile.ZipFile(package) as archive:
                entry = archive.infolist()[0]
                self.assertEqual(entry.orig_filename, "runtime\\native.dll")
                self.assertEqual(entry.filename, "runtime/native.dll")
            with self.assertRaises(ValueError):
                bindings.audit_package(package)

    def test_isolated_consumer_uses_exact_local_feed_source_and_clean_environment(self):
        core = self.package(name="Core.1.0.0.nupkg")
        vendor = self.package(name="Vendor.2.0.0.nupkg")
        parent = self.root / "owned output"
        excluded = self.root / "source/target"
        tools = self.root / "tools"
        lookalike = self.root / "source/target-other"
        env = dict(
            self.env,
            PATH=os.pathsep.join(
                str(p) for p in (excluded, excluded / "release", tools, lookalike)
            ),
            DYLD_LIBRARY_PATH="source dylibs",
            LD_LIBRARY_PATH="source libraries",
            DYLD_FALLBACK_LIBRARY_PATH="fallback",
            Nuget_Packages="global cache",
            RestoreAdditionalProjectSources="unexpected feed",
            restorefallbackfolders="unexpected fallback",
            SDK_NATIVE_LIBRARY="source override",
            OtherOverride="host-owned",
        )
        before = env.copy()
        source = 'using Example;\nConsole.WriteLine("λ & owned fixture");\n'
        seen = []

        def inspect(argv, **options):
            self.run_command(argv, **options)
            work = Path(options["cwd"])
            seen.append(work)
            self.assertEqual((work / "Program.cs").read_text(), source)
            for name in (
                "Directory.Build.props",
                "Directory.Build.targets",
                "Directory.Packages.props",
            ):
                settings = ET.parse(work / name).getroot()
                self.assertEqual((settings.tag, list(settings)), ("Project", []))
            self.assertEqual((work / "Directory.Build.rsp").read_bytes(), b"")
            project = ET.parse(work / "Consumer.csproj")
            self.assertEqual(project.findtext(".//RuntimeIdentifier"), "win-arm64")
            self.assertEqual(
                [
                    (p.get("Include"), p.get("Version"))
                    for p in project.findall(".//PackageReference")
                ],
                [("Vendor", "2.0.0")],
            )
            config = ET.parse(work / "NuGet.Config")
            self.assertIsNotNone(config.find("./packageSources/clear"))
            self.assertIsNotNone(config.find("./fallbackPackageFolders/clear"))
            feeds = config.findall("./packageSources/add")
            self.assertEqual(len(feeds), 1)
            feed = Path(feeds[0].get("value"))
            self.assertEqual(feed.parent, work)
            self.assertEqual({p.name for p in feed.iterdir()}, {core.name, vendor.name})
            for package in (core, vendor):
                self.assertEqual((feed / package.name).read_bytes(), package.read_bytes())
            clean = options["env"]
            self.assertEqual(clean["NUGET_PACKAGES"], str(work / "packages"))
            for key in (
                "SDK_NATIVE_LIBRARY",
                "DYLD_LIBRARY_PATH",
                "LD_LIBRARY_PATH",
                "DYLD_FALLBACK_LIBRARY_PATH",
                "Nuget_Packages",
                "RestoreAdditionalProjectSources",
                "restorefallbackfolders",
            ):
                self.assertNotIn(key, clean)
            self.assertEqual(clean["OtherOverride"], "host-owned")
            self.assertEqual(clean["PATH"].split(os.pathsep), [str(tools), str(lookalike)])
            return subprocess.CompletedProcess(argv, 0)

        bindings.run_isolated_consumer(
            "dotnet",
            packages=[core, vendor],
            references={"Vendor": "2.0.0"},
            source=source,
            work_parent=parent,
            env=env,
            runtime="win-arm64",
            clear_environment=("SDK_NATIVE_LIBRARY",),
            exclude_paths=(excluded,),
            runner=inspect,
        )
        self.assertEqual([call[0][1] for call in self.calls], ["restore", "run"])
        self.assertIn("--no-restore", self.calls[-1][0])
        self.assertEqual(env, before)
        self.assertTrue(all(not work.exists() for work in seen))

    def test_portable_consumer_and_build_only_do_not_execute_sdk_code(self):
        package = self.package()
        for execute in (True, False):
            self.calls.clear()

            def inspect(argv, **options):
                project = ET.parse(Path(options["cwd"]) / "Consumer.csproj")
                self.assertIsNone(project.find(".//RuntimeIdentifier"))
                self.assertEqual(project.findtext(".//SelfContained"), "false")
                return self.run_command(argv, **options)

            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// SDK fixture",
                work_parent=self.root / "output",
                env=self.env,
                execute=execute,
                runner=inspect,
            )
            self.assertEqual(
                [call[0][1] for call in self.calls], ["restore", "run" if execute else "build"]
            )

    def test_each_consumer_has_fresh_workspace_and_cache(self):
        package = self.package()
        workspaces = []

        def inspect(argv, **options):
            if argv[1] == "restore":
                workspaces.append(Path(options["cwd"]))
                self.assertFalse(Path(options["env"]["NUGET_PACKAGES"]).exists())
            return self.run_command(argv, **options)

        for _ in range(2):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// SDK fixture",
                work_parent=self.root / "output",
                env=self.env,
                runner=inspect,
            )
        self.assertEqual(len(set(workspaces)), 2)
        self.assertTrue(all(not path.exists() for path in workspaces))

    def test_consumer_restore_failure_propagates_and_cleans_owned_workspace(self):
        package = self.package()
        parent = self.root / "output"

        def failed(argv, **options):
            self.run_command(argv, **options)
            raise subprocess.CalledProcessError(41, argv)

        with self.assertRaises(subprocess.CalledProcessError) as error:
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=parent,
                env=self.env,
                runner=failed,
            )
        self.assertEqual(error.exception.returncode, 41)
        self.assertEqual(len(self.calls), 1)
        self.assertFalse(list(parent.iterdir()))

    def test_consumer_retries_windows_dll_cleanup_without_rerunning_commands(self):
        package = self.package()
        parent = self.root / "output"
        real_cleanup = tempfile.TemporaryDirectory.cleanup

        def cleanup(directory):
            winerror = cleanup_attempts.pop(0)
            if winerror is None:
                real_cleanup(directory)
                return
            error = PermissionError("The native library is locked")
            error.winerror = winerror
            raise error

        for winerror in (5, 32, 33):
            with self.subTest(winerror=winerror):
                self.calls.clear()
                cleanup_attempts = [winerror, None]
                with (
                    mock.patch.object(
                        tempfile.TemporaryDirectory, "cleanup", autospec=True, side_effect=cleanup
                    ),
                    mock.patch.object(workspaces.time, "sleep") as sleep,
                ):
                    result = bindings.run_isolated_consumer(
                        "dotnet",
                        packages=[package],
                        references={"Example": "1.0.0"},
                        source="// fixture",
                        work_parent=parent,
                        env=self.env,
                        runner=self.run_command,
                    )
                self.assertEqual(result.returncode, 0)
                self.assertEqual([call[0][1] for call in self.calls], ["restore", "run"])
                self.assertEqual(sleep.call_count, 1)
                self.assertFalse(cleanup_attempts)
                self.assertFalse(list(parent.iterdir()))

    def test_relative_consumer_parent_yields_absolute_child_paths(self):
        package = self.package()

        def inspect(argv, **options):
            self.assertTrue(Path(options["cwd"]).is_absolute())
            project = Path(argv[2] if argv[1] == "restore" else argv[argv.index("--project") + 1])
            self.assertTrue(project.is_absolute())
            self.assertTrue(project.is_file())
            return self.run_command(argv, **options)

        with chdir(self.root):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=Path("output"),
                env=self.env,
                runner=inspect,
            )

    def test_duplicate_feed_names_are_rejected_before_commands(self):
        first = self.package(name="one/Example.1.0.0.nupkg")
        second = self.package(name="two/example.1.0.0.nupkg")
        with self.assertRaises(ValueError):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[first, second],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=self.root / "output",
                env=self.env,
                runner=self.run_command,
            )
        self.assertFalse(self.calls)

    def test_restore_evidence_rejects_an_external_feed_cache_or_different_archive_before_run(self):
        package = self.package()
        for invalid, diagnostic in (
            ("sources", "outside its local feed"),
            ("cache", "outside its fresh cache"),
            ("archive", "Restored package differs"),
            ("extra archive", "Restored package differs"),
            ("missing archive", "did not retain its package archive"),
            ("path", "invalid cache path"),
        ):
            self.calls.clear()

            def restore(argv, **options):
                result = self.run_command(argv, **options)
                if argv[1] != "restore":
                    self.fail("Build or run started without valid restore evidence")
                work = Path(options["cwd"])
                assets_path = work / "obj/project.assets.json"
                assets = json.loads(assets_path.read_text(encoding="utf-8"))
                if invalid == "sources":
                    assets["project"]["restore"]["sources"][str(self.root / "other-feed")] = {}
                elif invalid == "cache":
                    assets["packageFolders"][str(self.root / "other-cache")] = {}
                elif invalid == "path":
                    assets["libraries"]["Example/1.0.0"]["path"] = "../1.0.0"
                elif invalid == "extra archive":
                    extra = work / "packages/outside/1.0.0/outside.1.0.0.nupkg"
                    extra.parent.mkdir(parents=True)
                    extra.write_bytes(b"unsafe implicit dependency")
                else:
                    installed = work / "packages/example/1.0.0/example.1.0.0.nupkg"
                    if invalid == "missing archive":
                        installed.unlink()
                    else:
                        installed.write_bytes(b"different consumed bytes")
                assets_path.write_text(json.dumps(assets), encoding="utf-8")
                return result

            with (
                self.subTest(invalid=invalid),
                self.assertRaisesRegex(ValueError, diagnostic),
            ):
                bindings.run_isolated_consumer(
                    "dotnet",
                    packages=[package],
                    references={"Example": "1.0.0"},
                    source="// fixture",
                    work_parent=self.root / "output",
                    env=self.env,
                    runner=restore,
                )
            self.assertEqual([argv[1] for argv, _ in self.calls], ["restore"])
            self.assertFalse(list((self.root / "output").iterdir()))

    def test_isolated_consumer_rejects_explicit_external_restore_properties(self):
        with self.assertRaisesRegex(ValueError, "isolated restore settings"):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[self.package()],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=self.root / "output",
                env=self.env,
                properties={"RESTOREadditionalProjectSources": str(self.root)},
                runner=self.run_command,
            )
        self.assertFalse(self.calls)

    def test_corrupt_local_feed_copy_fails_before_restore(self):
        package = self.package()
        parent = self.root / "output"

        def corrupt(source, target):
            Path(target).write_bytes(b"corrupt package copy")

        with (
            mock.patch.object(bindings.shutil, "copyfile", side_effect=corrupt),
            self.assertRaises(ValueError),
        ):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=parent,
                env=self.env,
                runner=self.run_command,
            )
        self.assertFalse(self.calls)
        self.assertFalse(list(parent.iterdir()))
        with zipfile.ZipFile(package) as archive:
            self.assertIn("Example.nuspec", archive.namelist())

    def test_package_changed_during_consumer_is_not_reported_as_success(self):
        package = self.package()

        def changed(argv, **options):
            self.run_command(argv, **options)
            if argv[1] == "run":
                package.write_bytes(b"changed after restore")
            return subprocess.CompletedProcess(argv, 0)

        with self.assertRaises(ValueError):
            bindings.run_isolated_consumer(
                "dotnet",
                packages=[package],
                references={"Example": "1.0.0"},
                source="// fixture",
                work_parent=self.root / "output",
                env=self.env,
                runner=changed,
            )


@unittest.skipUnless(os.environ.get("NAVI_TEST_DOTNET") == "1", "Requires the prepared .NET SDK")
class NativeConsumerTests(unittest.TestCase):
    @staticmethod
    def package(folder, name, dependency=None):
        package = folder / f"{name}.1.0.0.nupkg"
        dependencies = (
            f'<dependencies><dependency id="{dependency}" version="[1.0.0]" /></dependencies>'
            if dependency
            else ""
        )
        with zipfile.ZipFile(package, "w") as archive:
            archive.writestr(
                f"{name}.nuspec",
                f"<package><metadata><id>{name}</id><version>1.0.0</version>"
                "<authors>Fixture</authors><description>Local fixture</description>"
                f"{dependencies}</metadata></package>",
            )
            archive.writestr("lib/net10.0/_._", "")
        return package

    def test_parent_build_settings_do_not_change_a_clean_consumer(self):
        env = dict(os.environ, DOTNET_CLI_TELEMETRY_OPTOUT="1", DOTNET_NOLOGO="1")
        dotnet = env.get("DOTNET_BIN") or shutil.which("dotnet")
        self.assertIsNotNone(dotnet, "The prepared .NET SDK is required for native consumer tests")
        with tempfile.TemporaryDirectory(prefix="consumer-parent-fixture-") as directory:
            root = Path(directory)
            package = self.package(root, "IsolationFixture")
            for name, value in {
                "Directory.Build.props": '<Project><Import Project="missing-parent-props"/></Project>',
                "Directory.Build.targets": '<Project><Import Project="missing-parent-targets"/></Project>',
                "Directory.Packages.props": (
                    "<Project><PropertyGroup><ManagePackageVersionsCentrally>true"
                    "</ManagePackageVersionsCentrally></PropertyGroup></Project>"
                ),
                "Directory.Build.rsp": "/p:DefineConstants=PARENT_RESPONSE",
            }.items():
                (root / name).write_text(value, encoding="utf-8")
            bindings.run_isolated_consumer(
                dotnet,
                packages=[package],
                references={"IsolationFixture": "1.0.0"},
                source=(
                    "#if PARENT_RESPONSE\n#error Parent response leaked\n#endif\n"
                    'Console.WriteLine("Clean fixture executed");'
                ),
                work_parent=root,
                env=env,
            )

    def test_installed_consumer_only_resolves_declared_transitive_package_archives(self):
        dotnet = os.environ.get("DOTNET_BIN") or shutil.which("dotnet")
        self.assertIsNotNone(dotnet, "The prepared .NET SDK is required for native consumer tests")
        with tempfile.TemporaryDirectory(prefix="consumer-restore-fixture-") as directory:
            root = Path(directory)
            outside = root / "extra feed"
            outside.mkdir()
            dependency = self.package(outside, "ConsumerDependency")
            package = self.package(root, "ConsumerPrimary", dependency="ConsumerDependency")
            env = dict(
                os.environ,
                DOTNET_CLI_TELEMETRY_OPTOUT="1",
                DOTNET_CLI_UI_LANGUAGE="en",
                DOTNET_NOLOGO="1",
                RestoreAdditionalProjectSources=str(outside),
            )
            options = {
                "references": {"ConsumerPrimary": "1.0.0"},
                "source": 'Console.WriteLine("Verified local closure executed");',
                "work_parent": root,
                "env": env,
            }

            def capture(argv, **kwargs):
                return subprocess.run(argv, **kwargs, capture_output=True, text=True)

            with self.assertRaises(subprocess.CalledProcessError) as error:
                bindings.run_isolated_consumer(
                    dotnet, packages=[package], runner=capture, **options
                )
            self.assertEqual(error.exception.cmd[1], "restore")
            output = error.exception.stdout + error.exception.stderr
            self.assertIn("NU1101", output)
            self.assertIn("ConsumerDependency", output)

            for runtime in (None, "win-arm64"):
                with self.subTest(runtime=runtime):
                    result = bindings.run_isolated_consumer(
                        dotnet,
                        packages=[package, dependency],
                        runner=capture,
                        runtime=runtime,
                        execute=runtime is None,
                        **options,
                    )
                    if runtime is None:
                        self.assertIn("Verified local closure executed", result.stdout)


if __name__ == "__main__":
    unittest.main()
