use std::{
    env, fs,
    io::ErrorKind,
    path::{Path, PathBuf},
    process::{self, Command, Output, Stdio},
    sync::atomic::{AtomicUsize, Ordering},
};

static NEXT: AtomicUsize = AtomicUsize::new(0);

struct Rules(PathBuf);

impl Rules {
    fn new() -> Self {
        loop {
            let directory = env::temp_dir().join(format!(
                "sdk-dep-info-{}-{}",
                process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match fs::create_dir(&directory) {
                Ok(()) => return Self(directory),
                Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("Could not create an isolated rule directory: {error}"),
            }
        }
    }

    fn write(&self, name: &str, bytes: &[u8]) -> PathBuf {
        let path = self.0.join(name);
        fs::write(&path, bytes).unwrap();
        path
    }

    fn compiler_rule(&self, source: &Path, name: &str) -> PathBuf {
        let path = self.0.join(name);
        let output = Command::new(env::var_os("RUSTC").unwrap_or_else(|| "rustc".into()))
            .current_dir(&self.0)
            .args([
                "--edition=2024",
                "--crate-name",
                "sdk_dep_info_fixture",
                "--crate-type",
                "lib",
                "--emit=dep-info",
                "-o",
            ])
            .arg(&path)
            .arg(source)
            .output()
            .unwrap();
        assert!(output.status.success(), "{output:?}");
        path
    }

    fn read(&self, files: &[&Path]) -> Output {
        Command::new(env!("CARGO_BIN_EXE_sdk-rustc-dep-info"))
            .current_dir(&self.0)
            .arg("--files")
            .args(files)
            .stdin(Stdio::null())
            .output()
            .unwrap()
    }
}

impl Drop for Rules {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

#[test]
fn reads_two_compiler_generated_rules_with_a_shared_escaped_source() {
    let rules = Rules::new();
    let source = "#[path = \"gesture 空 module.rs\"] mod shared;\npub use shared::probe;\n";
    let first = rules.write("first input.rs", source.as_bytes());
    let second = rules.write("second input.rs", source.as_bytes());
    let shared = rules.write("gesture 空 module.rs", b"pub fn probe() -> u32 { 1 }\n");
    let first_rule = rules.compiler_rule(&first, "first output.d");
    let second_rule = rules.compiler_rule(&second, "second output.d");

    let output = rules.read(&[&first_rule, &second_rule]);
    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    let text = String::from_utf8(output.stdout).unwrap();
    let selected: Vec<_> = text
        .split('\n')
        .map(|name| rules.0.join(name).canonicalize().unwrap())
        .collect();
    let expected = [&first, &shared, &second, &shared].map(|path| path.canonicalize().unwrap());
    // The compiler may order sources differently within a rule on each host.
    assert_eq!(selected.len(), expected.len());
    for (actual, expected) in selected
        .as_chunks::<2>()
        .0
        .iter()
        .zip(expected.as_chunks::<2>().0)
    {
        assert_eq!(actual.iter().min(), expected.iter().min());
        assert_eq!(actual.iter().max(), expected.iter().max());
    }
}

#[test]
fn normalizes_crlf_and_lone_cr_rules_in_the_original_file_order() {
    let rules = Rules::new();
    let first = rules.write(
        "first rule.d",
        b"# compiler comment\r\noutput: first.rs \\\r\n generated.rs\r\n",
    );
    let second = rules.write(
        "second rule.d",
        b"# compiler comment\routput: second.rs \\\r with\\ space.rs\r",
    );

    let output = rules.read(&[&first, &second]);
    assert!(output.status.success(), "{output:?}");
    assert!(output.stderr.is_empty(), "{output:?}");
    assert_eq!(
        output.stdout,
        b"first.rs\ngenerated.rs\nsecond.rs\nwith space.rs"
    );
}

#[test]
fn reports_only_the_failed_read_and_withholds_earlier_output() {
    let rules = Rules::new();
    let first = rules.write("first rule.d", b"output: first.rs\n");
    let malformed = rules.write("依存 malformed.d", b"output: invalid\xff");
    let missing = rules.0.join("missing last.d");

    for failed in [&malformed, &missing] {
        let output = rules.read(&[&first, failed]);
        assert_eq!(output.status.code(), Some(1), "{output:?}");
        assert!(output.stdout.is_empty(), "{output:?}");
        let stderr = String::from_utf8(output.stderr).unwrap();
        assert!(
            stderr.contains(failed.file_name().unwrap().to_str().unwrap()),
            "{stderr}"
        );
        assert!(!stderr.contains("first rule.d"), "{stderr}");
    }
}
