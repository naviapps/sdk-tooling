use std::{
    fs,
    path::Path,
    process::{Command, Output},
};

use tempfile::TempDir;

const REVISION: &str = "1111111111111111111111111111111111111111";
const OTHER: &str = "2222222222222222222222222222222222222222";

fn command(revision: &str) -> String {
    format!("uvx --from git+https://github.com/naviapps/sdk-tooling.git@{revision} sdk-tooling")
}

fn write_mise(root: &Path, revision: &str) {
    fs::write(
        root.join("mise.toml"),
        format!("[tasks.sdk-tooling]\nrun = '{}'\n", command(revision)),
    )
    .unwrap();
}

fn temporary_sdk() -> TempDir {
    let root = tempfile::Builder::new()
        .prefix("SDK checkout ")
        .tempdir()
        .unwrap();
    fs::write(root.path().join("Cargo.toml"), "[workspace]\n").unwrap();
    write_mise(root.path(), REVISION);
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
    assert_eq!(output.status.code(), Some(1));
    let message = String::from_utf8_lossy(&output.stderr);
    assert!(message.contains(error), "{message}");
}

#[test]
fn cli_accepts_relative_checkout_paths_with_spaces_and_leading_hyphens() {
    let working_directory = tempfile::tempdir().unwrap();
    let roots = ["-sdk checkout", "another SDK"];
    for name in roots {
        let checkout = working_directory.path().join(name);
        fs::create_dir(&checkout).unwrap();
        fs::write(checkout.join("Cargo.toml"), "[workspace]\n").unwrap();
        fs::write(checkout.join("ruff.toml"), "line-length = 90\n").unwrap();
        write_mise(&checkout, REVISION);
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
    succeeds(invoke(&["--quality", "--revision", OTHER]));
    succeeds(invoke(&["--check", "--quality", "--revision", OTHER]));
    for name in roots {
        let checkout = working_directory.path().join(name);
        assert_eq!(
            fs::read(checkout.join("ruff.toml")).unwrap(),
            include_bytes!("../templates/ruff.toml")
        );
        assert!(!checkout.join("tools").exists());
    }
}

#[test]
fn check_reports_adopted_policy_drift_without_writing() {
    let root = temporary_sdk();
    let path = root.path();
    let ruff = path.join("ruff.toml");
    fs::write(&ruff, "line-length = 90\n").unwrap();
    succeeds(sync(&[path], &["--check"]));
    fails(sync(&[path], &["--check", "--quality"]), "templates differ");
    assert_eq!(fs::read_to_string(&ruff).unwrap(), "line-length = 90\n");

    succeeds(sync(&[path], &["--quality", "--revision", REVISION]));
    assert_eq!(
        fs::read(&ruff).unwrap(),
        include_bytes!("../templates/ruff.toml")
    );
    assert!(!path.join("rust-toolchain.toml").exists());
    assert!(!path.join("tools").exists());
}

#[test]
fn pin_updates_preserve_mise_tasks_and_sdk_owned_policy() {
    for rust in [true, false] {
        let root = temporary_sdk();
        let path = root.path();
        if !rust {
            fs::remove_file(path.join("Cargo.toml")).unwrap();
            fs::write(path.join("Package.swift"), "// swift-tools-version: 6.3\n").unwrap();
            fs::write(path.join(".editorconfig"), "root = true\n").unwrap();
        }
        let previous = command(REVISION);
        let mise = format!(
            "# Keep {REVISION} in this comment\r\n[tasks.\"sdk-tooling\"]\r\nrun = '{previous}' # review\r\n\r\n[tasks.check]\r\nrun = \"make test\"\r\n"
        );
        fs::write(path.join("mise.toml"), &mise).unwrap();
        let sdk_policies = [
            ".gitattributes",
            ".dprint.json",
            ".clang-format",
            ".swift-format",
        ];
        for policy in sdk_policies {
            fs::write(path.join(policy), "SDK-owned policy\n").unwrap();
        }

        fails(
            sync(&[path], &["--check", "--revision", OTHER]),
            "differs from the requested revision",
        );
        assert_eq!(fs::read_to_string(path.join("mise.toml")).unwrap(), mise);

        succeeds(sync(&[path], &["--quality", "--revision", OTHER]));
        let expected = mise.replacen(&previous, &command(OTHER), 1);
        assert_eq!(
            fs::read_to_string(path.join("mise.toml")).unwrap(),
            expected
        );
        for policy in sdk_policies {
            assert_eq!(
                fs::read_to_string(path.join(policy)).unwrap(),
                "SDK-owned policy\n"
            );
        }
        if !rust {
            assert_eq!(
                fs::read_to_string(path.join(".editorconfig")).unwrap(),
                "root = true\n"
            );
        }
        succeeds(sync(
            &[path],
            &["--quality", "--check", "--revision", OTHER],
        ));
    }
}

#[test]
fn malformed_or_legacy_checkouts_block_writes_to_all_selected_paths() {
    let first = temporary_sdk();
    let legacy = temporary_sdk();
    let roots = &[first.path(), legacy.path()];
    let mise = legacy.path().join("mise.toml");
    let legacy_tools = legacy.path().join("tools");
    fs::create_dir(&legacy_tools).unwrap();
    let legacy_pin = legacy_tools.join("tooling-sources.json");
    fs::write(
        &legacy_pin,
        format!(r#"{{"naviapps/sdk-tooling":"{REVISION}"}}"#),
    )
    .unwrap();
    let legacy_contents = fs::read(&legacy_pin).unwrap();

    let first_ruff = first.path().join("ruff.toml");
    fs::write(&first_ruff, "line-length = 90\n").unwrap();
    let update = || sync(roots, &["--quality", "--revision", OTHER]);
    write_mise(legacy.path(), "main");
    fails(update(), "canonical immutable revision");
    fs::write(&mise, "[tasks.check]\nrun = 'make test'\n").unwrap();
    fails(update(), "requires a [tasks.sdk-tooling] run command");
    fs::remove_file(&mise).unwrap();
    fails(update(), "requires mise.toml");

    assert_eq!(
        fs::read_to_string(&first_ruff).unwrap(),
        "line-length = 90\n"
    );
    assert!(
        fs::read_to_string(first.path().join("mise.toml"))
            .unwrap()
            .contains(REVISION)
    );
    assert_eq!(fs::read(&legacy_pin).unwrap(), legacy_contents);
    assert_eq!(fs::read_dir(&legacy_tools).unwrap().count(), 1);
}

#[test]
fn check_accepts_distinct_immutable_pins_unless_a_revision_is_required() {
    let first = temporary_sdk();
    let second = temporary_sdk();
    write_mise(second.path(), OTHER);
    let roots = &[first.path(), second.path()];

    succeeds(sync(roots, &["--check"]));
    fails(
        sync(roots, &["--check", "--revision", REVISION]),
        "differs from the requested revision",
    );
    fails(sync(roots, &["--revision", "main"]), "immutable");
    fails(sync(roots, &[]), "immutable");
    assert!(
        fs::read_to_string(second.path().join("mise.toml"))
            .unwrap()
            .contains(OTHER)
    );
}

#[test]
fn adopted_editorconfig_keeps_local_sections() {
    let root = temporary_sdk();
    let path = root.path();
    let editorconfig = path.join(".editorconfig");
    let local = b"[bindings/dotnet/src/Library/**.cs]\r\ndotnet_diagnostic.IDE0005.severity = warning\r\n\r\n[fixtures/*.cs]\r\nindent_size = 4";
    fs::write(
        &editorconfig,
        [b"root = true\r\n\r\n", local.as_slice()].concat(),
    )
    .unwrap();
    succeeds(sync(&[path], &["--revision", REVISION]));
    let merged = fs::read(&editorconfig).unwrap();
    for section in [
        b"[bindings/dotnet/src/Library/**.cs]\r\ndotnet_diagnostic.IDE0005.severity = warning"
            .as_slice(),
        b"[fixtures/*.cs]\r\nindent_size = 4".as_slice(),
    ] {
        assert!(merged.windows(section.len()).any(|bytes| bytes == section));
    }

    succeeds(sync(&[path], &["--check"]));
}

#[test]
fn editorconfig_check_detects_managed_headers_with_surrounding_whitespace() {
    let root = temporary_sdk();
    let path = root.path();
    let editorconfig = path.join(".editorconfig");
    let expected = [
        include_bytes!("../templates/.editorconfig").as_slice(),
        b"\n[fixtures/*.cs]  \nindent_size = 4\n",
    ]
    .concat();

    for header in [b"[*]  \n".as_slice(), b"  [*]\r\n"] {
        let previous = [expected.as_slice(), b"\n", header, b"indent_size = 8\n"].concat();
        fs::write(&editorconfig, &previous).unwrap();

        fails(sync(&[path], &["--check"]), ".editorconfig");
        assert_eq!(fs::read(&editorconfig).unwrap(), previous);

        succeeds(sync(&[path], &["--revision", REVISION]));
        assert_eq!(fs::read(&editorconfig).unwrap(), expected);
        succeeds(sync(&[path], &["--check"]));
    }
}

#[test]
fn a_pending_file_preserves_the_current_pin_and_another_publishers_bytes() {
    let root = temporary_sdk();
    let path = root.path();
    let pin = path.join("mise.toml");
    let original = fs::read(&pin).unwrap();
    let pending = path.join("mise.toml.pending");
    fs::write(&pending, "another publisher").unwrap();

    fails(
        sync(&[path], &["--revision", OTHER]),
        "Cannot stage configuration",
    );
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
    let mise = last.path().join("mise.toml");
    fs::remove_file(&mise).unwrap();
    symlink(external.path(), &mise).unwrap();
    let first_ruff = first.path().join("ruff.toml");
    fs::write(&first_ruff, "line-length = 90\n").unwrap();

    fails(
        sync(
            &[first.path(), last.path()],
            &["--quality", "--revision", OTHER],
        ),
        "linked or non-ordinary SDK configuration",
    );
    assert_eq!(
        fs::read_to_string(&first_ruff).unwrap(),
        "line-length = 90\n"
    );
    assert!(fs::read(external.path()).unwrap().is_empty());
}
