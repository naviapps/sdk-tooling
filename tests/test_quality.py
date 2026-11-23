"""Static checks respect source ownership, tool pins and failing exit statuses."""

import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sdk_tooling import quality


class QualityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("NAVI_CI_SOURCE_FILES", None)
        os.environ.pop("NAVI_CI_SOURCE_FILES_SHA256", None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()
        subprocess.run(["git", "init", "--quiet", self.root], check=True)

    def write(self, relative, value=""):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value.encode("utf-8"))
        return path

    def test_sources_include_new_files_but_not_ignored_generated_or_vendor_inputs(self):
        self.write(".gitignore", "ignored.py\n")
        for name in [
            "tools/new.py",
            "ignored.py",
            "Artifacts/header.h",
            "ThirdParty/source.py",
            "vendor/package/helper.py",
            "target/generated.c",
            "obj/generated.cs",
        ]:
            self.write(name)
        self.assertEqual(quality.source_files(self.root), [".gitignore", "tools/new.py"])

    def test_source_directory_names_are_not_case_folded_into_generated_outputs(self):
        sources = [
            "Sources/Build/Policy.swift",
            "fixtures/macos/Sources/Target/main.swift",
            "tools/build",
        ]
        for name in sources:
            self.write(name)
        subprocess.run(["git", "-C", str(self.root), "add", sources[1]], check=True)
        self.assertEqual(quality.source_files(self.root), sources)

    def test_git_selection_keeps_tracked_ignored_files_and_skips_deleted_files(self):
        self.write(".gitignore", "owned.py\n")
        self.write("owned.py")
        removed = self.write("removed.py")
        subprocess.run(
            ["git", "-C", str(self.root), "add", "--force", "owned.py", "removed.py"], check=True
        )
        removed.unlink()
        self.assertEqual(quality.source_files(self.root), [".gitignore", "owned.py"])

    def test_linked_first_party_source_is_rejected(self):
        target = self.write("target/original.py")
        try:
            (self.root / "linked.py").symlink_to(target)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation; no privilege changes attempted")
            raise
        with self.assertRaisesRegex(ValueError, "ordinary first-party"):
            quality.source_files(self.root)

    def test_tracked_source_below_a_linked_directory_is_rejected(self):
        source = self.write("Sources/Library/main.py")
        subprocess.run(["git", "-C", str(self.root), "add", str(source)], check=True)
        target = self.root / "target"
        source.parent.rename(target)
        try:
            source.parent.symlink_to(target, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation; no privilege changes attempted")
            raise
        with self.assertRaisesRegex(
            ValueError, "ordinary first-party files: Sources/Library/main.py"
        ):
            quality.source_files(self.root)

    def test_wrong_tool_version_fails_before_checking_source(self):
        self.write("mise.toml", '[tools]\nruff = "0.17.3"\n')
        with (
            patch.object(quality.shutil, "which", return_value="/tools/ruff"),
            patch.object(quality.subprocess, "check_output", return_value="ruff 0.17.30"),
        ):
            with self.assertRaisesRegex(RuntimeError, "differs from mise.toml"):
                quality.tool(self.root, "ruff")

    def test_missing_tool_does_not_install_or_mutate_the_repository(self):
        self.write("mise.toml", '[tools]\nruff = "0.17.3"\n')
        with (
            patch.object(quality.shutil, "which", return_value=None),
            patch.object(quality.subprocess, "run") as run,
        ):
            with self.assertRaisesRegex(RuntimeError, "mise install"):
                quality.tool(self.root, "ruff")
            run.assert_not_called()

    def test_workflow_check_passes_pinned_linters_and_disables_out_of_scope_tools(self):
        self.write(".github/workflows/ci.yml")
        self.write(
            "mise.toml",
            '[tools]\nactionlint = "1.7.12"\nshellcheck = "0.11.0"\n'
            '"pypi:pyflakes" = { version = "4.1.0", os = ["macos", "linux"] }\n',
        )
        executables = {
            name: str(self.root / "tool directory" / name)
            for name in ("actionlint", "shellcheck", "pyflakes")
        }
        versions = {
            executables["actionlint"]: "1.7.12",
            executables["shellcheck"]: "ShellCheck\nversion: 0.11.0",
            executables["pyflakes"]: "4.1.0 Python 3.15.2 on Darwin",
        }
        for host, pyflakes in (
            ("darwin", executables["pyflakes"]),
            ("linux", executables["pyflakes"]),
            ("win32", ""),
        ):
            with (
                self.subTest(host=host),
                patch.object(quality.sys, "platform", host),
                patch.object(quality.shutil, "which", side_effect=executables.__getitem__) as find,
                patch.object(
                    quality.subprocess,
                    "check_output",
                    side_effect=lambda args, **_: versions[args[0]],
                ) as version,
                patch.object(quality, "source_files", return_value=[".github/workflows/ci.yml"]),
                patch.object(quality, "run") as run,
            ):
                self.assertEqual(quality.check(self.root, languages=["data"]), 0)
                run.assert_called_once_with(
                    self.root,
                    [
                        executables["actionlint"],
                        f"-shellcheck={executables['shellcheck']}",
                        f"-pyflakes={pyflakes}",
                        "./.github/workflows/ci.yml",
                    ],
                )
                if not pyflakes:
                    self.assertNotIn("pyflakes", [call.args[0] for call in find.call_args_list])
                    self.assertNotIn(
                        executables["pyflakes"],
                        [call.args[0][0] for call in version.call_args_list],
                    )

    def test_workflow_check_refuses_missing_or_wrong_version_of_applicable_linter(self):
        self.write(".github/workflows/ci.yml")
        self.write(
            "mise.toml",
            '[tools]\nactionlint = "1.7.12"\nshellcheck = "0.11.0"\n',
        )
        for shellcheck, message in (
            (None, "Missing shellcheck"),
            ("/host/shellcheck", "shellcheck differs from mise.toml"),
        ):
            with (
                self.subTest(shellcheck=shellcheck),
                patch.object(
                    quality.shutil, "which", side_effect=["/tools/actionlint", shellcheck]
                ),
                patch.object(
                    quality.subprocess, "check_output", side_effect=["1.7.12", "version: 0.10.0"]
                ),
                patch.object(quality, "source_files", return_value=[".github/workflows/ci.yml"]),
                patch.object(quality, "run") as run,
            ):
                with self.assertRaisesRegex(RuntimeError, message):
                    quality.check(self.root, languages=["data"])
                run.assert_not_called()

    def test_undeclared_workflow_linters_are_not_discovered_on_path(self):
        self.write("mise.toml", '[tools]\nactionlint = "1.7.12"\n')
        with patch.object(quality.shutil, "which") as find:
            self.assertEqual(quality.workflow_linters(self.root), ["-shellcheck=", "-pyflakes="])
        find.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Shell files are not checked on Windows")
    def test_shell_source_requires_a_pinned_formatter(self):
        self.write("mise.toml", "[tools]\n")
        with (
            patch.object(quality.shutil, "which", return_value="/host/dprint"),
            patch.object(quality, "run") as run,
        ):
            self.assertEqual(quality.check(self.root, languages=["shell"]), 0)
            self.write("tools/check.sh", "exit 0\n")
            with self.assertRaisesRegex(RuntimeError, r"add dprint to .*/mise\.toml"):
                quality.check(self.root, languages=["shell"])
            run.assert_not_called()

    @unittest.skipIf(os.name == "nt", "Shell files are not checked on Windows")
    def test_shell_check_and_format_keep_their_own_source_scope(self):
        script = "./tools/check.sh"
        self.write("tools/check.sh", "exit 0\n")
        self.write("vendor/check.sh", "bad syntax\n")
        self.write("README.md", "# README\n")
        for write in (False, True):
            with (
                self.subTest(write=write),
                patch.object(quality, "tool", side_effect=lambda root, name: name),
                patch.object(quality, "run") as run,
            ):
                quality.check(self.root, write=write, languages=["shell"])
                commands = [
                    ["dprint", "fmt" if write else "check", "--config", ".dprint.json", script]
                ]
                if not write:
                    commands.extend([["bash", "-n", script], ["shellcheck", script]])
                self.assertEqual([call.args[1] for call in run.call_args_list], commands)

    def test_unused_check_failure_is_propagated_without_running_formatter(self):
        self.write("-example.py", "unused = 1\n")
        error = subprocess.CalledProcessError(1, ["ruff", "check"])
        with (
            patch.object(quality, "tool", return_value="ruff"),
            patch.object(quality, "run", side_effect=error) as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                quality.check(self.root, languages=["python"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[1][-1], "./-example.py")

    def test_format_operation_does_not_remove_imports_or_run_the_product(self):
        self.write("source.py", "import os\n")
        with (
            patch.object(quality, "tool", return_value="ruff"),
            patch.object(quality, "run") as run,
        ):
            quality.check(self.root, write=True, languages=["python"])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(run.call_args.args[1], ["ruff", "format", "./source.py"])

    def test_unused_rust_dependency_fails_check_but_is_never_removed_by_format(self):
        self.write("Cargo.toml", "[workspace]\nmembers = []\n")
        self.write("bindings/web/rust/Cargo.toml", "[workspace]\nmembers = []\n")
        failure = subprocess.CalledProcessError(1, ["cargo-machete"])

        def command(root, arguments):
            if arguments[0] == "cargo-machete":
                raise failure

        with (
            patch.object(quality, "tool", return_value="cargo-machete"),
            patch.object(quality, "run", side_effect=command) as run,
        ):
            with self.assertRaises(subprocess.CalledProcessError):
                quality.check(self.root, languages=["rust"])
            self.assertEqual(run.call_count, 3)
            self.assertEqual(run.call_args.args[1], ["cargo-machete", self.root])
            run.reset_mock()
            quality.check(self.root, languages=["rust"], write=True)
            self.assertEqual(run.call_count, 2)
            self.assertTrue(
                all(call.args[1][:2] == ["cargo", "fmt"] for call in run.call_args_list)
            )

    def test_docc_symbol_links_never_reach_generic_markdown_checks_or_formatting(self):
        self.write(".dprint.json", "{}\n")
        catalog_markdown = "# ``Core``\n\n## Topics\n\n### Types\n\n- ``CoreType``\n"
        catalogs = [
            self.write("Sources/Core/Core.docc/Core.md", catalog_markdown),
            self.write("Sources/Core/Core.docc/Guides/Overview.md", catalog_markdown),
        ]
        subprocess.run(["git", "-C", str(self.root), "add", str(catalogs[0])], check=True)
        self.write("Sources/Core/Core.docc/theme-settings.json", "{}\n")
        readme_markdown = "# README\n\nUse ``value``.\n"

        for write in (False, True):
            with self.subTest(write=write):
                readme = self.write("README.md", readme_markdown)
                markdown_inputs = []

                def formatter(root, arguments):
                    self.assertEqual(
                        arguments,
                        [
                            "dprint",
                            "fmt" if write else "check",
                            "--config",
                            ".dprint.json",
                            "./.dprint.json",
                            "./README.md",
                            "./Sources/Core/Core.docc/theme-settings.json",
                        ],
                    )
                    for name in arguments[4:]:
                        path = root / name
                        if path.suffix == ".md":
                            original = path.read_bytes()
                            markdown_inputs.append(original)
                            if write:
                                path.write_bytes(original.replace(b"``", b"`"))

                with (
                    patch.object(quality, "tool", return_value="dprint"),
                    patch.object(quality, "run", side_effect=formatter) as run,
                ):
                    quality.check(self.root, write=write, languages=["data"])
                    run.assert_called_once()

                self.assertEqual(markdown_inputs, [readme_markdown.encode()])
                self.assertEqual(
                    readme.read_bytes(),
                    readme_markdown.encode().replace(b"``", b"`")
                    if write
                    else readme_markdown.encode(),
                )
                for catalog in catalogs:
                    self.assertEqual(catalog.read_bytes(), catalog_markdown.encode())


class ManifestQualityTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("NAVI_CI_SOURCE_FILES", None)
        os.environ.pop("NAVI_CI_SOURCE_FILES_SHA256", None)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name).resolve()

    def write(self, name, value=""):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value)
        return path

    def manifest(self, files=None, *, data=None, document=None):
        if data is None:
            document = document if document is not None else {"version": 1, "files": files}
            data = (json.dumps(document, separators=(",", ":")) + "\n").encode()
        path = self.root / ".navi-ci-source-files.json"
        path.write_bytes(data)
        os.environ.update(
            NAVI_CI_SOURCE_FILES=str(path),
            NAVI_CI_SOURCE_FILES_SHA256=hashlib.sha256(data).hexdigest(),
        )
        return path

    def test_gitless_snapshot_checks_selected_actual_bytes_and_preserves_exclusions(self):
        files = [
            "Artifacts/header.h",
            "Sources/Build/Policy.swift",
            "Sources/Core/Core.docc/Core.md",
            "Sources/Core/Core.docc/theme-settings.json",
            "Sources/Target/main.py",
            "ThirdParty/license.py",
            "ignored-but-tracked.py",
            "tools/build",
            "tools/new 日本語.py",
        ]
        for name in files:
            self.write(name)
        self.write(".gitignore", "*.py\n")
        self.write("not-captured.py")
        self.manifest(files)
        with patch.object(quality.subprocess, "check_output") as git:
            self.assertEqual(
                quality.source_files(self.root),
                [
                    name
                    for name in files
                    if name
                    not in {
                        "Artifacts/header.h",
                        "Sources/Core/Core.docc/Core.md",
                        "ThirdParty/license.py",
                    }
                ],
            )
            git.assert_not_called()
        self.assertFalse((self.root / ".git").exists())

    def test_manifest_format_and_later_checks_read_changed_selected_files(self):
        source = self.write("source.py", "before\n")
        manifest = self.manifest(["source.py"])
        original = manifest.read_bytes()

        def format_source(root, arguments):
            self.assertEqual(arguments, ["ruff", "format", "./source.py"])
            source.write_text("after\n")

        with (
            patch.object(quality, "tool", return_value="ruff"),
            patch.object(quality, "run", side_effect=format_source),
        ):
            self.assertEqual(quality.check(self.root, write=True, languages=["python"]), 0)
        self.write("later.py")
        self.assertEqual(quality.source_files(self.root), ["source.py"])
        self.assertEqual(source.read_text(), "after\n")
        self.assertEqual(manifest.read_bytes(), original)

    def test_gitless_workflow_check_passes_selected_config_and_pinned_linters(self):
        self.write(".github/workflows/ci.yml", "jobs: {}\n")
        self.write(
            "mise.toml",
            '[tools]\nactionlint = "1.7.12"\nshellcheck = "0.11.0"\n"pypi:pyflakes" = "4.1.0"\n',
        )
        executables = {
            name: str(self.root / "tool directory" / name)
            for name in ("actionlint", "shellcheck", "pyflakes")
        }
        for extensions in [("yaml",), ("yml",), ("yaml", "yml")]:
            with self.subTest(extensions=extensions):
                configs = [".github/actionlint." + extension for extension in extensions]
                for config in configs:
                    self.write(config, 'self-hosted-runner:\n  labels: ["navi-ci"]\n')
                self.manifest(sorted([*configs, ".github/workflows/ci.yml", "mise.toml"]))
                with (
                    patch.object(
                        quality,
                        "resolve_tool",
                        side_effect=lambda root, name, pin: executables[name],
                    ),
                    patch.object(quality, "run") as run,
                ):
                    self.assertEqual(quality.check(self.root, languages=["data"]), 0)
                    run.assert_called_once_with(
                        self.root,
                        [
                            executables["actionlint"],
                            "-config-file",
                            self.root / configs[0],
                            f"-shellcheck={executables['shellcheck']}",
                            f"-pyflakes={executables['pyflakes']}",
                            "./.github/workflows/ci.yml",
                        ],
                    )
        self.assertFalse((self.root / ".git").exists())

    def test_manifest_check_propagates_an_actual_command_failure(self):
        self.write("source.py")
        self.write("check", "raise SystemExit(7)\n")
        self.manifest(["source.py"])
        with patch.object(quality, "tool", return_value=sys.executable):
            with self.assertRaises(subprocess.CalledProcessError) as raised:
                quality.check(self.root, languages=["python"])
        self.assertEqual(raised.exception.returncode, 7)

    def test_manifest_parent_alias_checks_current_selected_bytes(self):
        source = self.write("source.py", "before\n")
        manifest = self.manifest(["source.py"])
        alias = self.root / "workspace-alias"
        try:
            alias.symlink_to(self.root, target_is_directory=True)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation; no privilege changes attempted")
            raise
        os.environ["NAVI_CI_SOURCE_FILES"] = str(alias / manifest.name)
        self.assertEqual(quality.source_files(self.root), ["source.py"])
        source.write_text("after\n")
        for operation in ["check", "format"]:
            self.write(
                operation,
                "from pathlib import Path\nimport sys\n"
                "assert Path(sys.argv[-1]).read_text() == 'after\\n'\n",
            )
        with patch.object(quality, "tool", return_value=sys.executable):
            self.assertEqual(quality.check(self.root, languages=["python"]), 0)

    def test_manifest_environment_and_hash_fail_without_git_fallback(self):
        self.write("source.py")
        for remove, value in [
            ("NAVI_CI_SOURCE_FILES", None),
            ("NAVI_CI_SOURCE_FILES_SHA256", None),
            ("NAVI_CI_SOURCE_FILES_SHA256", "0" * 64),
            ("NAVI_CI_SOURCE_FILES_SHA256", "not-a-hash"),
            ("NAVI_CI_SOURCE_FILES", ""),
        ]:
            with self.subTest(environment=remove, value=value):
                self.manifest(["source.py"])
                if value is None:
                    del os.environ[remove]
                else:
                    os.environ[remove] = value
                with patch.object(quality.subprocess, "check_output") as git:
                    with self.assertRaises(ValueError):
                        quality.source_files(self.root)
                    git.assert_not_called()

    def test_invalid_manifest_documents_and_names_fail(self):
        for document in [
            {"version": True, "files": ["source.py"]},
            {"version": 2, "files": ["source.py"]},
            {"version": 1, "files": []},
            {"version": 1, "files": "source.py"},
            {"version": 1, "files": [1]},
            {"version": 1, "files": ["source.py", "source.py"]},
            {"version": 1, "files": ["z.py", "a.py"]},
            {"version": 1, "files": ["source.py"], "extra": True},
            [],
        ]:
            with self.subTest(document=document):
                self.manifest(document=document)
                with self.assertRaisesRegex(
                    ValueError, "Invalid source selection manifest|sorted and unique"
                ):
                    quality.source_files(self.root)
        for name in [
            "",
            ".",
            "./source.py",
            "../source.py",
            "/source.py",
            "a//b.py",
            "a/./b.py",
            "a/../b.py",
            "a/",
            "a\\b.py",
            "C:source.py",
            "C:/source.py",
            "a\n.py",
            "a\x00.py",
            ".navi-ci-source-files.json",
            ".NAVI-CI-SOURCE-FILES.JSON",
        ]:
            with self.subTest(name=name):
                self.manifest([name])
                with self.assertRaisesRegex(ValueError, "Unsafe source selection name"):
                    quality.source_files(self.root)
        self.manifest(data=b"not JSON")
        with self.assertRaises(ValueError):
            quality.source_files(self.root)
        self.manifest([f"source-{index:06}.py" for index in range(100001)])
        with self.assertRaisesRegex(ValueError, "Invalid source selection manifest"):
            quality.source_files(self.root)
        self.manifest(data=json.dumps({"version": 1, "files": ["source.py"]}).encode("utf-16"))
        with self.assertRaises(UnicodeDecodeError):
            quality.source_files(self.root)

    def test_manifest_size_path_and_selected_missing_files_fail(self):
        self.write("source.py")
        manifest = self.manifest(data=b" " * (16 * 1024 * 1024 + 1))
        with self.assertRaisesRegex(ValueError, "bound"):
            quality.source_files(self.root)
        self.manifest(["source.py"])
        for path in [
            str(manifest.relative_to(self.root)),
            str(self.root / "other.json"),
            str(self.root.parent / manifest.name),
            str(self.root) + "/./" + manifest.name,
            str(self.root) + "/sub/../" + manifest.name,
            str(self.root) + "//" + manifest.name,
            str(self.root) + "/\n" + manifest.name,
        ]:
            with self.subTest(path=path):
                os.environ["NAVI_CI_SOURCE_FILES"] = path
                with self.assertRaisesRegex(ValueError, "ordinary workspace manifest"):
                    quality.source_files(self.root)
        self.manifest(["missing.py"])
        with self.assertRaisesRegex(ValueError, "missing or not an ordinary"):
            quality.source_files(self.root)
        (self.root / "directory.py").mkdir()
        self.manifest(["directory.py"])
        with self.assertRaisesRegex(ValueError, "missing or not an ordinary"):
            quality.source_files(self.root)

    def test_linked_manifest_or_first_party_input_is_rejected_but_artifacts_are_excluded(self):
        target = self.write("target/original.py")
        manifest = self.manifest(["source.py"])
        saved = self.root / "selection.json"
        manifest.rename(saved)
        try:
            manifest.symlink_to(saved)
        except OSError as error:
            if os.name == "nt" and getattr(error, "winerror", None) == 1314:
                self.skipTest("Windows denied symlink creation; no privilege changes attempted")
            raise
        with self.assertRaisesRegex(ValueError, "ordinary workspace manifest"):
            quality.source_files(self.root)
        manifest.unlink()
        (self.root / "source.py").symlink_to(target)
        self.manifest(["source.py"])
        with self.assertRaisesRegex(ValueError, "ordinary first-party"):
            quality.source_files(self.root)
        (self.root / "Sources").symlink_to(target.parent, target_is_directory=True)
        self.manifest(["Sources/original.py"])
        with self.assertRaisesRegex(ValueError, "ordinary first-party"):
            quality.source_files(self.root)
        (self.root / "Artifacts").mkdir()
        (self.root / "Artifacts/header.h").symlink_to(target)
        self.manifest(["Artifacts/header.h"])
        self.assertEqual(quality.source_files(self.root), [])
