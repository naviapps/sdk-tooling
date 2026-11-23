"""Verify repository-owned inputs are passed to the shared SDK checks."""

import argparse
import json
import os
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

from sdk_tooling import sdk_checks


class SDKChecksTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.parser = argparse.ArgumentParser()
        sdk_checks.add_commands(self.parser.add_subparsers(required=True))

    def write(self, relative, text):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(text), encoding="utf-8")
        return path

    def check(self, *arguments):
        options = self.parser.parse_args(arguments)
        options.handler(options, root=self.root)

    def test_detailed_swift_consumer_uses_owned_fixture_and_inputs(self):
        self.write("Package.swift", "// SDK manifest")
        fixture = self.write(
            "fixtures/consumers/swift/Package.swift", "// consumer manifest"
        ).parent
        self.write(
            "sdk-checks.toml",
            """
            [swift]
            executable = "SDKConsumer"
            inputs = ["Package.swift"]
            """,
        )
        with (
            patch("sys.platform", "darwin"),
            patch("sdk_tooling.consumers.swiftpm.run_swift_consumer") as run,
        ):
            self.check(
                "swift-consumer",
                "--distribution",
                "--build-system",
                "swiftbuild",
                "--allow-warnings",
            )
        run.assert_called_once()
        self.assertEqual(run.call_args.kwargs["root"], self.root)
        self.assertEqual(run.call_args.kwargs["fixture"], fixture)
        self.assertEqual(list(run.call_args.kwargs["inputs"]), ["Package.swift"])
        self.assertEqual(list(run.call_args.kwargs["executables"]), ["SDKConsumer"])
        self.assertTrue(run.call_args.kwargs["verify_dependencies"])
        self.assertEqual(run.call_args.kwargs["build_system"], "swiftbuild")
        self.assertFalse(run.call_args.kwargs["warnings_as_errors"])

    def test_swift_dependency_verification_is_opt_in_per_sdk(self):
        for enabled in (False, True):
            with self.subTest(verify_dependencies=enabled):
                self.write(
                    "sdk-checks.toml",
                    f'[swift]\nexecutable = "Example"\ninputs = ["Package.swift"]\n'
                    f"verify_dependencies = {str(enabled).lower()}\n",
                )
                with (
                    patch("sys.platform", "darwin"),
                    patch("sdk_tooling.consumers.swiftpm.run_swift_consumer") as run,
                ):
                    self.check("swift-consumer")
                self.assertEqual(run.call_args.kwargs["verify_dependencies"], enabled)

    def test_x64_package_consumers_get_each_fixture_and_preserve_user_inputs(self):
        self.write(
            "sdk-checks.toml",
            """
            [nuget]
            package_id = "Example.Bindings"
            fixtures = ["fixtures/public.cs", "fixtures/trimming.cs"]
            windows_x64 = true
            """,
        )
        options = self.parser.parse_args(
            ["nuget-consumer", "package.nupkg", "--dependency", "dependency.nupkg"]
        )
        with (
            patch("sys.platform", "win32"),
            patch("sdk_tooling.consumers.nuget.check_nuget") as check,
        ):
            options.handler(options, root=self.root)
        self.assertEqual(options.package, Path("package.nupkg"))
        self.assertEqual(options.dependency, [Path("dependency.nupkg")])
        self.assertIsNone(options.windows_architecture)
        self.assertEqual(
            [call.kwargs["fixture"] for call in check.call_args_list],
            [self.root / "fixtures/public.cs", self.root / "fixtures/trimming.cs"],
        )
        for call in check.call_args_list:
            arguments = call.kwargs["args"]
            self.assertEqual(arguments.package, options.package)
            self.assertEqual(arguments.dependency, options.dependency)
            self.assertEqual(arguments.windows_architecture, "x64")
            self.assertEqual(call.kwargs["package_id"], "Example.Bindings")
            self.assertEqual(call.kwargs["work_parent"], self.root / ".native-build/packages")

    def test_docs_find_only_owned_catalogs_and_use_declared_settings(self):
        first = self.write("Sources/First/First.docc/First.md", "First").parent
        second = self.write("Sources/Second/Second.docc/Second.md", "Second").parent
        self.write("Tests/Fixtures/NotPublic.docc/Fixture.md", "Internal only")
        self.write(
            "sdk-checks.toml",
            """
            [docs]
            retain_archives = true
            graph_arguments = ["--skip-synthesized-members"]
            """,
        )
        with patch("sdk_tooling.docs.check_catalogs") as check_catalogs:
            self.check("docs")
        check_catalogs.assert_called_once()
        options = check_catalogs.call_args.kwargs
        self.assertEqual(check_catalogs.call_args.args[0], self.root)
        self.assertEqual(list(check_catalogs.call_args.args[1]), [first, second])
        self.assertEqual(options["output"], self.root / "build/docs")
        self.assertEqual(list(options["graph_arguments"]), ["--skip-synthesized-members"])
        self.assertTrue(options["retain_archives"])

    def test_docs_reject_output_outside_checkout(self):
        self.write("Sources/Module/Module.docc/Module.md", "Module")
        self.write("sdk-checks.toml", '[docs]\noutput = "../outside"\n')
        with patch("sdk_tooling.docs.check_catalogs") as check_catalogs:
            with self.assertRaises(ValueError):
                self.check("docs")
            check_catalogs.assert_not_called()

    def test_docs_reject_symlinked_output_outside_checkout(self):
        self.write("Sources/Module/Module.docc/Module.md", "Module")
        self.write("sdk-checks.toml", '[docs]\noutput = "outside-link/archives"\n')
        output = self.root / "outside-link"
        with tempfile.TemporaryDirectory() as outside:
            try:
                output.symlink_to(outside, target_is_directory=True)
            except OSError as error:
                if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                    self.skipTest("Windows runner cannot create directory symlinks")
                raise
            with patch("sdk_tooling.docs.check_catalogs") as check_catalogs:
                with self.assertRaises(ValueError):
                    self.check("docs")
                check_catalogs.assert_not_called()

    def test_installed_consumer_resolves_cargo_pins_and_supplies_owned_c_options(self):
        self.write("Artifacts/CExample.framework/Headers/example.h", "/* SDK header */")
        self.write(
            "Cargo.toml",
            """
            [workspace.dependencies]
            support-crate = { git = "https://github.com/sdk-team/support-crate.git", rev = "abc" }
            """,
        )
        self.write(
            "Cargo.lock",
            """
            version = 4
            [[package]]
            name = "tokio"
            version = "1.49.0"
            source = "registry+https://github.com/rust-lang/crates.io-index"

            [[package]]
            name = "tokio"
            version = "0.1.0"
            """,
        )
        self.write(
            "sdk-checks.toml",
            """
            [rust_c]
            git_url = "https://github.com/sdk-team/example-sdk.git"
            rust_packages = ["example-sdk"]
            rust_git_dependencies = ["support-crate"]
            rust_registry_dependencies = {tokio = ["rt"]}

            [rust_c.c]
            framework = "Artifacts/CExample.framework"
            header = "example.h"
            options = {path = "{owned}/document.preset", maximumByteCount = 4096}
            """,
        )
        with patch("sdk_tooling.consumers.rust_c.check_installed") as check_installed:
            self.check("rust-c-consumers", "--language", "c", "--revision", "a" * 40)
        check_installed.assert_called_once()
        options = check_installed.call_args.kwargs
        self.assertEqual(options["root"], self.root)
        self.assertEqual(options["args"].language, ["c"])
        self.assertEqual(options["args"].revision, "a" * 40)
        self.assertEqual(options["git_url"], "https://github.com/sdk-team/example-sdk.git")
        self.assertEqual(list(options["rust_packages"]), ["example-sdk"])
        self.assertEqual(
            options["rust_git_dependencies"],
            {
                "support-crate": {
                    "git": "https://github.com/sdk-team/support-crate.git",
                    "rev": "abc",
                }
            },
        )
        self.assertEqual(
            options["rust_registry_dependencies"],
            {"tokio": {"version": "=1.49.0", "features": ["rt"]}},
        )
        self.assertEqual(options["framework"], "Artifacts/CExample.framework")
        self.assertEqual(options["header"], "example.h")
        client, owned = self.root / "client", self.root / "owned"
        client.mkdir()
        owned.mkdir()
        (options_file,) = options["c_arguments"](client, owned)
        self.assertEqual(options_file, client / "file-options.json")
        self.assertEqual(
            json.loads(options_file.read_text(encoding="utf-8")),
            {"path": f"{owned}/document.preset", "maximumByteCount": 4096},
        )


if __name__ == "__main__":
    unittest.main()
