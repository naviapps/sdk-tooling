import tempfile
import unittest
from pathlib import Path

from sdk_tooling import native_plan


class NativePlanTests(unittest.TestCase):
    def setUp(self):
        temporary = self.enterContext(tempfile.TemporaryDirectory())
        self.root = Path(temporary).resolve()
        (self.root / "ThirdParty").mkdir()
        (self.root / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.99.1"\n', encoding="utf-8"
        )
        for name in ("alpha", "beta"):
            ffi = self.root / "crates" / name
            ffi.mkdir(parents=True)
            (ffi / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n', encoding="utf-8")
            (ffi / "include").mkdir()
            (ffi / "include/example.h").write_text("", encoding="utf-8")
            (ffi / "module.modulemap").write_text(
                f'module C{name} {{ header "example.h" }}\n', encoding="utf-8"
            )

    def write_specs(self, receipt):
        (self.root / "native.toml").write_text(
            f"""[[artifacts]]
id = "alpha"
ffi = "crates/alpha"
symbol_prefix = "alpha_"
minimum_macos = "26.0"
receipt = "alpha.json"

[[artifacts]]
id = "beta"
ffi = "crates/beta"
symbol_prefix = "beta_"
minimum_macos = "26.0"
receipt = "{receipt}"
""",
            encoding="utf-8",
        )

    def test_each_declarative_product_uses_its_own_framework_and_receipt(self):
        self.write_specs("beta.json")
        products = native_plan.builders(self.root)
        self.assertEqual(list(products), ["alpha", "beta"])
        selected = native_plan.builder(self.root, "beta")
        self.assertEqual(selected.artifact.name, "Cbeta.xcframework")
        self.assertEqual(selected.receipt.name, "beta.json")
        self.assertEqual(selected.configuration["specification"], "native.toml")
        self.assertIn(self.root / "native.toml", selected._mandatory_sources())
        with self.assertRaisesRegex(ValueError, "Select one"):
            native_plan.builder(self.root)

    def test_rejects_an_overlapping_publication_before_building(self):
        self.write_specs("alpha.json")
        with self.assertRaisesRegex(ValueError, "distinct receipt"):
            native_plan.builders(self.root)
