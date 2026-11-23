"""Check the SDK-owned distribution plan, source identity and credential boundaries."""

import json
import os
import runpy
import subprocess
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "tools/check-sdk-distribution.py"
DISPATCH = runpy.run_path(str(SCRIPT))


class DistributionTests(unittest.TestCase):
    def distribution_plan(self, content):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            (root / "sdk-distribution.toml").write_text(dedent(content), encoding="utf-8")
            return DISPATCH["distribution_plan"](root)

    def test_distribution_plan_uses_the_projects_commands_and_verified_archives(self):
        checksum = "a" * 64
        plan = self.distribution_plan(
            f"""
            [macos]
            commands = [
              ["cargo", "fetch", "--locked"],
              ["python", "checks/consumer.py", "--artifacts", "{{temp}}/sdk artifacts"],
            ]

            [[macos.archives]]
            url = "https://example.invalid/helper.tar.gz"
            sha256 = "{checksum}"
            bin = "tool/bin"

            [windows]
            commands = [["dotnet", "run", "--project", "Windows Fixture.csproj"]]
            """
        )
        self.assertEqual(
            plan["macos"]["commands"],
            [
                ["cargo", "fetch", "--locked"],
                ["python", "checks/consumer.py", "--artifacts", "{temp}/sdk artifacts"],
            ],
        )
        self.assertEqual(
            plan["macos"]["archives"],
            [
                {
                    "url": "https://example.invalid/helper.tar.gz",
                    "sha256": checksum,
                    "bin": "tool/bin",
                }
            ],
        )
        self.assertEqual(
            plan["windows"]["commands"],
            [["dotnet", "run", "--project", "Windows Fixture.csproj"]],
        )
        self.assertFalse(plan["windows"]["archives"])

    def test_distribution_plan_accepts_a_project_with_only_one_platform(self):
        for platform in ("macos", "windows"):
            with self.subTest(platform=platform):
                plan = self.distribution_plan(f'[{platform}]\ncommands = [["make", "check"]]')
                self.assertEqual(set(plan), {platform})

    def test_distribution_plan_rejects_unknown_settings_and_invalid_commands(self):
        plans = (
            "",
            '[linux]\ncommands = [["make", "check"]]',
            '[macos]\ncommands = [["make", "check"]]\nrunner = "custom"',
            "[macos]",
            '[macos]\ncommands = "make check"',
            '[macos]\ncommands = ["make", "check"]',
            "[macos]\ncommands = []",
            "[macos]\ncommands = [[]]",
            '[macos]\ncommands = [["", "check"]]',
            '[macos]\ncommands = [["make", 1]]',
            '[macos]\ncommands = [["make", "first\\nsecond"]]',
        )
        for content in plans:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self.distribution_plan(content)

    def test_distribution_plan_rejects_unsafe_archive_metadata(self):
        valid = {
            "url": "https://example.invalid/helper.tar.gz",
            "sha256": "a" * 64,
            "bin": "tool/bin",
        }
        invalid = (
            {"url": "http://example.invalid/helper.tar.gz"},
            {"url": "https:///helper.tar.gz"},
            {"url": "https://user:pass@example.invalid/helper.tar.gz"},
            {"url": "https://example.invalid/helper.tar.gz#fragment"},
            {"sha256": "A" * 64},
            {"sha256": "a" * 63},
            {"bin": "/usr/bin"},
            {"bin": "C:/Windows"},
            {"bin": "../bin"},
            {"bin": "tool/../bin"},
            {"bin": "tool\\bin"},
            {"exec": "helper"},
        )
        for changed in invalid:
            with self.subTest(changed=changed), self.assertRaises(ValueError):
                fields = valid | changed
                archive = "\n".join(
                    f"{name} = {json.dumps(value)}" for name, value in fields.items()
                )
                self.distribution_plan(
                    '[macos]\ncommands = [["make", "check"]]\n[[macos.archives]]\n' + archive
                )

    def test_windows_commands_use_literal_arguments_and_share_one_scratch_directory(self):
        revision = "a" * 40
        with tempfile.TemporaryDirectory(prefix="distribution test ") as directory:
            root = Path(directory).resolve()
            temp = root / "scratch files"
            temp.mkdir()
            tooling = root / "shared tools"
            script = root / "fixture test.py"
            script.write_text(
                dedent(
                    """
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    mode, output, tooling, argument = sys.argv[1:]
                    path = Path(output)
                    details = {
                        "tooling": tooling,
                        "arg": argument,
                        "source": os.environ["GITHUB_SHA"],
                        "temp": str(path.parent),
                        "credential": "GH_TOKEN" in os.environ,
                    }
                    assert Path(os.environ["SDK_DISTRIBUTION_TEMP"]) == path.parent
                    if mode == "write":
                        path.write_text(json.dumps(details), encoding="utf-8")
                    else:
                        assert json.loads(path.read_text(encoding="utf-8")) == details
                    """
                ),
                encoding="utf-8",
            )
            arguments = ["{temp}/result.json", "{tooling}", "one argument with spaces & $HOME"]
            settings = {
                "commands": [
                    ["python", script.name, "write", *arguments],
                    ["python", script.name, "verify", *arguments],
                ],
                "archives": [],
            }
            with patch.dict(os.environ, {"GH_TOKEN": "unrelated token"}):
                env = DISPATCH["child_environment"](revision, tooling, temp=temp)
                DISPATCH["execute"](root, "windows", settings, env, temp, tooling)
            self.assertEqual(
                json.loads((temp / "result.json").read_text(encoding="utf-8")),
                {
                    "tooling": str(tooling),
                    "arg": arguments[-1],
                    "source": revision,
                    "temp": str(temp),
                    "credential": False,
                },
            )

    def test_sdk_child_identity_replaces_workflow_sha_without_credentials(self):
        environment = {
            "NAVI_CI_SOURCE_COMMIT": "a" * 40,
            "NAVI_CI_SOURCE_FINGERPRINT": "b" * 64,
            "NAVI_CI_SOURCES": "manifest.json",
            "GITHUB_SHA": "a" * 40,
            "GITHUB_TOKEN": "test token",
            "GH_TOKEN": "test token",
            "ACTIONS_RUNTIME_TOKEN": "test token",
            "MISE_GITHUB_TOKEN": "test token",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": "unrelated authorization header",
            "GIT_CONFIG_VALUE_1": "unrelated credential helper",
        }
        with patch.dict(os.environ, environment, clear=True):
            actual = DISPATCH["child_environment"]("c" * 40, SCRIPT.parent)
        self.assertEqual(actual["GITHUB_SHA"], "c" * 40)
        self.assertEqual(actual["NAVI_SDK_TOOLING_SOURCE"], str(SCRIPT.parent))
        self.assertEqual(actual["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(actual["GIT_CONFIG_VALUE_0"], "")
        self.assertFalse(
            (environment.keys() - {"GITHUB_SHA", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"})
            & actual.keys()
        )

    def test_pin_is_one_full_tooling_commit(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            manifest = root / "tools/tooling-sources.json"
            manifest.parent.mkdir()
            repository = "example-org/verification-tools"
            expected = "a" * 40
            manifest.write_text(json.dumps({repository: expected}), encoding="utf-8")
            self.assertEqual(DISPATCH["tooling_revision"](root, repository), expected)
            for invalid in (
                {repository: "main"},
                {repository: expected, "naviapps/other": expected},
            ):
                with self.subTest(invalid=invalid):
                    manifest.write_text(json.dumps(invalid), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "one full lowercase Git SHA"):
                        DISPATCH["tooling_revision"](root, repository)

    def test_pin_uses_the_exact_mise_task_when_declared(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            repository = "example-org/verification-tools"
            legacy = root / "tools/tooling-sources.json"
            legacy.parent.mkdir()
            legacy.write_text(json.dumps({repository: "a" * 40}), encoding="utf-8")
            config = root / "mise.toml"
            prefix = f"uvx --from git+https://github.com/{repository}.git@"
            suffix = " sdk-tooling"
            revision = "b" * 40

            def write(location):
                config.write_text(
                    f"[tasks.sdk-tooling]\nrun = {json.dumps(location)}\n", encoding="utf-8"
                )

            write(prefix + revision + suffix)
            self.assertEqual(DISPATCH["tooling_revision"](root, repository), revision)
            for location in (
                prefix + "main" + suffix,
                prefix + revision + "&token=override" + suffix,
                prefix.replace(repository, "example-org/other") + revision + suffix,
                prefix + revision + " another-cli",
            ):
                with self.subTest(location=location):
                    write(location)
                    with self.assertRaises(ValueError):
                        DISPATCH["tooling_revision"](root, repository)

    def test_windows_dotnet_is_optional_and_uses_the_project_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            read_pin = DISPATCH["windows_dotnet_version"]
            self.assertEqual(read_pin(source), "")
            settings = source / "mise.toml"
            settings.write_text('[tools]\ndotnet = "10.0.403"\n', encoding="utf-8")
            self.assertEqual(read_pin(source), "10.0.403")
            settings.write_text(
                '[tools]\ndotnet = { version = "10.0.403", os = ["macos"] }\n', encoding="utf-8"
            )
            self.assertEqual(read_pin(source), "")
            settings.write_text('[tools]\ndotnet = "10.0.x"\n', encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "one exact .NET SDK version"):
                read_pin(source)

    def test_sdk_checkout_rejects_off_main_commit(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable).resolve()
            repository = "example-org/preset-sdk"

            def git(*args):
                return subprocess.check_output(
                    [
                        "git",
                        "-c",
                        "user.name=Distribution Test",
                        "-c",
                        "user.email=distribution@example.invalid",
                        "-c",
                        "commit.gpgsign=false",
                        "-c",
                        f"core.hooksPath={os.devnull}",
                        *args,
                    ],
                    cwd=root,
                    text=True,
                ).strip()

            git("init", "--quiet", "--initial-branch=main")
            git("remote", "add", "origin", f"https://github.com/{repository}")
            git("commit", "--quiet", "--allow-empty", "-m", "Public main")
            stable = git("rev-parse", "HEAD")
            git("update-ref", "refs/remotes/origin/main", stable)
            verify = DISPATCH["verify_checkout"]
            verify(root, stable, repository, os.environ, require_main=True)
            git("commit", "--quiet", "--allow-empty", "-m", "Unreviewed change")
            with self.assertRaises(subprocess.CalledProcessError):
                verify(root, git("rev-parse", "HEAD"), repository, os.environ, require_main=True)


if __name__ == "__main__":
    unittest.main()
