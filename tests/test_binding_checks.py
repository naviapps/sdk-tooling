import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sdk_tooling import binding_checks


class BindingChecksTests(unittest.TestCase):
    def test_x64_package_consumers_get_each_fixture_and_preserve_user_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "sdk-checks.toml").write_text(
                """[bindings]
nuget_id = "Example.Bindings"
nuget_fixtures = ["fixtures/public.cs", "fixtures/trimming.cs"]
windows_x64 = true
""",
                encoding="utf-8",
            )
            options = ["package.nupkg", "--dependency", "dependency.nupkg"]
            check = Mock()
            with (
                patch.object(binding_checks.sys, "platform", "win32"),
                patch.object(binding_checks.consumers, "check_nuget", check),
            ):
                binding_checks.run_consumer("nuget", root=root, argv=options)
            self.assertEqual(options, ["package.nupkg", "--dependency", "dependency.nupkg"])
            self.assertEqual(
                [call.kwargs["fixture"] for call in check.call_args_list],
                [root / "fixtures/public.cs", root / "fixtures/trimming.cs"],
            )
            for call in check.call_args_list:
                self.assertEqual(call.kwargs["argv"], [*options, "--windows-architecture", "x64"])
                self.assertEqual(call.kwargs["package_id"], "Example.Bindings")
                self.assertEqual(call.kwargs["work_parent"], root / ".native-build/packages")

    def test_portable_binding_does_not_add_a_windows_target_argument(self):
        args = ["--library", "libExample.dll"]
        with patch.object(binding_checks.sys, "platform", "win32"):
            self.assertEqual(binding_checks._options(args, windows_x64=False), args)

    def test_framework_bundle_is_selected_from_the_installed_consumer_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "sdk-checks.toml").write_text(
                """[bindings]
abi_fixture = "ffi/tests/ownership.c"
ffi = "ffi"

[installed.c]
framework = "Artifacts/CExample.framework"
""",
                encoding="utf-8",
            )
            check = Mock()
            with patch.object(binding_checks.consumers, "check_c", check):
                binding_checks.run_consumer("c", root=root, argv=[])
            check.assert_called_once_with(
                source=root / "ffi/tests/ownership.c",
                include_dirs=(root / "ffi/include",),
                library=root / "Artifacts/CExample.framework/CExample",
                work_parent=root / ".native-build",
                argv=[],
            )
