use std::{
    fs,
    io::Write,
    path::Path,
    process::{Command, Output, Stdio},
};

use tempfile::TempDir;

const PIN: &str = "tools/tooling-sources.json";
const REVISION: &str = "1111111111111111111111111111111111111111";
const OTHER: &str = "2222222222222222222222222222222222222222";

fn temporary_sdk() -> TempDir {
    let root = tempfile::Builder::new()
        .prefix("SDK checkout ")
        .tempdir()
        .unwrap();
    fs::write(root.path().join("Cargo.toml"), "[workspace]\n").unwrap();
    root
}

fn sync(roots: &[&Path], arguments: &[&str]) -> Output {
    Command::new(env!("CARGO_BIN_EXE_sdk-template-sync"))
        .args(arguments)
        .arg("--")
        .args(roots)
        .output()
        .unwrap()
}

fn succeeds(output: Output) {
    assert!(
        output.status.success(),
        "{}",
        String::from_utf8_lossy(&output.stderr)
    );
}

fn fails(output: Output, error: &str) {
    assert!(!output.status.success());
    let message = String::from_utf8_lossy(&output.stderr);
    assert!(message.contains(error), "{message}");
}

#[test]
fn cli_syncs_and_checks_relative_git_checkouts_from_an_external_directory() {
    let working_directory = tempfile::tempdir().unwrap();
    let roots = ["-sdk checkout", "another SDK"];
    for name in roots {
        let checkout = working_directory.path().join(name);
        fs::create_dir(&checkout).unwrap();
        fs::write(checkout.join("Cargo.toml"), "[workspace]\n").unwrap();
        succeeds(
            Command::new("git")
                .args(["init", "--quiet"])
                .current_dir(checkout)
                .output()
                .unwrap(),
        );
    }

    let invoke = |arguments: &[&str]| {
        Command::new(env!("CARGO_BIN_EXE_sdk-template-sync"))
            .current_dir(working_directory.path())
            .args(arguments)
            .arg("--")
            .args(roots)
            .output()
            .unwrap()
    };
    succeeds(invoke(&[
        "--quality",
        "--swift-format",
        "--revision",
        REVISION,
    ]));

    let templates = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../templates");
    for name in roots {
        let checkout = working_directory.path().join(name);
        for (source, destination) in [
            ("_tooling.py", "tools/_tooling.py"),
            ("quality.py", "tools/quality.py"),
            (".swift-format", ".swift-format"),
        ] {
            assert_eq!(
                fs::read(checkout.join(destination)).unwrap(),
                fs::read(templates.join(source)).unwrap(),
                "{name}/{destination}"
            );
        }
        let expected_pin = format!("{{\n  \"naviapps/sdk-tooling\": \"{REVISION}\"\n}}\n");
        assert_eq!(
            fs::read_to_string(checkout.join(PIN)).unwrap(),
            expected_pin
        );
    }

    let check = |revision| {
        invoke(&[
            "--check",
            "--quality",
            "--swift-format",
            "--revision",
            revision,
        ])
    };
    succeeds(check(REVISION));
    let mismatch = check(OTHER);
    assert_eq!(mismatch.status.code(), Some(1));
    fails(mismatch, "differs from the requested revision");

    let pending = working_directory
        .path()
        .join(roots[0])
        .join("tools/tooling-sources.json.pending");
    fs::write(&pending, "another publisher").unwrap();
    let conflict = invoke(&["--revision", OTHER]);
    assert_eq!(conflict.status.code(), Some(1));
    fails(conflict, "Cannot stage configuration");
    assert_eq!(fs::read_to_string(pending).unwrap(), "another publisher");
    succeeds(check(REVISION));
}

#[test]
fn check_detects_drift_without_repairing_files_or_pins() {
    let root = temporary_sdk();
    let roots = &[root.path()];
    succeeds(sync(roots, &["--revision", REVISION]));
    let pin = root.path().join(PIN);
    let canonical = format!("{{\n  \"naviapps/sdk-tooling\": \"{REVISION}\"\n}}\n");
    assert_eq!(fs::read_to_string(&pin).unwrap(), canonical);

    // The original checker accepts a structured JSON document, including
    // noncanonical whitespace and escaped keys.
    let equivalent = format!("{{ \"naviapps\\/sdk-tooling\" : \"{REVISION}\" }}");
    fs::write(&pin, &equivalent).unwrap();
    succeeds(sync(roots, &["--check"]));
    succeeds(sync(roots, &["--check", "--revision", REVISION]));
    fails(
        sync(roots, &["--check", "--revision", OTHER]),
        "differs from the requested revision",
    );
    assert_eq!(fs::read_to_string(&pin).unwrap(), equivalent);

    let loader = root.path().join("tools/_tooling.py");
    fs::write(&loader, "edited").unwrap();
    fails(sync(roots, &["--check"]), "templates differ");
    assert_eq!(fs::read_to_string(&loader).unwrap(), "edited");
    assert_eq!(fs::read_to_string(&pin).unwrap(), equivalent);
}

fn modern_command(revision: &str) -> String {
    format!("uvx --from git+https://github.com/naviapps/sdk-tooling.git@{revision} sdk-tooling")
}

#[test]
fn modern_sdk_pin_updates_preserve_mise_tasks_and_sdk_owned_policies() {
    for rust in [true, false] {
        let root = temporary_sdk();
        let path = root.path();
        if !rust {
            fs::remove_file(path.join("Cargo.toml")).unwrap();
            fs::write(path.join("Package.swift"), "// swift-tools-version: 6.3\n").unwrap();
            fs::write(path.join(".editorconfig"), "root = true\n").unwrap();
        }
        let command = modern_command(REVISION);
        let mise = format!(
            "# Keep {REVISION} in this comment\r\n[tasks.\"sdk-tooling\"]\r\nrun = '{command}' # review\r\n\r\n[tasks.check]\r\nrun = \"make test\"\r\n"
        );
        fs::write(path.join("mise.toml"), &mise).unwrap();
        for policy in [".gitattributes", ".dprint.json"] {
            fs::write(path.join(policy), "SDK-owned policy\n").unwrap();
        }
        fs::write(path.join("ruff.toml"), "line-length = 90\n").unwrap();

        fails(
            sync(&[path], &["--check", "--revision", OTHER]),
            "differs from the requested revision",
        );
        assert_eq!(fs::read_to_string(path.join("mise.toml")).unwrap(), mise);

        succeeds(sync(&[path], &["--quality", "--revision", OTHER]));
        let expected = mise.replacen(&command, &modern_command(OTHER), 1);
        assert_eq!(
            fs::read_to_string(path.join("mise.toml")).unwrap(),
            expected
        );
        for policy in [".gitattributes", ".dprint.json"] {
            assert_eq!(
                fs::read_to_string(path.join(policy)).unwrap(),
                "SDK-owned policy\n"
            );
        }
        let templates = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../templates");
        assert_eq!(
            fs::read(path.join("ruff.toml")).unwrap(),
            fs::read(templates.join("ruff.toml")).unwrap()
        );
        assert!(!path.join("tools").exists());
        assert!(!path.join("rust-toolchain.toml").exists());
        if !rust {
            assert_eq!(
                fs::read_to_string(path.join(".editorconfig")).unwrap(),
                "root = true\n"
            );
        }
        assert!(!path.join(".clang-format").exists());
        succeeds(sync(
            &[path],
            &["--quality", "--check", "--revision", OTHER],
        ));
    }
}

#[test]
fn malformed_modern_task_blocks_writes_and_never_uses_the_legacy_pin() {
    let first = temporary_sdk();
    let later = temporary_sdk();
    let roots = &[first.path(), later.path()];
    let mise = later.path().join("mise.toml");
    fs::write(
        &mise,
        format!(
            "[tasks.sdk-tooling]\nrun = '{cmd}'\n",
            cmd = modern_command("main")
        ),
    )
    .unwrap();
    fails(
        sync(roots, &["--revision", REVISION]),
        "canonical immutable revision",
    );
    assert!(!first.path().join("tools").exists());
    assert!(!later.path().join("tools").exists());
}

#[test]
fn malformed_or_mutable_pins_fail_without_writes() {
    let root = temporary_sdk();
    let roots = &[root.path()];
    fails(sync(roots, &["--revision", "main"]), "immutable");
    fails(sync(roots, &[]), "immutable");
    assert!(!root.path().join("tools").exists());

    succeeds(sync(roots, &["--revision", REVISION]));
    fails(sync(roots, &["--check", "--revision", "main"]), "immutable");
    let pin = root.path().join(PIN);
    for invalid in [
        "null".into(),
        r#"{"naviapps/sdk-tooling": "main"}"#.into(),
        r#"{"naviapps/sdk-tooling": 123}"#.into(),
        format!(r#"{{"naviapps/sdk-tooling": "{REVISION}", "unrelated": "other"}}"#),
    ] {
        fs::write(&pin, &invalid).unwrap();
        fails(sync(roots, &["--check"]), "canonical and immutable");
        assert_eq!(fs::read_to_string(&pin).unwrap(), invalid);
    }
}

#[test]
fn optional_policies_keep_sdk_choices_and_local_editorconfig_sections() {
    let root = temporary_sdk();
    let roots = &[root.path()];
    let swift_format = root.path().join(".swift-format");
    let editorconfig = root.path().join(".editorconfig");
    let local = b"[bindings/dotnet/src/Library/**.cs]\r\ndotnet_diagnostic.IDE0005.severity = warning\r\n\r\n[fixtures/*.cs]\r\nindent_size = 4";
    fs::write(&swift_format, "SDK-owned choice").unwrap();
    fs::write(
        &editorconfig,
        [b"root = true\r\n\r\n", local.as_slice()].concat(),
    )
    .unwrap();
    succeeds(sync(roots, &["--revision", REVISION]));
    assert_eq!(
        fs::read_to_string(&swift_format).unwrap(),
        "SDK-owned choice"
    );
    assert!(!root.path().join("tools/quality.py").exists());
    succeeds(sync(
        roots,
        &["--quality", "--swift-format", "--revision", REVISION],
    ));
    assert_ne!(
        fs::read_to_string(&swift_format).unwrap(),
        "SDK-owned choice"
    );
    assert!(root.path().join("tools/quality.py").is_file());
    assert!(root.path().join("ruff.toml").is_file());
    let merged = fs::read(&editorconfig).unwrap();
    for section in [
        b"[bindings/dotnet/src/Library/**.cs]\r\ndotnet_diagnostic.IDE0005.severity = warning"
            .as_slice(),
        b"[fixtures/*.cs]\r\nindent_size = 4".as_slice(),
    ] {
        assert!(merged.windows(section.len()).any(|bytes| bytes == section));
    }
    succeeds(sync(roots, &["--check", "--quality", "--swift-format"]));
}

fn git_hash(root: &Path, args: &[&str], data: &[u8]) -> Vec<u8> {
    let mut git = Command::new("git")
        .args(args)
        .current_dir(root)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    git.stdin.take().unwrap().write_all(data).unwrap();
    let output = git.wait_with_output().unwrap();
    assert!(output.status.success());
    output.stdout
}

#[test]
fn synced_attributes_keep_sources_canonical_and_retained_bytes_verbatim() {
    let root = temporary_sdk();
    succeeds(sync(&[root.path()], &["--revision", REVISION]));
    succeeds(
        Command::new("git")
            .args(["init", "--quiet"])
            .current_dir(root.path())
            .output()
            .unwrap(),
    );
    let raw = b"first\r\nsecond\r\n";
    for (path, expected) in [
        ("crates/core/src/lib.rs", b"first\nsecond\n".as_slice()),
        ("ThirdParty/dependency/LICENSE", raw.as_slice()),
        ("Artifacts/runtime.txt", raw.as_slice()),
    ] {
        let actual = git_hash(
            root.path(),
            &[
                "-c",
                "core.autocrlf=true",
                "hash-object",
                "--stdin",
                "--path",
                path,
            ],
            raw,
        );
        let expected = git_hash(
            root.path(),
            &["hash-object", "--stdin", "--no-filters"],
            expected,
        );
        assert_eq!(actual, expected, "{path}");
    }
}

#[test]
fn pin_conflict_preserves_the_previous_pin_and_another_publishers_bytes() {
    let root = temporary_sdk();
    let roots = &[root.path()];
    succeeds(sync(roots, &["--revision", REVISION]));
    let pin = root.path().join(PIN);
    let original = fs::read(&pin).unwrap();
    let pending = root.path().join("tools/tooling-sources.json.pending");
    fs::write(&pending, "another publisher").unwrap();
    let result = sync(roots, &["--revision", OTHER]);
    assert!(!result.status.success());
    assert_eq!(fs::read(&pin).unwrap(), original);
    assert_eq!(fs::read_to_string(&pending).unwrap(), "another publisher");
}

#[cfg(unix)]
#[test]
fn linked_configuration_in_a_later_checkout_blocks_writes() {
    use std::os::unix::fs::symlink;

    let first = temporary_sdk();
    let last = temporary_sdk();
    let external = tempfile::NamedTempFile::new().unwrap();
    let destination = last.path().join("tools");
    fs::create_dir(&destination).unwrap();
    symlink(external.path(), destination.join("tooling-sources.json")).unwrap();
    fails(
        sync(&[first.path(), last.path()], &["--revision", REVISION]),
        "linked or non-ordinary SDK configuration",
    );
    assert!(!first.path().join("tools").exists());
    assert!(!last.path().join(".editorconfig").exists());
    assert!(fs::read(external.path()).unwrap().is_empty());
}
