"""Disposable fixture workspace cleanup must preserve acceptance failures."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import workspaces


class WorkspaceTests(unittest.TestCase):
    def test_workspace_removes_executable_after_late_windows_release(self):
        error = PermissionError("The native program is still locked")
        error.winerror = 5
        remove_tree = tempfile.TemporaryDirectory._rmtree

        def remove_after_release(name, *, ignore_errors):
            elapsed = sum(wait.args[0] for wait in sleep.call_args_list)
            if name == str(work) and elapsed < 20.0:
                raise error
            remove_tree(name, ignore_errors=ignore_errors)

        with (
            patch.object(tempfile.TemporaryDirectory, "_rmtree", side_effect=remove_after_release),
            patch.object(workspaces.time, "sleep") as sleep,
        ):
            with tempfile.TemporaryDirectory() as parent:
                with workspaces.temporary_workspace(parent, prefix="fixture-") as work:
                    (work / "abi.exe").touch()
                self.assertFalse(work.exists())
            self.assertFalse(Path(parent).exists())

    def test_workspace_surfaces_permanent_windows_cleanup_failure(self):
        error = PermissionError("The native program is still locked")
        error.winerror = 5
        final_error = PermissionError("The native program remains locked")
        final_error.winerror = 5
        with tempfile.TemporaryDirectory() as parent:
            with (
                patch.object(
                    tempfile.TemporaryDirectory, "_rmtree", side_effect=[error] * 8 + [final_error]
                ) as cleanup,
                patch.object(workspaces.time, "sleep") as sleep,
                self.assertRaises(PermissionError) as raised,
            ):
                with workspaces.temporary_workspace(parent, prefix="fixture-"):
                    pass
            self.assertIs(raised.exception, final_error)
            self.assertEqual(cleanup.call_count, 9)
            self.assertEqual(
                [wait.args[0] for wait in sleep.call_args_list],
                [0.1, 0.2, 0.4, 0.8, 1.6, 3.2, 6.4, 12.8],
            )

    def test_workspace_preserves_body_error_when_cleanup_fails(self):
        error = PermissionError("Workspace removal denied")
        body_error = ValueError("Fixture execution failed")
        body_error.add_note("Fixture diagnostic")
        with tempfile.TemporaryDirectory() as parent:
            with (
                patch.object(tempfile.TemporaryDirectory, "_rmtree", side_effect=error) as cleanup,
                patch.object(workspaces.time, "sleep") as sleep,
                self.assertRaisesRegex(ValueError, "Fixture execution failed") as raised,
            ):
                with workspaces.temporary_workspace(parent, prefix="fixture-") as work:
                    raise body_error
            cleanup.assert_called_once()
            sleep.assert_not_called()
            self.assertIs(raised.exception, body_error)
            self.assertEqual(
                raised.exception.__notes__,
                ["Fixture diagnostic", f"Could not remove temporary workspace {work}: {error}"],
            )
