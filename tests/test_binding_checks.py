import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from sdk_tooling import binding_checks, check_options


class BindingChecksTests(unittest.TestCase):
    def test_portable_binding_does_not_add_a_windows_target_argument(self):
        args = ["--library", "libExample.dll"]
        with patch("sys.platform", "win32"):
            self.assertEqual(check_options.windows_x64_options(args, windows_x64=False), args)

    def test_framework_bundle_is_selected_from_the_installed_consumer_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "sdk-checks.toml").write_text(
                """[bindings]
abi_fixture = "ffi/tests/ownership.c"
ffi = "ffi"

[rust_c.c]
framework = "Artifacts/CExample.framework"
""",
                encoding="utf-8",
            )
            check = Mock()
            with patch.object(binding_checks, "check_c", check):
                binding_checks.run_check("c", root=root, argv=[])
            check.assert_called_once_with(
                source=root / "ffi/tests/ownership.c",
                include_dirs=(root / "ffi/include",),
                library=root / "Artifacts/CExample.framework/CExample",
                work_parent=root / ".native-build",
                argv=[],
            )
