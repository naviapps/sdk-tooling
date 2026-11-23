"""Check the SDK-owned distribution plan, source identity and credential boundaries."""

import json
import os
import runpy
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent
from unittest.mock import patch

SCRIPT = Path(__file__).resolve().parents[1] / "ci/check_distribution.py"
DISPATCH = runpy.run_path(str(SCRIPT))


class DistributionTests(unittest.TestCase):
    def distribution_plan(self, content):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            (root / "sdk-distribution.toml").write_text(dedent(content), encoding="utf-8")
            return DISPATCH["distribution_plan"](root)

    def test_distribution_plan_uses_the_projects_commands(self):
        plan = self.distribution_plan(
            """
            [macos]
            commands = [
              ["cargo", "fetch", "--locked"],
              ["python", "checks/consumer.py", "--artifacts", "{temp}/sdk artifacts"],
            ]

            [windows]
            commands = [["dotnet", "run", "--project", "Windows Fixture.csproj"]]
            """
        )
        self.assertEqual(
            plan["macos"],
            [
                ["cargo", "fetch", "--locked"],
                ["python", "checks/consumer.py", "--artifacts", "{temp}/sdk artifacts"],
            ],
        )
        self.assertEqual(
            plan["windows"],
            [["dotnet", "run", "--project", "Windows Fixture.csproj"]],
        )

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

    def test_windows_commands_use_literal_arguments_and_share_one_scratch_directory(self):
        revision = "a" * 40
        with tempfile.TemporaryDirectory(prefix="distribution test ") as directory:
            root = Path(directory).resolve()
            temp = root / "scratch files"
            temp.mkdir()
            tooling = root / "shared tools"
            script = tooling / "ci/run_sdk_tooling.py"
            script.parent.mkdir(parents=True)
            script.write_text(
                dedent(
                    """
                    import json
                    import os
                    import sys
                    import tempfile
                    from pathlib import Path

                    mode, output, tooling, argument = sys.argv[1:]
                    path = Path(output)
                    details = {
                        "tooling": tooling,
                        "arg": argument,
                        "source": os.environ["GITHUB_SHA"],
                        "temp": str(path.parent),
                        "child_temp_root": str(Path(tempfile.gettempdir()).resolve()),
                        "credential": "GH_TOKEN" in os.environ,
                    }
                    if mode == "write":
                        path.write_text(json.dumps(details), encoding="utf-8")
                    else:
                        assert json.loads(path.read_text(encoding="utf-8")) == details
                    """
                ),
                encoding="utf-8",
            )
            arguments = ["{temp}/result.json", "{tooling}", "one argument with spaces & $HOME"]
            commands = [
                ["sdk-tooling", "write", *arguments],
                ["python", "-B", "{tooling}/ci/run_sdk_tooling.py", "verify", *arguments],
            ]
            with patch.dict(
                os.environ,
                {"GH_TOKEN": "unrelated token", "RUNNER_TEMP": str(root), "TMPDIR": str(temp)},
            ):
                env = DISPATCH["child_environment"](revision)
                DISPATCH["execute"](root, "windows", commands, env, temp, tooling)
            self.assertEqual(
                json.loads((temp / "result.json").read_text(encoding="utf-8")),
                {
                    "tooling": str(tooling),
                    "arg": arguments[-1],
                    "source": revision,
                    "temp": str(temp),
                    "child_temp_root": str(root),
                    "credential": False,
                },
            )

    def test_macos_commands_use_the_pinned_cli_and_leave_explicit_make_arguments_intact(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            (root / "mise.toml").touch()
            launcher = root / "shared $tooling's path" / "ci/run_sdk_tooling.py"
            launcher.parent.mkdir(parents=True)
            launcher.touch()
            commands = [
                ["sdk-tooling", "docs"],
                ["make", "check-distribution"],
                ["mise", "run", "check-web-distribution", "--", "SDK_TOOLING=explicit command"],
            ]
            env = {"PATH": "fixture path"}
            execute = DISPATCH["execute"]
            with patch.dict(execute.__globals__) as globals_:
                invocations = []

                def run(source, environment, *command):
                    self.assertEqual(source, root)
                    invocations.append((environment, command))

                globals_["run"] = run
                execute(root, "macos", commands, env, root, launcher.parents[1])

            prefix = ("mise", "exec", "--")
            self.assertEqual(
                [command for _, command in invocations],
                [
                    ("mise", "trust", "mise.toml"),
                    ("mise", "install"),
                    (
                        *prefix,
                        sys.executable,
                        "-I",
                        "-B",
                        str(launcher),
                        "docs",
                    ),
                    (*prefix, "make", "check-distribution"),
                    (
                        *prefix,
                        "mise",
                        "run",
                        "check-web-distribution",
                        "--",
                        "SDK_TOOLING=explicit command",
                    ),
                ],
            )
            self.assertEqual([environment for environment, _ in invocations[:2]], [env] * 2)
            for environment, _ in invocations[2:]:
                self.assertIn("shared $$tooling", environment["SDK_TOOLING"])

    def test_missing_pinned_launcher_fails_before_running_commands(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            execute = DISPATCH["execute"]
            with self.assertRaisesRegex(ValueError, "must provide ci/run_sdk_tooling.py"):
                execute(root, "windows", [], {}, root, root / "tooling")

    def test_native_generation_runs_only_for_macos_with_a_regular_declaration(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable).resolve()
            select = DISPATCH["builds_xcframeworks"]
            self.assertFalse(select(root, "macos"))
            specification = root / "native.toml"
            specification.touch()
            self.assertTrue(select(root, "macos"))
            self.assertFalse(select(root, "windows"))
            specification.unlink()
            specification.mkdir()
            with self.assertRaisesRegex(ValueError, "ordinary SDK file: native.toml"):
                select(root, "macos")

    def test_native_generation_follows_pristine_checks_and_archives_the_verified_build(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable).resolve()
            source = root / "sdk"
            source.mkdir()
            (source / "Package.swift").touch()
            tooling = root / "pinned tooling"
            runner = tooling / "ci/run_sdk_tooling.py"
            runner.parent.mkdir(parents=True)
            runner.touch()
            output = root / "generated archive"
            env = {"GITHUB_SHA": "a" * 40}
            execute = DISPATCH["execute"]
            fetch = ["cargo", "fetch", "--locked"]
            for fetch_in_plan in (False, True):
                with (
                    self.subTest(fetch_in_plan=fetch_in_plan),
                    patch.dict(execute.__globals__) as globals_,
                ):
                    installed = ["make", "test-installed-consumers"]
                    plan = ([fetch] if fetch_in_plan else []) + [installed]
                    invocations = []

                    def run(checkout, environment, *command):
                        self.assertEqual(checkout, source)
                        self.assertEqual(environment["GITHUB_SHA"], env["GITHUB_SHA"])
                        invocations.append(command)

                    globals_["run"] = run
                    execute(
                        source,
                        "macos",
                        plan,
                        env,
                        root,
                        tooling,
                        xcframework_output=output,
                    )
                    pinned_cli = (sys.executable, "-I", "-B", str(runner))
                    expected = [
                        tuple(installed),
                        ("rustup", "target", "add", "aarch64-apple-darwin", "x86_64-apple-darwin"),
                    ]
                    if fetch_in_plan:
                        expected.insert(0, tuple(fetch))
                    else:
                        expected.append(tuple(fetch))
                    self.assertEqual(
                        invocations,
                        [
                            *expected,
                            (*pinned_cli, "xcframework"),
                            ("make", "test"),
                            (*pinned_cli, "swift-consumer", "--distribution"),
                            (
                                sys.executable,
                                "-I",
                                "-B",
                                str(SCRIPT.with_name("run_sdk_tooling.py")),
                                "xcframework-archive",
                                "--output",
                                str(output),
                                "--revision",
                                env["GITHUB_SHA"],
                                "--producer",
                                str(tooling),
                            ),
                        ],
                    )

    def test_sdk_child_identity_replaces_workflow_sha_without_credentials(self):
        environment = {
            "NAVI_CI_SOURCE_COMMIT": "a" * 40,
            "NAVI_CI_SOURCE_FINGERPRINT": "b" * 64,
            "NAVI_CI_SOURCES": "manifest.json",
            "NAVI_CI_SOURCE_FILES": "outside-selection.json",
            "navi_ci_source_files_sha256": "d" * 64,
            "NAVI_CI_ARTIFACTS": "outside-artifacts",
            "VM_RUNNER_SOURCE_MANIFEST": "another-source.json",
            "VM_RUNNER_SOURCE_FILES": "another-selection.json",
            "VM_RUNNER_ARTIFACTS_DIR": "another-output",
            "vm_runner_tools_root": "another-tools-root",
            "NAVI_SDK_TOOLING_SOURCE": "unrelated tooling checkout",
            "SDK_TOOLING": "unrelated tool command",
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
            actual = DISPATCH["child_environment"]("c" * 40)
        self.assertEqual(actual["GITHUB_SHA"], "c" * 40)
        self.assertEqual(actual["GIT_CONFIG_KEY_0"], "credential.helper")
        self.assertEqual(actual["GIT_CONFIG_VALUE_0"], "")
        self.assertFalse(
            (environment.keys() - {"GITHUB_SHA", "GIT_CONFIG_KEY_0", "GIT_CONFIG_VALUE_0"})
            & actual.keys()
        )

    def test_pin_requires_the_exact_mise_task(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable)
            repository = "example-org/verification-tools"
            config = root / "mise.toml"
            prefix = f"uvx --from git+https://github.com/{repository}.git@"
            suffix = " sdk-tooling"
            revision = "b" * 40

            def write(location):
                config.write_text(
                    f"[tasks.sdk-tooling]\nrun = {json.dumps(location)}\n", encoding="utf-8"
                )

            with self.assertRaisesRegex(ValueError, "mise.toml"):
                DISPATCH["tooling_revision"](root, repository)
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

    def test_windows_check_writes_the_shared_dotnet_pin(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            output = source / "github-output"
            env = dict(os.environ, GITHUB_OUTPUT=str(output))
            command = [sys.executable, "-I", "-B", str(SCRIPT), "dotnet-version"]
            missing = subprocess.run(command, cwd=source, env=env, capture_output=True, text=True)
            self.assertNotEqual(missing.returncode, 0)
            self.assertIn("exact Windows .NET SDK version", missing.stderr)
            (source / "mise.toml").write_text('[tools]\ndotnet = "10.0.403"\n', encoding="utf-8")
            subprocess.run(command, cwd=source, env=env, check=True)
            self.assertEqual(output.read_text(encoding="utf-8"), "dotnet=10.0.403\n")

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
