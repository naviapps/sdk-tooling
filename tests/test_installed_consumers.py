"""Exercise actual delivery, identity verification and isolation with a fake SDK builder."""

import hashlib
import json
import os
import plistlib
import struct
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from sdk_tooling import installed_consumers as driver
from sdk_tooling.artifacts import files, hashes

GIT_URL = "https://github.com/naviapps/fixture.git"
DEPENDENCY_URL = "https://github.com/naviapps/dependency.git"
FRAMEWORK = Path("Artifacts/CFixture.xcframework/macos-arm64_x86_64/CFixture.framework")
HEADER = "fixture.h"


class InstalledConsumersTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        work = Path(cls.enterClassContext(tempfile.TemporaryDirectory())).resolve()
        architecture = "x64" if os.name == "nt" else None
        env = driver._environment(work, rust=True, windows_architecture=architecture)
        if architecture:
            env, _ = driver.windows.native_msvc_environment(
                driver.windows.host_architecture(), env, target_architecture=architecture
            )
        root = Path(__file__).resolve().parents[1]
        env["RUSTUP_TOOLCHAIN"] = tomllib.loads((root / "rust-toolchain.toml").read_text())[
            "toolchain"
        ]["channel"]
        env["RUSTUP_AUTO_INSTALL"] = "0"
        cls.verifier = driver._prepare_identity_verifier(
            work, "rustc", env, driver.windows.MSVC_TARGETS.get(architecture)
        )

    def setUp(self):
        self.work = Path(self.enterContext(tempfile.TemporaryDirectory())).resolve()
        self.prepare_verifier = self.enterContext(
            patch.object(driver, "_prepare_identity_verifier", return_value=type(self).verifier)
        )

    def directory(self, name):
        path = self.work / name
        path.mkdir()
        return path

    def repository(self, name="sdk"):
        root = self.directory(name)
        driver._git(root, "init", "--quiet", "--initial-branch", "main")
        driver._git(root, "config", "user.name", "Fixture")
        driver._git(root, "config", "user.email", "fixture@example.invalid")
        (root / "producer").write_text("producer", encoding="utf-8")
        self.commit(root)
        return root

    @staticmethod
    def commit(root):
        driver._git(root, "add", ".")
        driver._git(root, "commit", "--quiet", "-m", "fixture")

    @staticmethod
    def manifest(packages):
        return (
            '[package]\nname = "outside-client"\nversion = "0.0.0"\nedition = "2024"\n'
            "[workspace]\n[dependencies]\n"
            + "".join(
                f'{name} = {{ git = "{GIT_URL}", rev = "@SDK_REVISION@" }}\n' for name in packages
            )
        )

    def rust_fixture(self, root, packages, dependency=None):
        fixture = root / "fixtures/rust-consumer"
        (fixture / "src").mkdir(parents=True)
        (fixture / "src/main.rs").write_text("fn main() {}\n", encoding="utf-8")
        template = self.manifest(packages)
        if dependency:
            template += f'dependency = {{ git = "{DEPENDENCY_URL}", rev = "{dependency}" }}\n'
        (fixture / "Cargo.toml.in").write_text(template, encoding="utf-8")
        (fixture / "Cargo.lock").write_text("producer state", encoding="utf-8")
        (root / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.99.1"\n', encoding="utf-8"
        )
        self.commit(root)

    def cli(self, root, argv, packages=("configuration-storage",), **options):
        return driver.check_installed(
            root=root,
            git_url=GIT_URL,
            rust_packages=packages,
            framework=FRAMEWORK,
            header=HEADER,
            argv=argv,
            **options,
        )

    def test_complete_bundle_delivers_exact_source_through_canonical_url(self):
        root = self.repository()
        rev, tree = driver._source_revision(root, None)
        bundle = self.work / "sdk.bundle"
        driver._git(root, "bundle", "create", str(bundle), "HEAD")
        task = self.directory("task")
        env = driver._environment(task, rust=True)
        mirror = driver._prepare_git_delivery(bundle, task, env, GIT_URL, rev, tree)
        self.assertEqual(
            driver._git(mirror, "rev-parse", "--is-shallow-repository", env=env), "false"
        )
        remote = self.work / "consumer-checkout"
        driver._git(task, "clone", GIT_URL, str(remote), env=env)
        self.assertEqual(driver._source_revision(remote, rev), (rev, tree))
        self.assertEqual(driver._source_revision(root, rev), (rev, tree))

    def dependency_bundle(self, name="dependency", index=0):
        source = self.repository(name)
        (source / "producer").write_text("distinct " + name, encoding="utf-8")
        self.commit(source)
        revision, _ = driver._source_revision(source, None)
        bundle = self.work / f"{index}.bundle"
        driver._git(source, "bundle", "create", str(bundle), "HEAD")
        row = {
            "repository": "naviapps/" + name,
            "url": "https://github.com/naviapps/" + name + ".git",
            "revision": revision,
            "bundle": bundle.name,
            "sha256": hashlib.sha256(bundle.read_bytes()).hexdigest(),
        }
        return source, row

    def test_additional_sdk_uses_exact_file_only_delivery(self):
        root = self.repository()
        source, row = self.dependency_bundle()
        revision = row["revision"]
        self.rust_fixture(root, ("configuration-storage",), revision)
        producer, tree = driver._source_revision(root, None)
        extra = f"git+{DEPENDENCY_URL}?rev={revision}#{revision}"
        sources = {
            "configuration-storage": f"git+{GIT_URL}?rev={producer}#{producer}",
            "dependency": extra,
            "workspace-crate": extra,
            "registry-crate": "registry+https://github.com/rust-lang/crates.io-index",
        }
        manifest = self.work / "manifest.json"
        manifest.write_text(json.dumps([row]), encoding="utf-8")
        original = driver._run_rust

        def cargo(command, *, cwd, env):
            cache = Path(env["CARGO_HOME"])
            if command[1] == "generate-lockfile":
                self.assertFalse(cwd.is_relative_to(root))
                self.assertEqual(list(cache.iterdir()), [])
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
                for name in ("GH_TOKEN", "GIT_CONFIG_COUNT", "NAVI_CI_SOURCES"):
                    self.assertNotIn(name, env)
                for url, pin in ((GIT_URL, producer), (DEPENDENCY_URL, revision)):
                    checkout = cache / pin
                    driver._git(cwd, "clone", url, str(checkout), env=env)
                    self.assertEqual(driver._git(checkout, "rev-parse", "HEAD", env=env), pin)
                (cwd / "Cargo.lock").write_text(
                    "".join(
                        f'[[package]]\nname = "{name}"\nsource = "{identity}"\n'
                        for name, identity in sources.items()
                    ),
                    encoding="utf-8",
                )
            if command[1] == "metadata":
                marker = cache / "Cargo.toml"
                marker.write_text("", encoding="utf-8")
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "packages": [
                                {"name": name, "source": identity, "manifest_path": str(marker)}
                                for name, identity in sources.items()
                            ]
                        }
                    )
                )
            return SimpleNamespace(stdout="")

        pin = {"git": DEPENDENCY_URL, "rev": revision}
        for index, delivery in enumerate((None, source)):
            report = self.work / f"evidence-{index}.json"
            with (
                self.subTest(delivery=delivery),
                patch.object(
                    driver,
                    "_run_rust",
                    side_effect=lambda *args, **kw: original(*args, runner=cargo, **kw),
                ),
                patch.object(driver.shutil, "which", return_value="prepared-cargo"),
                patch.dict(
                    os.environ,
                    {
                        "GH_TOKEN": "fixture",
                        "GIT_CONFIG_COUNT": "0",
                        "NAVI_CI_SOURCES": str(manifest) if delivery is None else "",
                    },
                ),
            ):
                argv = ["--language", "rust", "--evidence", str(report)]
                if delivery:
                    argv += ["--git-dependency-source", str(delivery)]
                self.cli(root, argv, rust_git_dependencies={"dependency": pin})
                evidence = json.loads(report.read_text(encoding="utf-8"))
                self.assertEqual(evidence["state"], "passed")
                self.assertEqual(evidence["rust"]["mirrorTree"], tree)
                self.assertEqual(evidence["rust"]["gitIdentity"], sources["configuration-storage"])
                self.assertEqual(evidence["rust"]["gitDependencies"], {"dependency": extra})

    def test_additional_sdk_refuses_wrong_pin_checksum_or_ambiguous_bundle(self):
        _, row = self.dependency_bundle()
        manifest = self.work / "manifest.json"
        pin = {"git": DEPENDENCY_URL, "rev": row["revision"]}
        pins = {row["repository"]: pin}
        with patch.dict(os.environ, {"NAVI_CI_SOURCES": str(manifest)}):
            for rows, message in (
                ([], "Missing or ambiguous"),
                ([row, dict(row, bundle="1.bundle")], "Missing or ambiguous"),
                ([row, dict(row, repository="NaviApps/dependency")], "Missing or ambiguous"),
                ([row, {"repository": "naviappſ/dependency"}], "Missing or ambiguous"),
                ([row, {"repository": "other", "bundle": "0.bundle"}], "bundle identity"),
                ([dict(row, revision="a" * 40)], "bundle identity"),
                ([dict(row, bundle="../0.bundle")], "bundle identity"),
                ([dict(row, sha256="invalid")], "bundle checksum"),
            ):
                with self.subTest(rows=rows):
                    manifest.write_text(json.dumps(rows), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        driver._ci_dependency_bundles(pins, type(self).verifier)
            root = self.repository()
            with self.assertRaisesRegex(ValueError, "Choose either"):
                self.cli(
                    root,
                    ["--language", "rust", "--git-dependency-source", "0.bundle"],
                    rust_git_dependencies={"dependency": pin},
                )

    def test_multiple_private_repositories_and_workspace_crates_use_exact_git_identities(self):
        root = self.repository()
        first, first_row = self.dependency_bundle()
        second, second_row = self.dependency_bundle("layout", 1)
        origins = (first_row, second_row)
        dependencies = {
            name: {"git": row["url"], "rev": row["revision"]}
            for name, row in (
                ("dependency", first_row),
                ("layout", second_row),
                ("layout-native", second_row),
            )
        }
        self.rust_fixture(root, ("configuration-storage",))
        template = root / "fixtures/rust-consumer/Cargo.toml.in"
        with template.open("a", encoding="utf-8") as content:
            for name, pin in dependencies.items():
                content.write(f'{name} = {{ git = "{pin["git"]}", rev = "{pin["rev"]}" }}\n')
        self.commit(root)
        revision, _ = driver._source_revision(root, None)
        identities = {
            name: driver._git_identity(pin["git"], pin["rev"]) for name, pin in dependencies.items()
        }
        identities["configuration-storage"] = driver._git_identity(GIT_URL, revision)
        manifest = self.work / "manifest.json"
        manifest.write_text(json.dumps(origins), encoding="utf-8")
        calls = []

        def cargo(command, *, cwd, env):
            calls.append(command[1])
            marker = Path(env["CARGO_HOME"]) / "Cargo.toml"
            if command[1] == "generate-lockfile":
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
                config = Path(env["GIT_CONFIG_GLOBAL"]).read_text(encoding="utf-8")
                self.assertEqual(config.count("insteadOf = "), 3)
                for url, commit in [
                    (GIT_URL, revision),
                    *((row["url"], row["revision"]) for row in origins),
                ]:
                    target = marker.parent / commit
                    driver._git(cwd, "clone", url, target, env=env)
                    self.assertEqual(driver._git(target, "rev-parse", "HEAD", env=env), commit)
                (cwd / "Cargo.lock").write_text(
                    "".join(
                        f'[[package]]\nname = "{name}"\nsource = "{source}"\n'
                        for name, source in identities.items()
                    ),
                    encoding="utf-8",
                )
            if command[1] == "metadata":
                marker.touch()
                return SimpleNamespace(
                    stdout=json.dumps(
                        {
                            "packages": [
                                {"name": name, "source": source, "manifest_path": str(marker)}
                                for name, source in identities.items()
                            ],
                        }
                    )
                )
            return SimpleNamespace(stdout="")

        original = driver._run_rust
        explicit = [
            "--git-dependency-source",
            f"{second_row['repository']}={second}",
            "--git-dependency-source",
            f"{first_row['repository']}={first}",
        ]
        with (
            patch.object(
                driver,
                "_run_rust",
                side_effect=lambda *args, **kw: original(*args, runner=cargo, **kw),
            ),
            patch.object(driver.shutil, "which", return_value="prepared-cargo"),
        ):
            for uses_ci in (True, False):
                evidence = self.work / f"multiple-{uses_ci}.json"
                with (
                    self.subTest(uses_ci=uses_ci),
                    patch.dict(os.environ, {"NAVI_CI_SOURCES": str(manifest) if uses_ci else ""}),
                ):
                    self.cli(
                        root,
                        [
                            "--language",
                            "rust",
                            "--evidence",
                            str(evidence),
                            *([] if uses_ci else explicit),
                        ],
                        rust_git_dependencies=dependencies,
                    )
                    self.assertEqual(calls, ["generate-lockfile", "fetch", "metadata", "run"])
                    calls.clear()
                    summary = json.loads(evidence.read_text(encoding="utf-8"))
                    self.assertEqual(summary["state"], "passed")
                    self.assertEqual(
                        summary["rust"]["gitDependencies"],
                        {name: identities[name] for name in dependencies},
                    )

            for change, error in (
                ({"sha256": "a" * 64}, "bundle checksum differs"),
                ({"revision": "a" * 40}, "bundle identity"),
            ):
                manifest.write_text(json.dumps([first_row, second_row | change]), encoding="utf-8")
                with (
                    self.subTest(change=change),
                    patch.dict(os.environ, {"NAVI_CI_SOURCES": str(manifest)}),
                    self.assertRaisesRegex(ValueError, error),
                ):
                    self.cli(root, ["--language", "rust"], rust_git_dependencies=dependencies)
                self.assertEqual(calls, [])

            (second / "producer").write_text("newer than the advertised pin", encoding="utf-8")
            self.commit(second)
            unrelated_head = self.work / "unrelated-head.bundle"
            driver._git(second, "bundle", "create", unrelated_head, "HEAD")
            content = unrelated_head.read_bytes()
            (self.work / second_row["bundle"]).write_bytes(content)
            manifest.write_text(
                json.dumps(
                    [first_row, second_row | {"sha256": hashlib.sha256(content).hexdigest()}]
                ),
                encoding="utf-8",
            )
            with (
                patch.dict(os.environ, {"NAVI_CI_SOURCES": str(manifest)}),
                self.assertRaisesRegex(ValueError, "bundle HEAD differs"),
            ):
                self.cli(root, ["--language", "rust"], rust_git_dependencies=dependencies)
            self.assertEqual(calls, [])

            with (
                patch.dict(os.environ, {"NAVI_CI_SOURCES": ""}),
                self.assertRaisesRegex(ValueError, "requires a distinct delivery"),
            ):
                self.cli(
                    root,
                    [
                        "--language",
                        "rust",
                        *explicit[:1],
                        f"{second_row['repository']}={first}",
                        *explicit[2:],
                    ],
                    rust_git_dependencies=dependencies,
                )
            self.assertEqual(calls, [])

    def test_multiple_repository_paths_require_exact_mappings_and_keep_the_legacy_single_path(self):
        a, b = "naviapps/first", "naviapps/second"
        pins = dict.fromkeys((a, b))
        path = "file with an=equals sign"
        self.assertEqual(driver._local_dependency_sources([path], {a: None}), {a: Path(path)})
        self.assertEqual(
            driver._local_dependency_sources([f"{a}={path}"], {a: None}), {a: Path(path)}
        )
        for values in (
            [path],
            [f"{a}={path}"],
            [f"{a}=one", f"{a}=two"],
            [f"{a}=one", "naviapps/other=two"],
            [f"{a}=one", f"{b}="],
        ):
            with (
                self.subTest(values=values),
                self.assertRaisesRegex(ValueError, "each SDK Git source once"),
            ):
                driver._local_dependency_sources(values, pins)

    def test_conflicting_revisions_or_aliases_of_one_repository_are_rejected(self):
        first = {"git": DEPENDENCY_URL, "rev": "a" * 40}
        for conflict in (
            {"rev": "b" * 40},
            {"git": DEPENDENCY_URL.replace("naviapps", "NaviApps")},
        ):
            with (
                self.subTest(conflict=conflict),
                self.assertRaisesRegex(ValueError, "one exact URL and revision"),
            ):
                driver._extra_dependencies(
                    {"first": first, "second": first | conflict}, GIT_URL, ()
                )

    def test_shallow_source_cannot_replace_complete_delivery(self):
        root = self.repository()
        rev, tree = driver._source_revision(root, None)
        shallow = self.work / "shallow"
        driver._git(self.work, "clone", "--depth=1", root.as_uri(), str(shallow))
        task = self.directory("task")
        with self.assertRaisesRegex(ValueError, "complete local mirror or bundle"):
            driver._prepare_git_delivery(
                shallow, task, driver._environment(task, rust=True), GIT_URL, rev, tree
            )
        self.assertFalse((task / "sdk.git").exists())

    def test_manifest_requires_exact_git_dependencies_for_both_fixture_shapes(self):
        for packages in (
            ("configuration-storage",),
            ("preset-documents", "preset-documents-file-io"),
        ):
            with self.subTest(packages=packages):
                revision = "a" * 40
                template = self.manifest(packages)
                self.assertNotIn(
                    "@SDK_REVISION@", driver._rust_manifest(template, GIT_URL, packages, revision)
                )
                with self.assertRaisesRegex(ValueError, "exact public Git"):
                    driver._rust_manifest(
                        template.replace(f'git = "{GIT_URL}"', 'path = "../sdk"'),
                        GIT_URL,
                        packages,
                        revision,
                    )

    def test_registry_dependency_requires_an_exact_manifest_version_and_features(self):
        packages = ("producer",)
        revision = "a" * 40
        registry = driver._registry_dependencies(
            {"tokio": {"version": "=1.54.1", "features": ["macros", "rt"]}}, packages
        )
        template = (
            self.manifest(packages)
            + 'tokio = { version = "=1.54.1", features = ["macros", "rt"] }\n'
        )
        options = {"registry_dependencies": registry}
        self.assertIn(
            'version = "=1.54.1"',
            driver._rust_manifest(template, GIT_URL, packages, revision, **options),
        )
        for changed in (
            template.replace('"=1.54.1"', '"1.54.1"'),
            template.replace('"rt"', '"full"'),
        ):
            with (
                self.subTest(changed=changed),
                self.assertRaisesRegex(ValueError, "exact public Git"),
            ):
                driver._rust_manifest(changed, GIT_URL, packages, revision, **options)

    def test_rust_metadata_rejects_a_manifest_outside_the_client_and_fresh_cache(self):
        client, cache = self.directory("client"), self.directory("cargo-home")
        package = "configuration-storage"
        expected = f"git+{GIT_URL}?rev={'a' * 40}#{'a' * 40}"
        manifest = cache / "Cargo.toml"
        manifest.write_text("resolved", encoding="utf-8")
        entry = {"name": package, "source": expected, "manifest_path": str(manifest)}
        metadata = {"packages": [entry]}
        outside = self.work / "developer-Cargo.toml"
        outside.write_text("developer", encoding="utf-8")
        entry["manifest_path"] = str(outside)
        with self.assertRaisesRegex(ValueError, "escaped"):
            driver._check_rust_metadata(
                metadata, client, cache, type(self).verifier, (package,), expected
            )

    def test_identity_verifier_rejects_ambiguous_framing_before_launching(self):
        with (
            patch.object(driver.subprocess, "run") as launch,
            self.assertRaisesRegex(ValueError, "Invalid Cargo package evidence record"),
        ):
            driver._verify_packages(
                type(self).verifier, [{"name": "bad\0record"}], ("producer",), GIT_URL, "Cargo.lock"
            )
        launch.assert_not_called()

    def test_identity_verifier_reports_transport_failures_without_process_output(self):
        for result, exception, message in (
            (None, subprocess.TimeoutExpired("fixture", 120), "Cannot run"),
            (None, OSError("private process error"), "Cannot run"),
            (
                subprocess.CompletedProcess([], 1, b"", b"private process error"),
                None,
                "Unexpected response",
            ),
            (
                subprocess.CompletedProcess([], 0, b"private process output", b""),
                None,
                "Unexpected response",
            ),
        ):
            with (
                self.subTest(message=message, result=result),
                patch.object(driver.subprocess, "run", return_value=result, side_effect=exception),
                self.assertRaisesRegex(ValueError, message) as failure,
            ):
                driver._verify_packages(
                    type(self).verifier, [{"name": "producer"}], (), GIT_URL, "Cargo.lock"
                )
            self.assertNotIn("private", str(failure.exception))

    def test_fixture_selection_rejects_empty_or_invalid_transport_output(self):
        for output in (b"", b"bad\0field", b"\xff"):
            with (
                self.subTest(output=output),
                patch.object(
                    driver.subprocess,
                    "run",
                    return_value=subprocess.CompletedProcess([], 0, output, b""),
                ),
                self.assertRaisesRegex(ValueError, "Unexpected response"),
            ):
                type(self).verifier("select-binary", ["", "fixture"], selects=True)

    def test_rust_modes_reuse_one_git_install_and_keep_selected_metadata(self):
        root = self.repository()
        packages = ("recognition", "rendering")
        self.rust_fixture(root, packages)
        template = root / "fixtures/rust-consumer/Cargo.toml.in"
        template.write_text(
            template.read_text(encoding="utf-8").replace(
                f'rendering = {{ git = "{GIT_URL}", rev = "@SDK_REVISION@" }}',
                f'rendering = {{ git = "{GIT_URL}", rev = "@SDK_REVISION@", optional = true }}',
            )
            + '\n[features]\ngeometry = ["dep:rendering"]\nraster = ["geometry"]\n',
            encoding="utf-8",
        )
        self.commit(root)
        revision, _ = driver._source_revision(root, None)
        identity = driver._git_identity(GIT_URL, revision)
        lock = "".join(
            f'[[package]]\nname = "{name}"\nsource = "{identity}"\n' for name in packages
        ).encode()
        modes = {
            "recognition": ((), packages[:1]),
            "geometry": (("geometry",), packages),
            "raster": (("raster",), packages),
        }
        report = self.work / "features.json"
        actions, caches, inspected = [], set(), []
        original = driver._run_rust

        def fake_cargo(command, *, cwd, env):
            action = command[1]
            actions.append(action)
            if action == "-vV":
                self.assertNotIn("CARGO_HOME", env)
                return SimpleNamespace(stdout="release: 1.99.1\nhost: aarch64-apple-darwin\n")
            cache = Path(env["CARGO_HOME"])
            caches.add(cache)
            if action == "generate-lockfile":
                self.assertFalse((cwd / "Cargo.lock").exists())
                (cwd / "Cargo.lock").write_bytes(lock)
            if action in {"metadata", "run"}:
                self.assertIn("--locked", command)
                self.assertIn("--offline", command)
                mode = (
                    command[command.index("--features") + 1]
                    if "--features" in command
                    else "recognition"
                )
                if action == "run":
                    self.assertEqual(Path(command[-1]).name, mode)
                    self.assertTrue(report.with_suffix(f".{mode}.cargo-metadata.json").exists())
                    return SimpleNamespace(stdout=mode + " passed\n")
                self.assertIn("--filter-platform", command)
                entries = []
                for name in modes[mode][1]:
                    manifest = cache / name / "Cargo.toml"
                    manifest.parent.mkdir(exist_ok=True)
                    manifest.touch()
                    entries.append(
                        {"name": name, "source": identity, "manifest_path": str(manifest)}
                    )
                return SimpleNamespace(stdout=json.dumps({"packages": entries}))
            return SimpleNamespace(stdout="")

        def rust(*args, **kwargs):
            return original(*args, runner=fake_cargo, **kwargs)

        def check(_metadata, mode):
            inspected.append(mode)
            return {"mode": mode}

        with (
            patch.object(driver, "_run_rust", side_effect=rust),
            patch.object(driver.shutil, "which", return_value="prepared-cargo"),
        ):
            driver.check_installed(
                root=root,
                git_url=GIT_URL,
                rust_packages=packages,
                rust_modes=modes,
                rust_mode_check=check,
                argv=["--evidence", str(report)],
            )
        self.assertEqual(actions.count("generate-lockfile"), 1)
        self.assertEqual(actions.count("fetch"), 1)
        self.assertEqual(inspected, list(modes))
        self.assertEqual(len(caches), 1)
        self.assertTrue(not any(cache.exists() for cache in caches))
        evidence = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(evidence["state"], "passed")
        self.assertEqual(report.with_suffix(".Cargo.lock").read_bytes(), lock)
        for mode in modes:
            result = evidence["rust"]["modes"][mode]
            self.assertEqual(result["state"], "passed")
            self.assertEqual(result["checks"], {"mode": mode})
            self.assertEqual(result["runtimeOutput"], mode + " passed\n")

        report = self.work / "rejected-feature.json"
        actions.clear()

        def reject_geometry(metadata, mode):
            if mode == "geometry":
                raise ValueError("A default raster dependency leaked into geometry")
            return check(metadata, mode)

        with (
            patch.object(driver, "_run_rust", side_effect=rust),
            patch.object(driver.shutil, "which", return_value="prepared-cargo"),
            self.assertRaisesRegex(ValueError, "leaked into geometry"),
        ):
            driver.check_installed(
                root=root,
                git_url=GIT_URL,
                rust_packages=packages,
                rust_modes=modes,
                rust_mode_check=reject_geometry,
                argv=["--evidence", str(report)],
            )
        self.assertEqual(actions.count("run"), 1)
        self.assertTrue(report.with_suffix(".geometry.cargo-metadata.json").is_file())
        self.assertFalse(report.with_suffix(".raster.cargo-metadata.json").exists())
        failure = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(failure["state"], "failed")
        self.assertEqual(failure["rust"]["modes"]["recognition"]["state"], "passed")
        self.assertEqual(
            failure["rust"]["modes"]["recognition"]["runtimeOutput"], "recognition passed\n"
        )
        self.assertEqual(failure["rust"]["modes"]["geometry"]["state"], "resolved")
        self.assertNotIn("runtimeOutput", failure["rust"]["modes"]["geometry"])
        self.assertNotIn("raster", failure["rust"]["modes"])

    def test_rust_uses_fresh_cache_and_keeps_own_resolution_when_run_fails(self):
        root = self.repository()
        packages = ("preset-documents", "preset-documents-file-io")
        self.rust_fixture(root, packages)
        rev, tree = driver._source_revision(root, None)
        identity = f"git+{GIT_URL}?rev={rev}#{rev}"
        lock = "".join(
            f'[[package]]\nname = "{name}"\nsource = "{identity}"\n' for name in packages
        ).encode()
        report = self.work / "evidence.json"
        events = []
        work_dirs = []
        original = driver._run_rust

        def fake_cargo(command, *, cwd, env):
            action = command[1]
            events.append(action)
            if action == "generate-lockfile":
                self.prepare_verifier.assert_called_once()
                self.assertNotIn("CARGO_HOME", self.prepare_verifier.call_args.args[2])
                self.assertNotIn("CARGO_TARGET_DIR", self.prepare_verifier.call_args.args[2])
                self.assertNotIn("RUSTUP_AUTO_INSTALL", self.prepare_verifier.call_args.args[2])
                self.assertNotIn("RUSTUP_AUTO_INSTALL", env)
                work_dirs.append(cwd.parent.parent)
                self.assertFalse((cwd / "Cargo.lock").exists())
                self.assertEqual(list(Path(env["CARGO_HOME"]).iterdir()), [])
                self.assertEqual(Path(env["CARGO_TARGET_DIR"]), cwd.parent / "target")
                self.assertEqual(env["RUSTUP_TOOLCHAIN"], "1.99.1")
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
                for key in ("RUSTFLAGS", "GITHUB_TOKEN", "NAVI_CI_SOURCES"):
                    self.assertNotIn(key, env)
                (cwd / "Cargo.lock").write_bytes(lock)
            if action == "metadata":
                selected = []
                for name in packages:
                    manifest = Path(env["CARGO_HOME"]) / "git/checkouts/sdk" / name / "Cargo.toml"
                    manifest.parent.mkdir(parents=True)
                    manifest.write_text("dependency", encoding="utf-8")
                    selected.append(
                        {"name": name, "source": identity, "manifest_path": str(manifest)}
                    )
                return SimpleNamespace(stdout=json.dumps({"packages": selected}))
            if action == "run":
                self.assertEqual(command[2:5], ["--locked", "--offline", "--"])
                self.assertEqual(list(Path(command[-1]).iterdir()), [])
                self.assertEqual(report.with_suffix(".Cargo.lock").read_bytes(), lock)
                self.assertTrue(report.with_suffix(".cargo-metadata.json").exists())
                raise subprocess.CalledProcessError(1, command)
            return SimpleNamespace(stdout="")

        def rust(*args):
            return original(*args, runner=fake_cargo)

        with (
            patch.object(driver, "_run_rust", side_effect=rust),
            patch.object(driver.shutil, "which", return_value="prepared-cargo"),
            patch.dict(os.environ, {"CARGO_HOME": "producer", "RUSTFLAGS": "producer"}),
            self.assertRaises(subprocess.CalledProcessError),
        ):
            self.cli(root, ["--language", "rust", "--evidence", str(report)], packages)
        evidence = json.loads(report.read_text(encoding="utf-8"))
        self.assertEqual(evidence["sdkTree"], tree)
        self.assertEqual(evidence["state"], "failed")
        self.assertEqual(evidence["rust"]["gitIdentity"], identity)
        self.assertEqual(evidence["rust"]["cargoLockSHA256"], hashlib.sha256(lock).hexdigest())
        self.assertEqual(events, ["generate-lockfile", "fetch", "metadata", "run"])
        self.assertTrue(work_dirs and not any(path.exists() for path in work_dirs))

    @unittest.skipIf(os.name == "nt", "Requires POSIX symlinks")
    def test_evidence_refuses_report_and_selected_rust_sidecar_symlinks_before_work(self):
        root = self.repository()
        sentinel = root / "producer"
        original = sentinel.read_bytes()
        revision = driver._source_revision(root, None)
        for index, suffix in enumerate(
            (None, ".Cargo.lock", ".cargo-metadata.json", ".geometry.cargo-metadata.json")
        ):
            report = self.work / f"evidence-{index}.json"
            output = report.with_suffix(suffix) if suffix else report
            output.symlink_to(sentinel)
            modes = (
                {
                    "rust_modes": {"geometry": (("geometry",), ("configuration-storage",))},
                    "rust_mode_check": lambda *_: {},
                }
                if suffix == ".geometry.cargo-metadata.json"
                else {}
            )
            with (
                self.subTest(suffix=suffix),
                patch.object(driver, "_run_rust") as rust,
                self.assertRaisesRegex(ValueError, "evidence output already exists"),
            ):
                self.cli(root, ["--language", "rust", "--evidence", str(report)], **modes)
            rust.assert_not_called()
            self.assertTrue(output.is_symlink())
            self.assertEqual(sentinel.read_bytes(), original)
        self.assertEqual(driver._source_revision(root, None), revision)

    @unittest.skipIf(os.name == "nt", "Requires POSIX symlinks")
    def test_new_sidecar_symlink_cannot_overwrite_producer_and_partial_evidence_survives(self):
        root = self.repository()
        sentinel = root / "producer"
        original = sentinel.read_bytes()
        revision = driver._source_revision(root, None)
        report = self.work / "evidence.json"
        lock = b"# resolved lock\n"

        def run_rust(root, work, git_url, packages, revision, tree, delivery, resolved):
            report.with_suffix(".cargo-metadata.json").symlink_to(sentinel)
            resolved(tree, driver._git_identity(git_url, revision), lock, {"packages": []})
            self.fail("A newly occupied sidecar must stop acceptance")

        with patch.object(driver, "_run_rust", run_rust), self.assertRaises(FileExistsError):
            self.cli(root, ["--language", "rust", "--evidence", str(report)])
        self.assertEqual(sentinel.read_bytes(), original)
        self.assertEqual(driver._source_revision(root, None), revision)
        self.assertEqual(report.with_suffix(".Cargo.lock").read_bytes(), lock)
        self.assertEqual(json.loads(report.read_bytes())["state"], "failed")

    def test_git_replace_cannot_relabel_a_different_producer_tree(self):
        root = self.repository()
        revision, tree = driver._source_revision(root, None)
        (root / "producer").write_text("replacement", encoding="utf-8")
        self.commit(root)
        replacement = driver._git(root, "rev-parse", "HEAD")
        driver._git(root, "checkout", "--quiet", "--detach", revision)
        driver._git(root, "replace", revision, replacement)
        self.assertEqual(driver._source_revision(root, revision), (revision, tree))
        env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        subprocess.run(["git", "reset", "--quiet", "--hard", "HEAD"], cwd=root, env=env, check=True)
        with self.assertRaisesRegex(ValueError, "clean committed SDK"):
            driver._source_revision(root, revision)

    def test_dirty_producer_or_wrong_revision_stops_before_delivery(self):
        root = self.repository()
        with self.assertRaisesRegex(ValueError, "source HEAD"):
            self.cli(root, ["--language", "rust", "--revision", "a" * 40])
        (root / "untracked").write_text("dirty", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "clean committed SDK"):
            self.cli(root, ["--language", "rust"])

    def c_sdk(self, root=None, *, minimum_macos="26.0"):
        root = root or self.directory("c-sdk")
        framework = root / FRAMEWORK
        for name, value in {
            "CFixture": b"accepted native bytes",
            "Headers/fixture.h": b"public header",
            "Resources/Info.plist": plistlib.dumps({"MinimumOSVersion": minimum_macos}),
            "Resources/ThirdPartyNotices.txt": b"original upstream notice",
        }.items():
            path = framework / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(value)
        notice = root / "ThirdParty/upstream/NOTICE"
        notice.parent.mkdir(parents=True)
        notice.write_bytes(b"original upstream notice")
        fixture = root / "fixtures/c-consumer/main.c"
        fixture.parent.mkdir(parents=True)
        fixture.write_text("installed client", encoding="utf-8")
        self.receipt(root, minimum_macos=minimum_macos)
        return root

    @staticmethod
    def receipt(root, *, minimum_macos="26.0"):
        receipt = {
            "configuration": {"minimumMacOS": minimum_macos},
            "inputs": hashes(files(root / "ThirdParty"), root),
            "outputs": hashes(files(root / FRAMEWORK), root),
        }
        (root / "Artifacts/native-build.json").write_text(json.dumps(receipt), encoding="utf-8")

    @staticmethod
    def depfile(client, *headers):
        targets = [
            header.as_posix().replace(" ", "\\ ").replace("#", "\\#").replace("$", "$$")
            for header in headers
        ]
        (client / "headers.d").write_text(
            "sdk-consumer: main.c \\\n  " + " \\\n  ".join(targets) + "\n", encoding="utf-8"
        )

    def test_c_compiles_with_only_installed_header_and_runs_in_owned_directory(self):
        root = self.c_sdk()
        task = self.directory("consumer's work #1 $HOME")
        calls = []

        def fake_clang(command, *, cwd, env):
            calls.append(command)
            self.assertEqual(cwd, task / "client")
            self.assertEqual(env["PATH"], "/usr/bin:/bin:/usr/sbin:/sbin")
            for key in ("GITHUB_TOKEN", "DYLD_LIBRARY_PATH", "CPATH", "RUSTFLAGS", "CARGO_HOME"):
                self.assertNotIn(key, env)
            if "clang" in command:
                self.depfile(cwd, task / "install" / FRAMEWORK.name / "Headers" / HEADER)
                self.assertEqual(command[command.index("-MT") + 1], "sdk-consumer")
                self.assertNotIn("-I", command)
                self.assertEqual(command[command.index("-F") + 1], str(task / "install"))
                self.assertFalse(any(str(root) in argument for argument in command))
            else:
                self.assertEqual(list(Path(command[1]).iterdir()), [])
            return SimpleNamespace(stdout="fixture passed")

        with patch.dict(
            os.environ, {"GITHUB_TOKEN": "fixture", "CPATH": str(root), "RUSTFLAGS": "fixture"}
        ):
            evidence = driver._run_c(root, task, FRAMEWORK, HEADER, None, fake_clang)
        self.assertEqual(len(calls), 2)
        self.assertIn("ThirdParty/upstream/NOTICE", evidence["installedFiles"])
        self.assertEqual(evidence["runtimeOutput"], "fixture passed")

    def test_c_uses_the_selected_tracked_source_and_receipt(self):
        root = self.c_sdk(self.repository(), minimum_macos="27.1")
        fixture = "fixtures/c-consumer/product.c"
        receipt = "Artifacts/CFixture.build.json"
        (root / "fixtures/c-consumer/main.c").rename(root / fixture)
        (root / "Artifacts/native-build.json").rename(root / receipt)
        self.commit(root)
        report = self.work / "product.json"
        unused_lock = report.with_suffix(".Cargo.lock")
        unused_lock.write_bytes(b"unrelated Rust evidence\n")
        actual_run_c = driver._run_c

        def fake_clang(command, *, cwd, env):
            if "clang" in command:
                self.assertEqual((cwd / "main.c").read_bytes(), (root / fixture).read_bytes())
                self.assertIn("-mmacosx-version-min=27.1", command)
                framework = cwd.parent / "install" / FRAMEWORK.name
                self.depfile(cwd, framework / "Headers" / HEADER)
            return SimpleNamespace(stdout="selected fixture passed\n")

        def run_c(*args, **kwargs):
            return actual_run_c(*args, runner=fake_clang, **kwargs)

        with patch.object(driver.sys, "platform", "darwin"), patch.object(driver, "_run_c", run_c):
            self.cli(
                root,
                ["--language", "c", "--evidence", str(report)],
                c_fixture=fixture,
                c_receipt=receipt,
            )
        evidence = json.loads(report.read_bytes())
        self.assertEqual(evidence["state"], "passed")
        self.assertEqual(evidence["c"]["runtimeOutput"], "selected fixture passed\n")
        self.assertEqual(unused_lock.read_bytes(), b"unrelated Rust evidence\n")

    def test_missing_default_c_fixture_retains_rust_evidence(self):
        root = self.c_sdk(self.repository())
        (root / "fixtures/c-consumer/main.c").unlink()
        self.commit(root)
        report = self.work / "both.json"
        lock = b"# isolated client lock\n"

        def run_rust(root, work, git_url, packages, revision, tree, delivery, resolved):
            resolved(tree, driver._git_identity(git_url, revision), lock, {"packages": []})
            return "Rust fixture passed\n"

        with (
            patch.object(driver.sys, "platform", "darwin"),
            patch.object(driver, "_run_rust", run_rust),
            self.assertRaises(FileNotFoundError),
        ):
            self.cli(root, ["--evidence", str(report)])
        evidence = json.loads(report.read_bytes())
        self.assertEqual(evidence["state"], "failed")
        self.assertEqual(evidence["rust"]["runtimeOutput"], "Rust fixture passed\n")
        self.assertEqual(report.with_suffix(".Cargo.lock").read_bytes(), lock)

    def test_c_rejects_ignored_files_and_a_receipt_outside_artifacts(self):
        root = self.c_sdk(self.repository())
        (root / ".gitignore").write_text("ignored.c\nignored.json\n", encoding="utf-8")
        for name in ("ignored.c", "Artifacts/ignored.json", "receipt.json"):
            (root / name).write_text("fixture", encoding="utf-8")
        self.commit(root)
        for index, (arguments, message) in enumerate(
            (
                ({"c_fixture": "ignored.c"}, "must be tracked"),
                ({"c_receipt": "Artifacts/ignored.json"}, "must be tracked"),
                ({"c_receipt": "receipt.json"}, "JSON receipt under Artifacts"),
            )
        ):
            report = self.work / f"invalid-product-{index}.json"
            with (
                self.subTest(arguments=arguments),
                patch.object(driver.sys, "platform", "darwin"),
                self.assertRaisesRegex(ValueError, message),
            ):
                self.cli(root, ["--language", "c", "--evidence", str(report)], **arguments)
            evidence = json.loads(report.read_bytes())
            self.assertEqual(evidence["state"], "failed")
            self.assertIn(message, evidence["failure"])

    @unittest.skipIf(os.name == "nt", "Requires POSIX symlinks")
    def test_c_rejects_selected_symlinks_outside_their_declared_roots(self):
        root = self.c_sdk(self.repository())
        (self.work / "outside.c").write_text("outside", encoding="utf-8")
        (root / "escape.c").symlink_to("../outside.c")
        (root / "receipt.json").write_text("outside Artifacts", encoding="utf-8")
        (root / "Artifacts/escape.json").symlink_to("../receipt.json")
        self.commit(root)
        for arguments in (
            {"c_fixture": "escape.c"},
            {"c_receipt": "Artifacts/escape.json"},
        ):
            with (
                self.subTest(arguments=arguments),
                patch.object(driver.sys, "platform", "darwin"),
                self.assertRaisesRegex(ValueError, "inside its declared root"),
            ):
                self.cli(root, ["--language", "c"], **arguments)

    def test_c_rejects_a_producer_header_before_runtime(self):
        root = self.c_sdk()
        task = self.directory("c-work")

        def fake_clang(command, *, cwd, env):
            self.assertIn("clang", command)
            self.depfile(
                cwd,
                task / "install" / FRAMEWORK.name / "Headers" / HEADER,
                root / FRAMEWORK / "Headers" / HEADER,
            )
            return SimpleNamespace(stdout="")

        with self.assertRaisesRegex(ValueError, "installed SDK header"):
            driver._run_c(root, task, FRAMEWORK, HEADER, None, fake_clang)

    def test_c_rejects_changes_to_installed_payload_during_execution(self):
        root = self.c_sdk()
        task = self.directory("c-work")
        framework = task / "install" / FRAMEWORK.name

        def fake_clang(command, *, cwd, env):
            if "clang" in command:
                self.depfile(cwd, framework / "Headers" / HEADER)
            else:
                (framework / "CFixture").write_bytes(b"changed at runtime")
            return SimpleNamespace(stdout="fixture passed")

        with self.assertRaisesRegex(ValueError, "installed C payload changed"):
            driver._run_c(root, task, FRAMEWORK, HEADER, None, fake_clang)

    def test_c_rejects_stale_or_unrecorded_framework_bytes(self):
        root = self.c_sdk()
        binary = root / FRAMEWORK / "CFixture"
        binary.write_bytes(b"changed binary")
        with self.assertRaisesRegex(ValueError, "receipt"):
            driver._stage_c(root, self.directory("stale-work"), FRAMEWORK)
        binary.write_bytes(b"accepted native bytes")
        (root / FRAMEWORK / "unrecorded").write_text("unrecorded", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "accepted output inventory"):
            driver._stage_c(root, self.directory("extra-work"), FRAMEWORK)

    def test_c_receipt_requires_the_frameworks_minimum_macos(self):
        root = self.c_sdk()
        for name, version, error in (
            ("invalid", "26.0 extra", "must declare a minimum macOS"),
            ("different", "27.1", "different minimum macOS"),
        ):
            with self.subTest(version=version), self.assertRaisesRegex(ValueError, error):
                self.receipt(root, minimum_macos=version)
                driver._stage_c(root, self.directory(name), FRAMEWORK)

    @unittest.skipIf(os.name == "nt", "Requires POSIX framework symlinks")
    def test_c_keeps_relative_framework_links_but_rejects_escape(self):
        root = self.c_sdk()
        framework = root / FRAMEWORK
        version = framework / "Versions/A"
        version.mkdir(parents=True)
        (framework / "Headers").rename(version / "Headers")
        (framework / "Versions/Current").symlink_to("A", target_is_directory=True)
        (framework / "Headers").symlink_to("Versions/Current/Headers", target_is_directory=True)
        self.receipt(root)
        installed, _, _ = driver._stage_c(root, self.directory("linked"), FRAMEWORK)
        self.assertEqual(
            os.readlink(installed / FRAMEWORK.name / "Headers"), "Versions/Current/Headers"
        )
        (framework / "escape").symlink_to(root / "ThirdParty/upstream/NOTICE")
        self.receipt(root)
        with self.assertRaisesRegex(ValueError, "symlink escapes"):
            driver._stage_c(root, self.directory("escaped"), FRAMEWORK)

    def test_default_gate_does_not_invoke_mac_framework_on_windows(self):
        root = self.repository()
        with (
            patch.object(driver.sys, "platform", "win32"),
            self.assertRaisesRegex(ValueError, "requires owned macOS"),
        ):
            self.cli(root, [])

    def test_windows_runs_the_same_fixture_for_native_and_emulated_targets(self):
        root = self.repository()
        packages = ("configuration-storage",)
        self.rust_fixture(root, packages)
        revision, _ = driver._source_revision(root, None)
        identity = driver._git_identity(GIT_URL, revision)
        original = driver._run_rust

        def msvc(architecture, *, environment, target_architecture):
            self.assertEqual((architecture, target_architecture), (host, target_arch))
            for variable in ("GIT_CONFIG_COUNT", "RUSTFLAGS", "CARGO_HOME"):
                self.assertNotIn(variable, environment)
            self.assertEqual(environment["USERPROFILE"], "pinned-toolchains")
            self.assertEqual(environment["TEMP"], environment["TMPDIR"])
            return dict(environment, LIB="prepared-msvc", INCLUDE="prepared-sdk"), None

        def rust(*args, **kwargs):
            def cargo(command, *, cwd, env):
                self.assertEqual(env["RUSTUP_TOOLCHAIN"], "1.99.1")
                self.assertEqual(env["RUSTUP_AUTO_INSTALL"], "0")
                self.assertEqual(env["LIB"], "prepared-msvc")
                self.assertEqual(env["INCLUDE"], "prepared-sdk")
                self.assertEqual(env["GIT_ALLOW_PROTOCOL"], "file")
                if Path(command[0]).suffix == ".exe":
                    actions.append("execute")
                    self.assertEqual(Path(command[0]).name, "selected.exe")
                    self.assertEqual(list(Path(command[1]).iterdir()), [])
                    return SimpleNamespace(stdout="Installed fixture passed\n")
                action = command[1]
                actions.append(action)
                if action == "-vV":
                    triple = driver.windows.MSVC_TARGETS[host]
                    return SimpleNamespace(stdout=f"release: 1.99.1\nhost: {triple}\n")
                if action == "--print":
                    self.assertEqual(command[2:], ["target-libdir", "--target", target])
                    return SimpleNamespace(stdout=str(stdlib))
                if action == "generate-lockfile":
                    self.assertEqual(
                        (cwd / "src/main.rs").read_bytes(),
                        (root / "fixtures/rust-consumer/src/main.rs").read_bytes(),
                    )
                    (cwd / "Cargo.lock").write_text(
                        f'[[package]]\nname = "{packages[0]}"\nsource = "{identity}"\n'
                    )
                if action == "metadata":
                    self.assertEqual(command[-2:], ["--filter-platform", target])
                    manifest = Path(env["CARGO_HOME"]) / "Cargo.toml"
                    manifest.touch()
                    return SimpleNamespace(
                        stdout=json.dumps(
                            {
                                "resolve": {"root": "client-package"},
                                "packages": [
                                    {
                                        "id": "client-package",
                                        "name": "outside-client",
                                        "default_run": "selected",
                                        "targets": [
                                            {"name": "other", "kind": ["bin"]},
                                            {"name": "selected", "kind": ["bin"]},
                                        ],
                                        "manifest_path": str(cwd / "Cargo.toml"),
                                    },
                                    {
                                        "name": packages[0],
                                        "source": identity,
                                        "manifest_path": str(manifest),
                                    },
                                ],
                            }
                        )
                    )
                if action in {"fetch", "build"}:
                    self.assertEqual(command[command.index("--target") + 1], target)
                    self.assertIn("--locked", command)
                if action == "build":
                    self.assertIn("--offline", command)
                    self.assertEqual(command[command.index("--bin") + 1], "selected")
                    program = Path(env["CARGO_TARGET_DIR"]) / target / "debug" / "selected.exe"
                    program.parent.mkdir(parents=True)
                    image = bytearray(70)
                    image[:2] = b"MZ"
                    struct.pack_into("<I", image, 60, 64)
                    image[64:68] = b"PE\0\0"
                    struct.pack_into("<H", image, 68, driver.windows.MSVC_MACHINES[image_arch])
                    program.write_bytes(image)
                    return SimpleNamespace(
                        stdout=json.dumps(
                            {
                                "reason": "compiler-artifact",
                                "package_id": "client-package",
                                "target": {"name": "selected", "kind": ["bin"]},
                                "executable": str(program),
                            }
                        )
                    )
                return SimpleNamespace(stdout="")

            return original(*args, runner=cargo, **kwargs)

        stdlib = self.directory("target-library")
        stdlib_file = stdlib / "libstd-fixture.rlib"
        for host, target_arch, image_arch, prepared, failure in (
            ("x64", "x64", "x64", True, None),
            ("arm64", "arm64", "arm64", True, None),
            ("arm64", "x64", "x64", True, None),
            ("arm64", "x64", "arm64", True, "Expected target-native x64 PE image"),
            ("arm64", "x64", "x64", False, "Install the pinned Rust standard library"),
            (
                "x64",
                "arm64",
                "arm64",
                True,
                "Select the native Windows Rust target or x64 on ARM64",
            ),
        ):
            target = driver.windows.MSVC_TARGETS[target_arch]
            report = self.work / f"{host}-{target_arch}-{image_arch}-{prepared}.json"
            actions = []
            if prepared:
                stdlib_file.touch()
            else:
                stdlib_file.unlink()
            with (
                self.subTest(host=host, target=target_arch, image=image_arch, prepared=prepared),
                patch.object(driver.sys, "platform", "win32"),
                patch.object(driver.windows, "host_architecture", return_value=host),
                patch.object(driver.windows, "native_msvc_environment", side_effect=msvc),
                patch.object(driver, "_run_rust", side_effect=rust),
                patch.object(
                    driver.shutil, "which", side_effect=lambda exe, **_: "prepared-" + exe
                ),
                patch.dict(
                    os.environ,
                    {
                        "USERPROFILE": "pinned-toolchains",
                        "RUSTFLAGS": "producer",
                        "GIT_CONFIG_COUNT": "0",
                    },
                ),
            ):
                arguments = ["--windows-architecture", target_arch, "--evidence", str(report)]
                if failure:
                    with self.assertRaisesRegex(ValueError, failure):
                        self.cli(root, arguments)
                else:
                    self.cli(root, arguments)
            evidence = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(evidence["state"], "failed" if failure else "passed")
            if failure:
                self.assertNotIn("execute", actions)
                self.assertNotIn("execution", evidence.get("rust", {}))
                if (host, target_arch) == ("x64", "arm64"):
                    self.assertEqual(actions, [])
                if not prepared:
                    self.assertEqual(actions, ["-vV", "--print"])
                continue
            self.assertEqual(evidence["rust"]["host"], driver.windows.MSVC_TARGETS[host])
            self.assertEqual(evidence["rust"]["target"], target)
            self.assertEqual(
                evidence["rust"]["execution"], "emulated" if host != target_arch else "native"
            )
            self.assertEqual(evidence["rust"]["runtimeOutput"], "Installed fixture passed\n")
            self.assertNotIn("c", evidence)
            self.assertEqual(
                actions,
                ["-vV"]
                + (["--print"] if host != target_arch else [])
                + ["generate-lockfile", "fetch", "metadata", "build", "execute"],
            )

    def test_windows_rejects_wrong_compiler_host_before_resolving_dependencies(self):
        root = self.repository()
        packages = ("configuration-storage",)
        self.rust_fixture(root, packages)
        revision, tree = driver._source_revision(root, None)
        with (
            patch.object(driver.windows, "host_architecture", return_value="arm64"),
            patch.object(
                driver.windows, "native_msvc_environment", return_value=({"PATH": ""}, None)
            ),
            patch.object(driver.shutil, "which", return_value="prepared-cargo"),
            patch.object(driver, "_prepare_git_delivery") as delivery,
            self.assertRaisesRegex(ValueError, "compiler host must match"),
        ):
            driver._run_rust(
                root,
                self.directory("windows"),
                GIT_URL,
                packages,
                revision,
                tree,
                root,
                None,
                runner=lambda *_, **__: SimpleNamespace(
                    stdout="release: 1.99.1\nhost: x86_64-pc-windows-msvc\n"
                ),
                windows_architecture="arm64",
            )
        delivery.assert_not_called()
