import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from sdk_tooling import adapters
from sdk_tooling.consumers import nuget


class AdapterTests(unittest.TestCase):
    def test_load_accepts_package_modules_but_not_relative_names(self):
        self.assertIs(adapters.load("consumers.nuget"), nuget)
        for name in (".nuget", "consumers..nuget", "consumers/nuget", "consumers.nuget."):
            with (
                self.subTest(name=name),
                self.assertRaisesRegex(ValueError, "Invalid SDK Tooling module"),
            ):
                adapters.load(name)

    def test_script_and_module_use_the_selected_tooling_with_isolated_python(self):
        with tempfile.TemporaryDirectory(prefix="SDK fixture ") as directory:
            root = Path(directory).resolve()
            distribution = root / "distribution"
            distribution.mkdir()
            script = distribution / "probe.py"
            script.write_text(
                "import json, sys\nfrom sdk_tooling import adapters\n"
                "print(json.dumps([adapters.__file__, sys.argv[1:]]))\n",
                encoding="utf-8",
            )
            expected = [str(Path(adapters.__file__).resolve()), ["one value"]]
            for target in (("distribution/probe.py",), ("-m", "distribution.probe")):
                with self.subTest(target=target):
                    result = subprocess.check_output(
                        adapters.python_command(root, *target, "one value"),
                        cwd=root,
                        env=dict(os.environ, PYTHONPATH=str(root / "unselected")),
                        text=True,
                    )
                    self.assertEqual(json.loads(result), expected)
            self.assertFalse(list(distribution.glob("__pycache__")))

    def test_distribution_support_records_fixture_sources_without_build_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = [
                "distribution/verify_windows.py",
                "fixtures/consumers/dotnet/Program.cs",
                "fixtures/consumers/rust/Cargo.toml.in",
                "fixtures/consumers/swift/Package.swift",
                "fixtures/consumers/c/main.c",
                "fixtures/acceptance/macos/acceptance.py",
                "fixtures/acceptance/windows/Cargo.lock",
                "fixtures/acceptance/windows/src/main.rs",
            ]
            for name in [
                *sources,
                "distribution/tests/test_distribution.py",
                "fixtures/consumers/dotnet/obj/Generated.cs",
                "fixtures/consumers/swift/.build/workspace-state.json",
                "fixtures/acceptance/windows/target/debug/build.rs",
            ]:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes(b"initial")

            evidence = adapters.support_inputs(root)
            self.assertEqual({name for name in evidence if not name.startswith("@")}, set(sources))
            self.assertIn("@sdk-tooling/sdk_tooling/consumers/nuget.py", evidence)
            fixture = "fixtures/consumers/dotnet/Program.cs"
            (root / fixture).write_bytes(b"modified")
            self.assertNotEqual(adapters.support_inputs(root)[fixture], evidence[fixture])

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
