"""Check the SDK-owned distribution plan, source identity and credential boundaries."""

import hashlib
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

    def test_combined_nuget_plan_requires_both_producers_and_checkout_owned_files(self):
        with tempfile.TemporaryDirectory() as disposable:
            source = Path(disposable).resolve()
            config = source / "sdk-distribution.toml"
            plan = dedent(
                """
                [macos]
                commands = [["make", "check-distribution"]]
                [windows]
                commands = [["sdk-tooling", "windows", "distribution/check.py"]]
                [nuget]
                pack = "distribution/pack.py"
                """
            )
            config.write_text(plan, encoding="utf-8")
            parse = DISPATCH["distribution_plan"]
            with self.assertRaisesRegex(ValueError, "require both platforms"):
                parse(source)
            (source / "global.json").touch()
            (source / "native.toml").touch()
            pack = source / "distribution/pack.py"
            pack.parent.mkdir()
            pack.touch()
            self.assertEqual(parse(source)["nuget"], "distribution/pack.py")
            config.write_text(
                plan.replace("distribution/pack.py", "distribution/../pack.py"), encoding="utf-8"
            )
            with self.assertRaisesRegex(ValueError, "nuget.pack"):
                parse(source)

    def test_only_combined_nuget_defers_macos_until_the_windows_build(self):
        with tempfile.TemporaryDirectory() as disposable:
            source = Path(disposable).resolve()
            (source / "global.json").touch()
            (source / "native.toml").touch()
            pin = "b" * 40
            normal = {"macos": [["make", "check-distribution"]], "windows": [["dotnet", "test"]]}
            workflow_plan = DISPATCH["workflow_plan"]
            plan = workflow_plan(source, normal, pin)
            self.assertEqual(
                plan,
                {
                    "platforms": '["macos", "windows"]',
                    "revision": pin,
                    "dotnet": "true",
                    "xcframeworks": "true",
                    "nuget": "false",
                },
            )
            with self.assertRaisesRegex(ValueError, "requires a combined NuGet"):
                workflow_plan(source, normal, pin, windows_on_arm=True)
            combined = normal | {"nuget": "distribution/pack.py"}
            plan = workflow_plan(source, combined, pin, windows_on_arm=True)
            self.assertEqual(json.loads(plan["platforms"]), ["windows"])
            self.assertEqual(plan["nuget"], "true")

    def test_macos_distribution_packs_after_regenerating_native_artifacts(self):
        with tempfile.TemporaryDirectory() as disposable:
            root = Path(disposable).resolve()
            source, tooling = root / "sdk", root / "tooling"
            source.mkdir()
            (source / "native.toml").touch()
            launcher = tooling / "ci" / "run_sdk_tooling.py"
            launcher.parent.mkdir(parents=True)
            launcher.touch()
            framework, package = root / "framework", root / "package"
            dotnet = root / "dotnet-x64/dotnet"
            log = []

            def execute(source, platform, commands, env, temp, tooling, *, xcframework_output):
                self.assertTrue(temp.is_dir())
                self.assertEqual(xcframework_output, framework)
                self.assertEqual(platform, "macos")
                log.append("rebuild")

            def macos_x64_environment(checkout, env, selected):
                self.assertEqual(checkout, source)
                self.assertEqual(selected, dotnet)
                return {"DOTNET_BIN": str(dotnet)}

            def pack(source, env, cli, script, output, *, macos_x64_env):
                self.assertEqual(script, "distribution/pack.py")
                self.assertEqual(output, package)
                self.assertEqual(macos_x64_env, {"DOTNET_BIN": str(dotnet)})
                log.append("pack")
                return "b" * 64

            main = DISPATCH["main"]
            with (
                patch("pathlib.Path.cwd", return_value=source),
                patch.dict(
                    main.__globals__,
                    verify_checkout=lambda *_args, **_kwargs: log.append("verify"),
                    child_environment=lambda _: {},
                    tooling_revision=lambda *_: "c" * 40,
                    distribution_plan=lambda _: {"macos": [], "nuget": "distribution/pack.py"},
                    execute=execute,
                    check_dotnet_version=lambda _source, env: env,
                    require_macos_dotnet_rid=lambda _source, _env, rid: log.append(rid),
                    macos_x64_environment=macos_x64_environment,
                    pack_combined_nuget=pack,
                    write_output=lambda **values: log.append(values),
                ),
                patch.dict(os.environ, GITHUB_OUTPUT="fixture"),
            ):
                main(
                    [
                        "macos",
                        "window-management",
                        "a" * 40,
                        "--tooling-repository",
                        "example-org/sdk-tooling",
                        "--tooling",
                        str(tooling),
                        "--xcframework-output",
                        str(framework),
                        "--nuget-artifact",
                        str(package),
                        "--macos-x64-dotnet",
                        str(dotnet),
                    ]
                )
            self.assertEqual(
                log, ["verify", "verify", "osx-arm64", "rebuild", "pack", {"sha256": "b" * 64}]
            )

    def test_combined_nuget_consumers_receive_one_byte_identical_package(self):
        with tempfile.TemporaryDirectory() as disposable:
            work = Path(disposable).resolve()
            source = work / "checkout"
            builds = source / ".native-build/packages"
            builds.mkdir(parents=True)
            (source / "mise.toml").touch()
            output = work / "accepted"
            pack = DISPATCH["pack_combined_nuget"]
            consume = DISPATCH["consume_package"]
            cli = ("verified-tooling",)
            script = "distribution/pack.py"
            package_name = "Example.Sdk.0.1.0.nupkg"
            package_bytes = b"the same native package for all consumers"
            checksum = hashlib.sha256(package_bytes).hexdigest()
            receipt = {
                "package": package_name,
                "runtimes": ["osx-arm64", "osx-x64", "win-x64"],
                "sha256": checksum,
            }
            seen = []
            hosts = []
            x64_env = {"DOTNET_BIN": "/opt/dotnet-x64/dotnet"}

            def run(checkout, env, *argv):
                self.assertEqual(checkout, source)
                if argv[:3] == ("mise", "exec", "--"):
                    self.assertEqual(env, {})
                    self.assertEqual(
                        argv[3:-1],
                        (
                            *cli,
                            "python",
                            script,
                            "--runtime",
                            "osx-arm64",
                            "--runtime",
                            "osx-x64",
                            "--runtime",
                            "win-x64",
                            "--result",
                        ),
                    )
                    (builds / package_name).write_bytes(package_bytes)
                    (builds / f"{package_name}.json").write_text(
                        json.dumps(receipt), encoding="utf-8"
                    )
                    Path(argv[-1]).write_text(package_name, encoding="utf-8")
                else:
                    self.assertEqual(argv[:2], (*cli, "nuget-consumer"))
                    seen.append((env, Path(argv[2]).read_bytes(), argv[3:]))

            with patch.dict(pack.__globals__, run=run, require_windows_host=hosts.append):
                self.assertEqual(
                    pack(source, {}, cli, script, output, macos_x64_env=x64_env), checksum
                )
                for host in ("x64", "arm64"):
                    self.assertEqual(
                        consume(source, {}, cli, output, expected=checksum, windows_host=host),
                        checksum,
                    )
                with self.assertRaisesRegex(ValueError, "accepted digest"):
                    consume(source, {}, cli, output, expected="0" * 64)

            self.assertEqual(hosts, ["x64", "arm64"])
            self.assertEqual(
                seen,
                [
                    ({}, package_bytes, ()),
                    (x64_env, package_bytes, ()),
                    *[({}, package_bytes, ("--windows-architecture", "x64"))] * 2,
                ],
            )
            self.assertEqual(
                {path.name for path in output.iterdir()}, {package_name, f"{package_name}.json"}
            )

    def test_combined_nuget_preserves_the_whole_dependency_feed_across_hosts(self):
        with tempfile.TemporaryDirectory() as disposable:
            work = Path(disposable).resolve()
            source = work / "checkout"
            builds = source / ".native-build/packages"
            builds.mkdir(parents=True)
            output = work / "accepted"
            primary = "Z.Sdk.0.1.0.nupkg"
            dependencies = ("A.First.0.1.0.nupkg", "B.Second.0.1.0.nupkg")
            pack, consume = DISPATCH["pack_combined_nuget"], DISPATCH["consume_package"]
            x64_env = {"DOTNET_BIN": "/opt/dotnet-x64/dotnet"}
            calls = []

            def publish(path, data, *, rid="win-x64"):
                path.write_bytes(data)
                path.with_suffix(".nupkg.json").write_text(
                    json.dumps(
                        {
                            "package": path.name,
                            "runtimes": ["osx-arm64", "osx-x64", rid],
                            "sha256": hashlib.sha256(data).hexdigest(),
                        }
                    ),
                    encoding="utf-8",
                )

            def run(_, env, mode, *argv):
                if mode == "python":
                    names = (primary, *reversed(dependencies))
                    for name in names:
                        publish(builds / name, name.encode("ascii"))
                    Path(argv[-1]).write_bytes(("\n".join(names) + "\n").encode("ascii"))
                    return
                self.assertEqual(mode, "nuget-consumer")
                self.assertEqual(Path(argv[0]).name, primary)
                self.assertEqual(Path(argv[0]).read_bytes(), primary.encode("ascii"))
                paths = [Path(argv[0]).parent / "dependencies" / name for name in dependencies]
                supplied = tuple(
                    argument for path in paths for argument in ("--dependency", str(path))
                )
                self.assertEqual(argv[1:5], supplied)
                self.assertEqual(
                    [path.read_bytes() for path in paths],
                    [name.encode("ascii") for name in dependencies],
                )
                calls.append((env, argv[5:]))

            with patch.dict(
                pack.__globals__,
                run=run,
                require_windows_host=lambda host: self.assertEqual(host, "arm64"),
            ):
                checksum = pack(
                    source, {}, (), "distribution/pack.py", output, macos_x64_env=x64_env
                )
                consume(source, {}, (), output, expected=checksum, windows_host="arm64")
                self.assertEqual(
                    calls, [({}, ()), (x64_env, ()), ({}, ("--windows-architecture", "x64"))]
                )
                candidate = output / "dependencies" / dependencies[0]
                publish(candidate, b"replacement package with a replacement receipt")
                with self.assertRaisesRegex(ValueError, "accepted digest"):
                    consume(source, {}, (), output, expected=checksum)
                publish(candidate, dependencies[0].encode("ascii"), rid="win-arm64")
                with self.assertRaisesRegex(ValueError, "accepted receipt"):
                    consume(source, {}, (), output, expected=checksum)
                publish(candidate, dependencies[0].encode("ascii"))

                extra = candidate.with_suffix(".tmp")
                extra.touch()
                with self.assertRaisesRegex(ValueError, "unexpected or missing members"):
                    consume(source, {}, (), output, expected=checksum)
                extra.unlink()

                def mutate(*_):
                    publish(candidate, b"replacement during the independent test")

                with (
                    patch.dict(pack.__globals__, run=mutate),
                    self.assertRaisesRegex(ValueError, "changed during consumer acceptance"),
                ):
                    consume(source, {}, (), output, expected=checksum)

    def test_combined_nuget_rejects_ambiguous_or_nonportable_packer_output(self):
        validate = DISPATCH["validate_package_names"]
        for names in (
            [],
            ["Example.nupkg", "example.nupkg"],
            ["Example.nupkg", ""],
            [".nupkg"],
            ["../Example.nupkg"],
            ["subdir/Example.nupkg"],
            [r"subdir\Example.nupkg"],
            ["Example.nupkg "],
            ["Example.nupkg\r"],
            ["NUL.1.0.nupkg"],
        ):
            with self.subTest(names=names), self.assertRaisesRegex(ValueError, "ordinary"):
                validate(names)

    def test_macos_x64_consumer_checks_the_exact_host_version_and_rid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            dotnet = root / "x64/dotnet"
            dotnet.parent.mkdir()
            dotnet.touch()
            (root / "global.json").write_text(
                '{"sdk":{"version":"10.0.403","rollForward":"disable"}}', encoding="utf-8"
            )
            select = DISPATCH["macos_x64_environment"]
            architecture = "x64"
            version = "10.0.403"

            def run(checkout, env, executable, option, *, stdout):
                self.assertEqual(checkout, root)
                self.assertEqual(Path(executable), dotnet)
                self.assertEqual(env["DOTNET_ROOT"], str(dotnet.parent))
                self.assertEqual(env["DOTNET_ROOT_X64"], str(dotnet.parent))
                self.assertEqual(env["DOTNET_HOST_PATH"], str(dotnet))
                return version if option == "--version" else f"\n RID:         osx-{architecture}\n"

            with (
                patch.object(sys, "platform", "darwin"),
                patch.dict(select.__globals__, run=run),
            ):
                env = select(root, {"DOTNET_BIN": "/arm64/dotnet", "DOTNET_ROOT": "/arm64"}, dotnet)
                self.assertEqual(env["DOTNET_BIN"], str(dotnet))
                architecture = "arm64"
                with self.assertRaisesRegex(ValueError, "must report osx-x64"):
                    select(root, {}, dotnet)
                version = "10.0.404"
                with self.assertRaisesRegex(ValueError, "exact .NET version"):
                    select(root, {}, dotnet)

    def test_windows_consumer_refuses_an_unexpected_runner_architecture(self):
        require = DISPATCH["require_windows_host"]
        for expected, actual, other in (("arm64", "ARM64", "x64"), ("x64", "AMD64", "arm64")):
            with (
                self.subTest(expected=expected),
                patch.object(sys, "platform", "win32"),
                patch.dict(require.__globals__, machine=lambda: actual),
            ):
                require(expected)
                with self.assertRaisesRegex(ValueError, f"Windows {other} worker"):
                    require(other)

    def test_combined_nuget_rejects_changes_during_acceptance(self):
        with tempfile.TemporaryDirectory() as disposable:
            work = Path(disposable).resolve()
            artifact = work / "Example.Sdk.0.1.0.nupkg"
            artifact.write_bytes(b"package")
            checksum = hashlib.sha256(b"package").hexdigest()
            artifact.with_suffix(".nupkg.json").write_text(
                json.dumps(
                    {
                        "package": artifact.name,
                        "runtimes": ["osx-arm64", "osx-x64", "win-x64"],
                        "sha256": checksum,
                    }
                ),
                encoding="utf-8",
            )
            consume = DISPATCH["consume_package"]

            def run(*_):
                artifact.write_bytes(b"replacement")

            with (
                patch.dict(consume.__globals__, run=run),
                self.assertRaisesRegex(ValueError, "changed during consumer acceptance"),
            ):
                consume(work, {}, (), work, expected=checksum)

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
            has_pin = DISPATCH["has_dotnet_pin"]
            self.assertFalse(has_pin(source))
            settings = source / "global.json"
            settings.write_text(
                '{"sdk":{"version":"10.0.403","rollForward":"disable"}}',
                encoding="utf-8",
            )
            self.assertTrue(has_pin(source))
            settings.unlink()
            settings.mkdir()
            with self.assertRaisesRegex(ValueError, "ordinary SDK file"):
                has_pin(source)

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
