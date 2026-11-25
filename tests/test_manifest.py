"""Verify unified SDK declarations and fallback for historical checkouts."""

import tempfile
import unittest
from pathlib import Path

from sdk_tooling.manifest import MANIFEST, manifest_path, read_section


class ManifestTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()

    def test_split_declaration_is_read_when_unified_manifest_is_absent(self):
        legacy = self.root / "native.toml"
        legacy.write_text('[[artifacts]]\nid = "sample"\n', encoding="utf-8")
        self.assertEqual(manifest_path(self.root, "native"), legacy)
        self.assertEqual(read_section(self.root, "native"), {"artifacts": [{"id": "sample"}]})
        self.assertIsNone(read_section(self.root, "checks", required=False))

    def test_unified_manifest_owns_all_sections_when_present(self):
        (self.root / "native.toml").write_text('[[artifacts]]\nid = "legacy"\n', encoding="utf-8")
        (self.root / MANIFEST).write_text(
            '[checks.swift]\nexecutable = "Example"\n', encoding="utf-8"
        )
        self.assertEqual(manifest_path(self.root, "checks"), self.root / MANIFEST)
        self.assertEqual(read_section(self.root, "checks"), {"swift": {"executable": "Example"}})
        self.assertIsNone(read_section(self.root, "native", required=False))
        with self.assertRaisesRegex(ValueError, r"Declare \[native\]"):
            read_section(self.root, "native")

    def test_invalid_sections_fail_instead_of_falling_back(self):
        config = self.root / MANIFEST
        for contents in ('checks = "invalid"\n', "[chekcs]\n"):
            with self.subTest(contents=contents):
                config.write_text(contents, encoding="utf-8")
                with self.assertRaises(ValueError):
                    read_section(self.root, "checks", required=False)
