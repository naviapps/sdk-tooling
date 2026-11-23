import argparse
import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

TEMPLATE = Path(__file__).resolve().parents[1] / "templates/_tooling.py"


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.tool = self.root / "tool"
        self.tool.mkdir()
        (self.tool / "sdk_tooling").mkdir()
        (self.tool / ".gitattributes").write_bytes(b"* text=auto eol=lf\n")
        (self.tool / "sdk_tooling/__init__.py").write_bytes(b'"""Fixture tooling."""\n')
        (self.tool / "sdk_tooling/probe.py").write_bytes(b'value = "pinned"\n')
        (self.tool / "sdk_tooling/hosted_windows.py").write_bytes(
            b"import json\n"
            b"\n"
            b"def main(root, argv):\n"
            b"    print(json.dumps([str(root), argv]))\n"
            b"    return 7\n"
        )
        self.git(self.tool, "init", "--quiet")
        self.git(self.tool, "add", ".")
        self.git(
            self.tool,
            "-c",
            "user.name=Fixture",
            "-c",
            "user.email=fixture@example.invalid",
            "commit",
            "--quiet",
            "-m",
            "fixture",
        )
        self.revision = self.git(self.tool, "rev-parse", "HEAD").strip()
        self.sdk = self.root / "sdk"
        (self.sdk / "tools").mkdir(parents=True)
        self.bootstrap = self.sdk / "tools/_tooling.py"
        shutil.copyfile(TEMPLATE, self.bootstrap)
        (self.sdk / "tools/tooling-sources.json").write_text(
            json.dumps({"naviapps/sdk-tooling": self.revision})
        )
        spec = importlib.util.spec_from_file_location("bootstrap_fixture", self.bootstrap)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        environment = patch.dict(
            os.environ,
            {
                "NAVI_SDK_TOOLING_CACHE": str(self.root / "cache"),
                "NAVI_SDK_TOOLING_OFFLINE": "1",
                "NAVI_SDK_TOOLING_SOURCE": "",
                "NAVI_CI_SOURCES": "",
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def git(self, root, *arguments):
        return subprocess.check_output(
            ["git", "-c", "core.hooksPath=" + os.devnull, "-C", str(root), *arguments],
            text=True,
            stderr=subprocess.PIPE,
        )

    def bundle(self):
        source = self.root / "sources"
        source.mkdir()
        bundle = source / "0.bundle"
        self.git(self.tool, "bundle", "create", str(bundle), "--all")
        mirror = bundle.with_suffix(".git")
        subprocess.run(
            ["git", "clone", "--quiet", "--mirror", str(bundle), str(mirror)],
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        entry = dict(
            repository="naviapps/sdk-tooling",
            revision=self.revision,
            url="https://github.com/naviapps/sdk-tooling.git",
            bundle=bundle.name,
            sha256=hashlib.sha256(bundle.read_bytes()).hexdigest(),
        )
        manifest = source / "manifest.json"
        manifest.write_text(json.dumps([entry]))
        os.environ["NAVI_CI_SOURCES"] = str(manifest)
        return manifest, entry, bundle

    def test_explicit_pinned_source_is_read_only_and_requires_clean_bytes(self):
        os.environ["NAVI_SDK_TOOLING_SOURCE"] = str(self.tool)
        self.assertEqual(self.module.source(), self.tool)
        (self.tool / "sdk_tooling/probe.py").write_bytes(b'value = "changed"\n')
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.module.source()

    def test_assume_unchanged_cannot_hide_corrupt_cached_code(self):
        os.environ["NAVI_SDK_TOOLING_SOURCE"] = str(self.tool)
        self.git(self.tool, "update-index", "--assume-unchanged", "sdk_tooling/probe.py")
        (self.tool / "sdk_tooling/probe.py").write_bytes(b'value = "changed"\n')
        self.assertEqual(self.git(self.tool, "status", "--porcelain").strip(), "")
        with self.assertRaisesRegex(ValueError, "cache bytes"):
            self.module.source()

    def test_offline_missing_cache_fails_before_fetch(self):
        with (
            patch.object(self.module, "git") as git,
            self.assertRaisesRegex(RuntimeError, "offline cache"),
        ):
            self.module.source()
        git.assert_not_called()

    def test_public_fetch_bypasses_configured_credential_helpers(self):
        os.environ["NAVI_SDK_TOOLING_OFFLINE"] = "0"
        original_git = self.module.git

        def local_fetch(root, *args):
            if "fetch" in args:
                self.assertEqual(args[:3], ("-c", "credential.helper=", "fetch"))
                self.assertEqual(args[-2:], (self.module.URL, self.revision))
                args = (*args[:-2], self.tool.as_uri(), self.revision)
            return original_git(root, *args)

        with patch.object(self.module, "git", side_effect=local_fetch):
            self.assertEqual(self.module.source().name, self.revision)

    def test_local_source_bundle_populates_one_reusable_verified_cache(self):
        self.bundle()
        normal = self.module.git
        with patch.object(self.module, "git", wraps=normal) as git:
            first = self.module.source()
            self.assertEqual(self.module.source(), first)
        self.assertEqual(first.name, self.revision)
        self.assertEqual(len([call for call in git.call_args_list if "fetch" in call.args]), 1)

    def test_concurrent_cache_publication_rejects_corrupt_winner(self):
        self.bundle()

        def lose_publication(candidate, destination):
            shutil.copytree(candidate, destination)
            self.git(destination, "update-index", "--assume-unchanged", "sdk_tooling/probe.py")
            (destination / "sdk_tooling/probe.py").write_text('value = "corrupt"\n')
            raise FileExistsError(destination)

        with (
            patch.object(self.module.os, "rename", side_effect=lose_publication),
            self.assertRaisesRegex(ValueError, "cache bytes"),
        ):
            self.module.source()

    def test_fixture_and_cached_bytes_match_git_blobs_even_with_autocrlf(self):
        with patch.dict(
            os.environ,
            {
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.autocrlf",
                "GIT_CONFIG_VALUE_0": "true",
            },
        ):
            self.bundle()
            cached = self.module.source()
            for name in (".gitattributes", "sdk_tooling/__init__.py", "sdk_tooling/probe.py"):
                with self.subTest(name=name):
                    committed = self.module.git(self.tool, "show", "HEAD:" + name)
                    self.assertNotIn(b"\r", committed)
                    self.assertEqual((self.tool / name).read_bytes(), committed)
                    self.assertEqual((cached / name).read_bytes(), committed)

    def test_wrong_pin_and_corrupt_archive_fail_before_any_checkout(self):
        manifest, entry, bundle = self.bundle()
        for changed in (
            {**entry, "revision": "0" * 40},
            {**entry, "bundle": "../0.bundle"},
            {**entry, "sha256": "0" * 64},
        ):
            manifest.write_text(json.dumps([changed]))
            with self.assertRaises(ValueError):
                self.module.source()
            self.assertFalse((self.root / "cache" / self.revision).exists())

    def test_changed_manifest_is_rejected_even_after_a_cache_hit(self):
        manifest, entry, _ = self.bundle()
        self.module.source()
        manifest.write_text(json.dumps([{**entry, "revision": "0" * 40}]))
        with self.assertRaisesRegex(ValueError, "captured pin"):
            self.module.source()

    def test_loader_uses_only_the_verified_source(self):
        os.environ["NAVI_SDK_TOOLING_SOURCE"] = str(self.tool)
        code = (
            "import importlib.util; s=importlib.util.spec_from_file_location('bootstrap',"
            + repr(str(self.bootstrap))
            + ");m=importlib.util.module_from_spec(s);s.loader.exec_module(m);"
            "print(m.load('probe').value)"
        )
        result = subprocess.check_output([sys.executable, "-I", "-B", "-c", code], text=True)
        self.assertEqual(result.strip(), "pinned")
        self.assertFalse(list(self.tool.rglob("*.pyc")))

    def test_candidate_forwards_verified_source_and_rechecks_bytes(self):
        parser = argparse.ArgumentParser()
        self.module.add_candidate_options(parser)
        self.assertEqual(self.module.select_candidate(parser, parser.parse_args([])), [])
        self.assertFalse(self.module.candidate_selected())
        options = ["--tooling-source", str(self.tool), "--tooling-revision", self.revision]
        args = parser.parse_args(options)
        self.assertEqual(self.module.select_candidate(parser, args, absolute=True), options)
        self.assertTrue(self.module.candidate_selected())
        self.module.verify_candidate()
        (self.tool / "sdk_tooling/probe.py").write_bytes(b'value = "changed"\n')
        with self.assertRaisesRegex(ValueError, "local changes"):
            self.module.verify_candidate()

    def test_entrypoint_passes_sdk_root_and_arguments_to_verified_driver(self):
        os.environ["NAVI_SDK_TOOLING_SOURCE"] = str(self.tool)
        args = ["-m", "tools.native_host", "--architecture", "x86_64"]
        result = subprocess.run(
            [sys.executable, "-E", "-S", "-B", str(self.bootstrap), *args],
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), [str(self.sdk), args])


if __name__ == "__main__":
    unittest.main()
