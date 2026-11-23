"""Portable receipts and synthetic PE checks; no Windows binary is executed."""

import struct
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import windows as sdk


def fixture(machine=0x8664, imported=b"KERNEL32.dll", exported=b"pgr_test"):
    data = bytearray(4096)

    def put(fmt, at, *values):
        struct.pack_into(fmt, data, at, *values)

    data[:2] = b"MZ"
    put("<I", 0x3C, 0x80)
    data[0x80:0x84] = b"PE\0\0"
    put("<HHIIIHH", 0x84, machine, 1, 0, 0, 0, 240, 0x2000)
    opt = 0x98
    put("<H", opt, 0x20B)
    put("<I", opt + 108, 16)
    put("<IIII", opt + 112, 0x1000, 0x100, 0x1200, 40)
    put("<8sIIII", opt + 240, b".rdata", 0xE00, 0x1000, 0xE00, 0x200)
    export_table(data, [(exported, 0)], [0x1400])
    put("<IIIII", 0x400, 0, 0, 0, 0x1280, 0)
    data[0x480 : 0x480 + len(imported) + 1] = imported + b"\0"
    return data


def export_table(data, names, addresses):
    struct.pack_into("<IIIII", data, 0x214, len(addresses), len(names), 0x1100, 0x1110, 0x1120)
    for index, address in enumerate(addresses):
        struct.pack_into("<I", data, 0x300 + index * 4, address)
    for index, (name, ordinal) in enumerate(names):
        start = 0x330 + index * 32
        struct.pack_into("<I", data, 0x310 + index * 4, start + 0xE00)
        struct.pack_into("<H", data, 0x320 + index * 2, ordinal)
        data[start : start + len(name) + 1] = name + b"\0"


class PEInspection(unittest.TestCase):
    def inspect(self, data, machine=0x8664, symbols=("pgr_test",)):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "metadata-fixture.dll"
            path.write_bytes(data)
            return sdk.inspect_pe(
                path,
                machine,
                set(symbols),
                allow_import=lambda name: name in {"kernel32.dll", "bcryptprimitives.dll"},
            )

    def test_each_architecture_is_checked_without_execution(self):
        for machine in (0x8664, 0xAA64):
            self.assertEqual(self.inspect(fixture(machine), machine)["imports"], ["kernel32.dll"])

    def test_wrong_architecture_export_and_runtime_dependency_are_rejected(self):
        for data in (
            fixture(0xAA64),
            fixture(exported=b"private_symbol"),
            fixture(imported=b"VCRUNTIME140.dll"),
            fixture(imported=b"surprise.dll"),
        ):
            with self.assertRaises(ValueError):
                self.inspect(data)

    def test_windows_system_rng_is_distinct_from_an_external_runtime(self):
        self.assertEqual(
            self.inspect(fixture(imported=b"bcryptprimitives.dll"))["imports"],
            ["bcryptprimitives.dll"],
        )
        with self.assertRaises(ValueError):
            self.inspect(fixture(imported=b"bcryptprimitives-extra.dll"))

    def test_truncation_and_bad_rvas_do_not_pass(self):
        for size in (0, 2, 64, 200, 600):
            with self.assertRaises(ValueError):
                self.inspect(fixture()[:size])
        data = fixture()
        struct.pack_into("<I", data, 0x310, 0xFFFFFF00)
        with self.assertRaises(ValueError):
            self.inspect(data)

    def test_forwarded_exports_are_rejected(self):
        data = fixture()
        struct.pack_into("<I", data, 0x300, 0x1040)
        with self.assertRaises(ValueError):
            self.inspect(data)

    def test_distinct_named_exports_cannot_hide_an_ordinal_only_export(self):
        data = fixture()
        names = [(b"pgr_alias", 0), (b"pgr_test", 0)]
        export_table(data, names, [0x1400, 0x1420])
        with self.assertRaisesRegex(ValueError, "Ordinal-only"):
            self.inspect(data, symbols=("pgr_alias", "pgr_test"))

    def test_named_aliases_and_unused_ordinal_slots_are_valid(self):
        for names, addresses in (
            ([(b"pgr_alias", 0), (b"pgr_test", 0)], [0x1400]),
            ([(b"pgr_alias", 0), (b"pgr_test", 2)], [0x1400, 0, 0x1420]),
        ):
            with self.subTest(names=names):
                data = fixture()
                export_table(data, names, addresses)
                symbols = ("pgr_alias", "pgr_test")
                self.assertEqual(self.inspect(data, symbols=symbols)["exports"], list(symbols))

    def test_named_exports_must_be_distinct_ordered_and_have_an_address(self):
        for names, addresses, symbols, message in (
            ([(b"pgr_test", 0), (b"pgr_test", 1)], [0x1400, 0x1420], ("pgr_test",), "C header"),
            (
                [(b"pgr_test", 0), (b"pgr_alias", 1)],
                [0x1400, 0x1420],
                ("pgr_alias", "pgr_test"),
                "C header",
            ),
            ([(b"pgr_test", 0)], [0], ("pgr_test",), "no address"),
            ([(b"pgr_test", 1)], [0x1400], ("pgr_test",), "Invalid export ordinal"),
        ):
            with self.subTest(names=names, addresses=addresses):
                data = fixture()
                export_table(data, names, addresses)
                with self.assertRaisesRegex(ValueError, message):
                    self.inspect(data, symbols=symbols)

    def test_directory_size_and_import_coverage_are_checked_before_inspection(self):
        for offset, value, diagnostic in (
            (0x98 + 108, 17, "directory count"),
            (0x98 + 112 + 4, 0, "Missing export directory"),
            (0x98 + 112 + 8, 0, "Invalid import directory coverage"),
            (0x98 + 112 + 13 * 8, 0x1200, "Delay-loaded"),
        ):
            with self.subTest(offset=offset):
                data = fixture()
                struct.pack_into("<I", data, offset, value)
                with self.assertRaisesRegex(ValueError, diagnostic):
                    self.inspect(data)
        data = fixture()
        struct.pack_into("<II", data, 0x98 + 112 + 8, 0, 0)
        self.assertEqual(self.inspect(data)["imports"], [])

    def test_native_verifier_uses_the_dll_bytes_and_preserves_static_crt_policy(self):
        with tempfile.TemporaryDirectory() as temporary:
            library = Path(temporary) / "fixture.dll"
            header = Path(temporary) / "ffi.h"
            header.write_text("void pgr_test(void);", encoding="utf-8")

            def verify():
                return sdk.verify_library(str(library), [header], "pgr_", Path("tools"), {})

            with patch.object(sdk.subprocess, "check_output", side_effect=AssertionError):
                library.write_bytes(fixture(0xAA64))
                self.assertEqual(verify(), {"pgr_test"})
                ordinal_only = fixture(0xAA64)
                export_table(ordinal_only, [(b"pgr_test", 0)], [0x1400, 0x1420])
                for data, diagnostic in (
                    (fixture(), "target-native"),
                    (fixture(0xAA64, exported=b"foreign"), "C header"),
                    (fixture(0xAA64, imported=b"VCRUNTIME140.dll"), "VC/UCRT runtime"),
                    (ordinal_only, "Ordinal-only"),
                ):
                    with self.subTest(diagnostic=diagnostic):
                        library.write_bytes(data)
                        with self.assertRaisesRegex(ValueError, diagnostic):
                            verify()


if __name__ == "__main__":
    unittest.main()
