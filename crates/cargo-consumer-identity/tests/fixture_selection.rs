use std::{
    io::Write,
    process::{Command, Output, Stdio},
};

fn request(operation: &str, input: &[u8]) -> Output {
    let mut process = Command::new(env!("CARGO_BIN_EXE_sdk-cargo-consumer-identity"))
        .arg(operation)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    process.stdin.take().unwrap().write_all(input).unwrap();
    process.wait_with_output().unwrap()
}

fn select(operation: &str, fields: &[&str]) -> Output {
    request(operation, fields.join("\0").as_bytes())
}

fn rejected(output: Output, diagnostic: &str) {
    assert_eq!(output.status.code(), Some(2));
    assert!(output.stdout.is_empty());
    assert_eq!(output.stderr, format!("{diagnostic}\n").as_bytes());
}

#[test]
fn cargo_metadata_names_a_default_or_one_binary() {
    for fields in [["", "fixture"].as_slice(), &["fixture", "other", "fixture"]] {
        let output = select("select-binary", fields);
        assert!(output.status.success(), "{output:?}");
        assert_eq!(output.stdout, b"fixture");
        assert!(output.stderr.is_empty());
    }
    for fields in [[""].as_slice(), &["", "a", "b"], &["wrong", "fixture"]] {
        rejected(
            select("select-binary", fields),
            "Select the external Rust fixture binary with default-run",
        );
    }
}

#[test]
fn cargo_build_must_identify_one_executable_for_the_selected_client_binary() {
    let path = "C:\\fixture path\\検証.exe";
    let mut fields = vec!["client-id", "fixture"];
    for record in [
        ["dependency-id", "fixture", "bin", "dep.exe"],
        ["client-id", "other", "bin", "other.exe"],
        ["client-id", "fixture", "", "library"],
        ["client-id", "fixture", "bin", ""],
    ] {
        fields.extend(record);
    }
    let diagnostic = "Cargo must report the freshly built Windows Rust fixture executable";
    rejected(select("select-executable", &fields), diagnostic);
    fields.extend(["client-id", "fixture", "bin", path]);
    let output = select("select-executable", &fields);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(output.stdout, path.as_bytes());
    assert!(output.stderr.is_empty());
    fields.extend(["client-id", "fixture", "bin", path]);
    rejected(select("select-executable", &fields), diagnostic);
    fields.push("unpaired");
    rejected(
        select("select-executable", &fields),
        "Incomplete Cargo fixture artifact record",
    );
}

#[test]
fn corrupt_utf8_cannot_select_a_different_artifact_path() {
    let input = b"client-id\0fixture\0client-id\0fixture\0bin\0C:\\fixture-\xff.exe";
    rejected(
        request("select-executable", input),
        "Cannot read Cargo package evidence",
    );
}

#[test]
fn ci_bundle_selection_requires_a_unique_exact_identity_at_the_manifest_position() {
    let repository = "naviapps/Fixture";
    let url = "https://github.com/naviapps/Fixture.git";
    let folded_url = "https://github.com/naviapps/fixture.git";
    let revision = "a0123456789012345678901234567890123456789";
    let header = [repository, "naviapps/fixture", url, folded_url, revision];
    let exact = [
        repository,
        "naviapps/fixture",
        url,
        folded_url,
        revision,
        "1.bundle",
    ];
    let unrelated = ["other", "other", "", "", "", "0.bundle"];
    let execute = |first: [&str; 6], second: [&str; 6]| {
        select("select-ci-bundle", &[&header[..], &first, &second].concat())
    };

    let output = execute(unrelated, exact);
    assert!(output.status.success(), "{output:?}");
    assert_eq!(output.stdout, b"1");
    assert!(output.stderr.is_empty());

    let ambiguous = "Missing or ambiguous additional SDK Git pin in CI source manifest";
    let invalid = "Invalid additional SDK Git bundle identity";
    rejected(execute(unrelated, unrelated), ambiguous);
    let mut alias = unrelated;
    alias[3] = folded_url;
    rejected(execute(alias, exact), ambiguous);
    let mut collision = unrelated;
    collision[5] = "1.bundle";
    rejected(execute(collision, exact), invalid);
    let mut wrong_pin = exact;
    wrong_pin[4] = "b0123456789012345678901234567890123456789";
    rejected(execute(unrelated, wrong_pin), invalid);
    rejected(execute(exact, unrelated), invalid);
}
