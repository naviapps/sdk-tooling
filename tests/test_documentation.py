"""DocC orchestration preserves owner validators, dependencies and prior output."""

import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from sdk_tooling.docs import check_catalogs
from sdk_tooling.sdk_checks import _validate_declaration_docs


class DocumentationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="sdk-docs-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.graphs = self.root / ".build/symbol graphs"
        self.graphs.mkdir(parents=True)
        self.catalogs = [
            self.root / "Sources" / name / (name + ".docc") for name in ("Core", "Adapter")
        ]
        for catalog in self.catalogs:
            catalog.mkdir(parents=True)
            (self.graphs / (catalog.stem + ".symbols.json")).write_text(
                json.dumps({"module": {"name": catalog.stem}, "symbols": []})
            )
        self.output = self.root / "docs"
        self.output.mkdir()
        self.calls = []
        self.fail_module = None
        self.omit_linking_data = False
        self.malformed_linking_data = False

    def runner(self, command, **options):
        self.calls.append(command)
        if command[0] == "swift":
            return SimpleNamespace(
                returncode=0, stdout="Files written to " + str(self.graphs) + "\n"
            )
        module = Path(command[3]).stem
        if module == self.fail_module:
            raise subprocess.CalledProcessError(1, command)
        for index, argument in enumerate(command):
            if argument == "--dependency":
                self.assertTrue(Path(command[index + 1]).is_dir())
        archive = Path(command[command.index("--output-path") + 1])
        archive.mkdir()
        (archive / "owned.txt").write_text(module)
        if "--enable-experimental-external-link-support" in command and not self.omit_linking_data:
            for name in ("linkable-entities.json", "link-hierarchy.json"):
                (archive / name).write_text("not-json" if self.malformed_linking_data else "{}")
        return SimpleNamespace(returncode=0)

    def invoke(self, **options):
        return check_catalogs(
            self.root,
            self.catalogs,
            output=self.output,
            bundle_identifier_prefix="com.example.",
            runner=self.runner,
            **options,
        )

    def old_archives(self):
        for catalog in self.catalogs:
            archive = self.output / (catalog.stem + ".doccarchive")
            archive.mkdir()
            (archive / "owned.txt").write_text("previous")

    def assert_previous(self):
        for catalog in self.catalogs:
            self.assertEqual(
                (self.output / (catalog.stem + ".doccarchive") / "owned.txt").read_text(),
                "previous",
            )

    def test_owner_options_validation_and_dependency_archives_are_preserved(self):
        self.old_archives()
        validated = []

        def validate(module, graphs):
            self.assertEqual([graph["module"]["name"] for graph in graphs], [module])
            validated.append(module)

        self.invoke(
            graph_arguments=("--skip-synthesized-members",),
            convert_arguments=("--enable-experimental-external-link-support",),
            symbol_validator=validate,
            retain_archives=True,
        )
        self.assertEqual(validated, ["Core", "Adapter"])
        self.assertIn("--skip-synthesized-members", self.calls[0])
        self.assertNotIn("--dependency", self.calls[1])
        self.assertIn("--dependency", self.calls[2])
        self.assertIn("--enable-experimental-external-link-support", self.calls[2])
        self.assertEqual(self.calls[2].count("--enable-experimental-external-link-support"), 1)
        for catalog in self.catalogs:
            self.assertEqual(
                (self.output / (catalog.stem + ".doccarchive") / "owned.txt").read_text(),
                catalog.stem,
            )

    def test_conversion_failure_preserves_all_previous_archives(self):
        self.old_archives()
        self.fail_module = "Adapter"
        with self.assertRaises(subprocess.CalledProcessError):
            self.invoke(retain_archives=True)
        self.assert_previous()

    def test_successful_conversion_without_link_metadata_does_not_publish_or_feed_dependencies(
        self,
    ):
        self.old_archives()
        for mode in ("missing", "malformed"):
            with self.subTest(mode=mode):
                self.calls.clear()
                self.omit_linking_data = mode == "missing"
                self.malformed_linking_data = mode == "malformed"
                with self.assertRaisesRegex(ValueError, "link metadata"):
                    self.invoke(retain_archives=True)
                self.assertEqual(len(self.calls), 2)
                self.assert_previous()

    def test_owner_validation_failure_prevents_conversion_and_publication(self):
        self.old_archives()

        def reject(module, graphs):
            raise ValueError("Missing owner declaration help")

        with self.assertRaisesRegex(ValueError, "owner declaration"):
            self.invoke(symbol_validator=reject, retain_archives=True)
        self.assertEqual(len(self.calls), 1)
        self.assert_previous()

    def test_validation_only_does_not_retain_archives(self):
        self.invoke()
        self.assertEqual(list(self.output.iterdir()), [])

    def test_foreign_symbol_graph_output_is_rejected_before_conversion(self):
        self.graphs = self.root / "foreign"
        with self.assertRaisesRegex(ValueError, "owned symbol graph"):
            self.invoke()
        self.assertEqual(len(self.calls), 1)


class DeclarationDocumentationTests(unittest.TestCase):
    def test_missing_and_blank_owned_help_fail_in_paths_with_spaces(self):
        with tempfile.TemporaryDirectory() as directory:
            sources = Path(directory) / "SDK Sources"
            symbol = {
                "location": {"uri": (sources / "State.swift").as_uri()},
                "kind": {"identifier": "swift.method"},
                "names": {"title": "update(_:now:)"},
                "pathComponents": ["State", "update(_:now:)"],
            }
            for comment in [{}, {"docComment": {"lines": [{"text": "   "}]}}]:
                with (
                    self.subTest(comment=comment),
                    self.assertRaisesRegex(ValueError, r"State\.update\(_:now:\)"),
                ):
                    _validate_declaration_docs(sources, [{"symbols": [dict(symbol, **comment)]}])
            symbol["docComment"] = {
                "lines": [{"text": "Submit state at absolute monotonic nanoseconds."}]
            }
            _validate_declaration_docs(sources, [{"symbols": [symbol]}])

    def test_imported_help_and_standard_witnesses_are_not_owned_contracts(self):
        def symbol(path, kind="swift.method", title="run()"):
            return {
                "location": {"uri": path.as_uri()},
                "kind": {"identifier": kind},
                "names": {"title": title},
                "pathComponents": ["Example", title],
            }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sdk/Sources"
            value = sources / "Value.swift"
            graph = {
                "symbols": [
                    symbol(root / "dependency/Sources/Other.swift"),
                    symbol(root / "sdk/SourcesElsewhere/Other.swift"),
                    symbol(value, "swift.extension"),
                    symbol(value, "swift.func.op", "==(_:_:)"),
                    {"kind": {"identifier": "swift.method"}, "pathComponents": ["Imported"]},
                ]
            }
            _validate_declaration_docs(sources, [graph])


if __name__ == "__main__":
    unittest.main()
