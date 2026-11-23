"""The Swift fixture sees only distributed inputs and fresh, disposable build state."""

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import consumers


class SwiftConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(self.temporary).resolve()
        self.sdk = self.root / "developer-sdk"
        self.fixture = self.root / "fixture"
        self.work = self.root / "work"
        self.work.mkdir()
        self.write(self.sdk, "Package.swift", b"SDK manifest")
        self.write(self.sdk, "Sources/Library/Library.swift", b"public API")
        self.write(self.sdk, "Sources/Library/Assets.bundle/value", b"resource bytes")
        self.write(self.sdk, "ThirdParty/NOTICE.txt", b"original upstream notice\n")
        self.write(self.sdk, "Artifacts/Native.framework/Versions/A/Native", b"native bytes")
        self.write(
            self.fixture, "Package.swift", b"SDK-owned manifest with ../sdk product selection"
        )
        self.write(self.fixture, "Sources/Fixture/main.swift", b"SDK-owned assertions")
        self.inputs = ("Package.swift", "Sources", "Artifacts", "ThirdParty")
        self.calls = []
        self.environment = self.enterContext(patch.dict(os.environ))
        os.environ.pop("GIT_CONFIG_GLOBAL", None)

    @staticmethod
    def write(root, name, data):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def command(self, argv, **options):
        self.calls.append((argv, options))
        if "show-dependencies" in argv:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps(
                    {
                        "path": str(options["cwd"]),
                        "dependencies": [
                            {"path": str(options["cwd"].parent / "sdk"), "dependencies": []}
                        ],
                    }
                ),
            )
        if "--show-bin-path" in argv:
            scratch = Path(argv[argv.index("--scratch-path") + 1])
            binary = scratch / "arm64-apple-macosx/debug"
            binary.mkdir(parents=True)
            return subprocess.CompletedProcess(argv, 0, stdout=str(binary) + "\n")
        return subprocess.CompletedProcess(argv, 0)

    def check(self, **options):
        consumers.run_swift_consumer(
            root=self.sdk,
            inputs=self.inputs,
            fixture=self.fixture,
            executables=("Fixture",),
            work_parent=self.work,
            runner=options.pop("runner", self.command),
            **options,
        )

    def link(self, path, target, *, target_is_directory=False):
        try:
            path.symlink_to(Path(target), target_is_directory=target_is_directory)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation; no privilege changes attempted")
            raise

    def test_installed_tree_preserves_native_links_resources_notices_and_fixture_policy(self):
        self.link(
            self.sdk / "Artifacts/Native.framework/Versions/Current", "A", target_is_directory=True
        )
        self.link(self.sdk / "Artifacts/Native.framework/Native", "Versions/Current/Native")
        for name in ("tools/private.py", "crates/core/src/lib.rs", ".git/config", ".build/cache"):
            self.write(self.sdk, name, b"developer only")
        self.write(self.sdk, "Sources/Library/.build/stale.swift", b"cached")
        for name in ("bin", "obj", "build", "tools", "crates", "target"):
            self.write(self.sdk, "Sources/Library/Assets.bundle/" + name, b"installed resource")
        self.write(self.fixture, ".build/stale", b"cached")

        def inspect(argv, **options):
            consumer = options["cwd"]
            installed = consumer.parent / "sdk"
            self.assertEqual(
                (consumer / "Package.swift").read_bytes(),
                (self.fixture / "Package.swift").read_bytes(),
            )
            for name in (
                "Package.swift",
                "Sources/Library/Library.swift",
                "Sources/Library/Assets.bundle/value",
                "Artifacts/Native.framework/Native",
                "ThirdParty/NOTICE.txt",
            ):
                self.assertEqual((installed / name).read_bytes(), (self.sdk / name).read_bytes())
            self.assertTrue((installed / "Artifacts/Native.framework/Native").is_symlink())
            for name in ("bin", "obj", "build", "tools", "crates", "target"):
                self.assertEqual(
                    (installed / "Sources/Library/Assets.bundle" / name).read_bytes(),
                    b"installed resource",
                )
            for name in ("tools", "crates", ".git", ".build", "Sources/Library/.build"):
                self.assertFalse((installed / name).exists())
            self.assertFalse((consumer / ".build").exists())
            return self.command(argv, **options)

        self.check(runner=inspect)
        self.assertEqual(list(self.work.iterdir()), [])
        self.assertFalse((self.sdk / ".swiftpm").exists())

    def test_preserves_developer_home_while_isolating_swift_state_and_tools(self):
        inherited = {
            "HOME": "/developer/home",
            "CFFIXED_USER_HOME": "/developer/core-foundation-home",
            "PATH": "/developer/cargo/bin:/developer/tooling/bin",
            "DEVELOPER_DIR": "/Applications/Selected Xcode.app/Contents/Developer",
            "DYLD_FRAMEWORK_PATH": "/developer/native",
            "LD_LIBRARY_PATH": "/developer/native",
            "PRESET_DOCUMENTS_NATIVE_LIBRARY": "/developer/lib.dylib",
            "SDKROOT": "/developer/sdk",
            "SWIFT_EXEC": "/developer/swift",
            "SWIFTPM_BUILD_DIR": "/developer/build",
            "SWIFTPM_MODULECACHE_OVERRIDE": "/developer/modules",
            "CPATH": "/developer/headers",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "unsafe",
            "GIT_CONFIG_VALUE_0": "value",
        }
        os.environ.update(inherited)
        self.check()
        self.assertEqual(len(self.calls), 4)
        graph, build, show_binary, execute = self.calls
        self.assertEqual(graph[0][:3], ["/usr/bin/xcrun", "swift", "package"])
        self.assertEqual(graph[0][-3:], ["show-dependencies", "--format", "json"])
        self.assertEqual(build[0][:3], ["/usr/bin/xcrun", "swift", "build"])
        self.assertNotIn("--build-system", build[0])
        self.assertEqual(show_binary[0], [*build[0], "--show-bin-path"])
        for option in ("--scratch-path", "--cache-path", "--config-path", "--security-path"):
            self.assertTrue(Path(build[0][build[0].index(option) + 1]).is_relative_to(self.work))
        self.assertIn("--disable-dependency-cache", build[0])
        self.assertIn("none", build[0])
        self.assertIn("-strict-concurrency=complete", build[0])
        self.assertIn("-warnings-as-errors", build[0])
        self.assertNotIn("--force-resolved-versions", build[0])
        self.assertTrue(Path(execute[0][0]).is_relative_to(self.work))
        env = build[1]["env"]
        for _, options in self.calls:
            self.assertIs(options["env"], env)
            self.assertTrue(options["check"])
        for key in inherited.keys() - {"HOME", "CFFIXED_USER_HOME", "PATH", "DEVELOPER_DIR"}:
            self.assertNotIn(key, env)
        for key in ("HOME", "CFFIXED_USER_HOME"):
            self.assertEqual(env[key], inherited[key])
        self.assertFalse((build[1]["cwd"].parent / "home").exists())
        self.assertEqual(env["DEVELOPER_DIR"], inherited["DEVELOPER_DIR"])
        self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
        self.assertEqual(env["GIT_CONFIG_NOSYSTEM"], "1")
        for key in ("XDG_CACHE_HOME", "XDG_CONFIG_HOME", "TMPDIR", "SWIFT_MODULECACHE_PATH"):
            self.assertTrue(Path(env[key]).is_relative_to(self.work))
        self.assertEqual(list(self.work.iterdir()), [])

    def test_cli_can_select_swift_build_and_keep_dependency_warnings_nonfatal(self):
        run = consumers.run_swift_consumer

        def run_with_test_command(**options):
            run(**options, runner=self.command)

        with (
            patch.object(consumers.sys, "platform", "darwin"),
            patch.object(consumers, "run_swift_consumer", side_effect=run_with_test_command),
        ):
            consumers.check_swift(
                root=self.sdk,
                inputs=self.inputs,
                fixture=self.fixture,
                executables=("Fixture",),
                work_parent=self.work,
                argv=["--build-system", "swiftbuild", "--allow-warnings"],
            )

        self.assertEqual(len(self.calls), 4)
        for argv, _ in self.calls[:3]:
            backend = argv.index("--build-system")
            self.assertEqual(argv[backend + 1], "swiftbuild")
            self.assertIn("-strict-concurrency=complete", argv)
            self.assertNotIn("-warnings-as-errors", argv)

    def test_does_not_invent_a_core_foundation_home_override(self):
        os.environ.pop("CFFIXED_USER_HOME", None)
        self.check()
        for _, options in self.calls:
            self.assertNotIn("CFFIXED_USER_HOME", options["env"])

    def test_root_pins_and_explicit_mirrors_survive_at_consumer_root_without_rewriting(self):
        pins = b'{"version":3,"pins":[{"revision":"immutable-dependency"}]}\n'
        mirrors = (
            b'{"version":1,"object":[{"original":"remote","mirror":"file:///job/local.git"}]}\n'
        )
        self.write(self.sdk, "Package.resolved", pins)
        self.write(self.sdk, ".swiftpm/configuration/mirrors.json", mirrors)
        git_config = self.write(
            self.root, "job/gitconfig", b'[url "file:///job/local.git"]\n insteadOf = remote\n'
        )
        os.environ["GIT_CONFIG_GLOBAL"] = str(git_config)

        def inspect(argv, **options):
            consumer = options["cwd"]
            self.assertEqual((consumer / "Package.resolved").read_bytes(), pins)
            self.assertEqual(
                (consumer / ".swiftpm/configuration/mirrors.json").read_bytes(), mirrors
            )
            self.assertEqual(
                Path(options["env"]["GIT_CONFIG_GLOBAL"]).read_bytes(), git_config.read_bytes()
            )
            self.assertNotEqual(Path(options["env"]["GIT_CONFIG_GLOBAL"]), git_config)
            if "build" in argv:
                self.assertIn("--force-resolved-versions", argv)
            return self.command(argv, **options)

        self.check(runner=inspect)
        self.assertEqual((self.sdk / "Package.resolved").read_bytes(), pins)

    def test_conflicting_fixture_pins_fail_and_cleanup_before_any_build(self):
        self.write(self.sdk, "Package.resolved", b"sdk pins")
        self.write(self.fixture, "Package.resolved", b"other pins")
        with self.assertRaisesRegex(ValueError, "Conflicting"):
            self.check()
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.work.iterdir()), [])

    def test_explicit_git_and_swift_mirrors_do_not_require_user_configuration(self):
        git_config = self.write(self.root, "explicit/gitconfig", b"explicit Git policy")
        mirror = self.write(self.root, "explicit/mirrors.json", b"explicit Swift policy")

        def inspect(argv, **options):
            self.assertEqual(
                Path(options["env"]["GIT_CONFIG_GLOBAL"]).read_bytes(), git_config.read_bytes()
            )
            self.assertEqual(
                (options["cwd"] / ".swiftpm/configuration/mirrors.json").read_bytes(),
                mirror.read_bytes(),
            )
            return self.command(argv, **options)

        self.check(runner=inspect, git_config=git_config, mirror_config=mirror)

    def test_escaping_relative_and_absolute_links_fail_before_any_build(self):
        for target in (
            str(self.sdk / "Sources/Library/Library.swift"),
            "../../../../developer-sdk/Sources/Library/Library.swift",
        ):
            with self.subTest(target=target):
                link = self.sdk / "Sources/Library/linked.swift"
                self.link(link, target)
                try:
                    with self.assertRaises((ValueError, FileNotFoundError)):
                        self.check()
                finally:
                    link.unlink()
                self.assertEqual(self.calls, [])
                self.assertEqual(list(self.work.iterdir()), [])

    def test_declaring_developer_inputs_is_rejected(self):
        for name in ("tools", "crates", "target", "../outside", str(self.sdk)):
            with self.subTest(name=name):
                self.inputs = ("Package.swift", name)
                with self.assertRaisesRegex(ValueError, "exclude developer trees"):
                    self.check()
                self.assertEqual(self.calls, [])
                self.assertEqual(list(self.work.iterdir()), [])

    def test_build_and_runtime_failures_remove_disposable_source_and_cache(self):
        for stage in ("build", "runtime"):
            with self.subTest(stage=stage):
                self.calls.clear()

                def failed(argv, **options):
                    if (stage == "build" and "build" in argv) or (
                        stage == "runtime" and len(argv) == 1
                    ):
                        self.write(options["cwd"].parent, "cache/partial", b"failed output")
                        raise subprocess.CalledProcessError(1, argv)
                    return self.command(argv, **options)

                with self.assertRaises(subprocess.CalledProcessError):
                    self.check(runner=failed)
                self.assertEqual(list(self.work.iterdir()), [])
                self.assertTrue((self.sdk / "Package.swift").is_file())

    def test_swift_cannot_select_an_existing_developer_binary_directory(self):
        def escaped(argv, **options):
            if "--show-bin-path" in argv:
                return subprocess.CompletedProcess(argv, 0, stdout=str(self.sdk) + "\n")
            return self.command(argv, **options)

        with self.assertRaisesRegex(ValueError, "fresh scratch tree"):
            self.check(runner=escaped)
        self.assertEqual(list(self.work.iterdir()), [])

    def test_transitive_local_dependency_cannot_escape_to_developer_checkout(self):
        def escaped(argv, **options):
            if "show-dependencies" in argv:
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    stdout=json.dumps(
                        {
                            "path": str(options["cwd"]),
                            "dependencies": [{"path": str(self.sdk), "dependencies": []}],
                        }
                    ),
                )
            return self.command(argv, **options)

        with self.assertRaisesRegex(ValueError, "dependency escapes"):
            self.check(runner=escaped)
        self.assertEqual(self.calls, [])
        self.assertEqual(list(self.work.iterdir()), [])


if __name__ == "__main__":
    unittest.main()
