import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sdk_tooling import adapters


class AdapterTests(unittest.TestCase):
    def test_script_and_module_use_the_selected_tooling_with_isolated_python(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as directory:
            root = Path(directory).resolve()
            fixtures = root / "fixtures"
            fixtures.mkdir()
            script = fixtures / "probe.py"
            script.write_text(
                "import json, sys\nfrom sdk_tooling import adapters\n"
                "print(json.dumps([adapters.__file__, sys.argv[1:]]))\n",
                encoding="utf-8",
            )
            expected = [str(Path(adapters.__file__).resolve()), ["one value"]]
            for target in (("fixtures/probe.py",), ("-m", "fixtures.probe")):
                with self.subTest(target=target):
                    result = subprocess.check_output(
                        adapters.python_command(root, *target, "one value"),
                        cwd=root,
                        env=dict(os.environ, PYTHONPATH=str(root / "unselected")),
                        text=True,
                    )
                    self.assertEqual(json.loads(result), expected)
            self.assertFalse(list(fixtures.glob("__pycache__")))

    def test_python_fixtures_cannot_escape_the_selected_checkout(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory).resolve()
            root = path / "sdk"
            root.mkdir()
            script = path / "outside.py"
            script.write_text("raise AssertionError('unexpected execution')\n", encoding="utf-8")
            result = subprocess.run(
                adapters.python_command(root, script), cwd=root, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("must be an ordinary file inside the SDK", result.stderr)
