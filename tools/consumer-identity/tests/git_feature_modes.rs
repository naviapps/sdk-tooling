use std::{
    env,
    ffi::OsStr,
    fs,
    io::{ErrorKind, Write},
    path::{self, PathBuf},
    process::{self, Command, Output, Stdio},
};

const ORIGIN: &str = "https://github.com/naviapps/consumer-test.git";

struct Fixture(PathBuf);

impl Fixture {
    fn new() -> Self {
        for index in 0.. {
            // Git and file URLs require the ordinary drive path on Windows.
            let path = path::absolute(
                env::temp_dir().join(format!("sdk-consumer-modes-{}-{index}", process::id())),
            )
            .unwrap();
            match fs::create_dir(&path) {
                Ok(()) => return Self(path),
                Err(error) if error.kind() == ErrorKind::AlreadyExists => continue,
                Err(error) => panic!("Could not create a Cargo fixture: {error}"),
            }
        }
        unreachable!()
    }

    fn write(&self, path: &str, text: &str) {
        let path = self.0.join(path);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, text).unwrap();
    }
}

impl Drop for Fixture {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn run(command: &mut Command) -> Vec<u8> {
    let output = command.output().unwrap();
    assert!(output.status.success(), "{command:?}: {output:?}");
    output.stdout
}

fn isolated_git(program: impl AsRef<OsStr>) -> Command {
    let mut command = Command::new(program);
    for (name, _) in env::vars_os() {
        if name
            .to_string_lossy()
            .to_ascii_uppercase()
            .starts_with("GIT_")
        {
            command.env_remove(name);
        }
    }
    command
        .env("GIT_CONFIG_NOSYSTEM", "1")
        .env("GIT_ALLOW_PROTOCOL", "file");
    command
}

fn invoke(label: &str, pins: &[&str], observed: &[u8]) -> Output {
    let mut command = Command::new(env!("CARGO_BIN_EXE_sdk-consumer-identity"))
        .arg(label)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    {
        let mut input = command.stdin.take().unwrap();
        input.write_all(pins.join("\0").as_bytes()).unwrap();
        input.write_all(b"\0").unwrap();
        input.write_all(observed).unwrap();
    }
    command.wait_with_output().unwrap()
}

#[test]
fn preserves_pinned_workspace_packages_across_actual_cargo_feature_modes() {
    let fixture = Fixture::new();
    fixture.write(
        "sdk/Cargo.toml",
        "[workspace]\nresolver = \"3\"\nmembers = [\"recognition\", \"rendering\", \"companion\"]\n",
    );
    for package in ["recognition", "rendering", "companion"] {
        let companion = if package == "recognition" {
            "[dependencies]\ncompanion = { path = \"../companion\" }\n"
        } else {
            ""
        };
        fixture.write(
            &format!("sdk/{package}/Cargo.toml"),
            &format!("[package]\nname = \"{package}\"\nversion = \"1.0.0\"\nedition = \"2024\"\n{companion}"),
        );
        fixture.write(&format!("sdk/{package}/src/lib.rs"), "");
    }
    let sdk = fixture.0.join("sdk");
    let config = fixture.0.join("gitconfig");
    let git = |arguments: &[&str]| {
        run(isolated_git("git")
            .current_dir(&sdk)
            .env("GIT_CONFIG_GLOBAL", &config)
            .args([
                "-c",
                "core.hooksPath=.",
                "-c",
                "commit.gpgsign=false",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
            ])
            .args(arguments))
    };
    git(&["init", "--quiet", "--template="]);
    git(&["add", "."]);
    git(&["commit", "--quiet", "-m", "portable SDK fixture"]);
    let revision = String::from_utf8(git(&["rev-parse", "HEAD"])).unwrap();
    let revision = revision.trim();
    fixture.write(
        "client/Cargo.toml",
        &format!(
            "[package]\nname = \"outside-client\"\nversion = \"0.0.0\"\nedition = \"2024\"\n\
             [workspace]\n[features]\ndefault = [\"geometry\"]\ngeometry = [\"dep:rendering\"]\nraster = [\"geometry\"]\n\
             [dependencies]\nrecognition = {{ git = \"{ORIGIN}\", rev = \"{revision}\" }}\n\
             rendering = {{ git = \"{ORIGIN}\", rev = \"{revision}\", optional = true }}\n"
        ),
    );
    fixture.write("client/src/main.rs", "fn main() {}\n");
    let client = fixture.0.join("client");
    let python = env::var_os("PREPARED_PYTHON").unwrap_or_else(|| {
        if cfg!(windows) {
            "python".into()
        } else {
            "python3".into()
        }
    });
    run(Command::new(&python).args([
        "-B", "-c",
        "from pathlib import Path\nimport sys\nPath(sys.argv[1]).write_text(f'[url \"{Path(sys.argv[2]).as_uri()}\"]\\n  insteadOf = {sys.argv[3]}\\n', encoding='utf-8')",
    ]).arg(&config).arg(&sdk).arg(ORIGIN));
    let cargo = |arguments: &[&str]| {
        run(isolated_git(env!("CARGO"))
            .current_dir(&client)
            .env("CARGO_HOME", fixture.0.join("cargo-home"))
            .env("CARGO_TARGET_DIR", fixture.0.join("target"))
            .env(
                "CARGO_NET_OFFLINE",
                if arguments[0] == "generate-lockfile" {
                    "false"
                } else {
                    "true"
                },
            )
            .env("CARGO_NET_GIT_FETCH_WITH_CLI", "true")
            .env("GIT_CONFIG_GLOBAL", &config)
            .args(arguments))
    };
    cargo(&["generate-lockfile"]);
    let lock = fs::read(client.join("Cargo.lock")).unwrap();
    let identity = format!("git+{ORIGIN}?rev={revision}#{revision}");
    let metadata = |features: &str| {
        let mut arguments = vec![
            "metadata",
            "--locked",
            "--offline",
            "--format-version",
            "1",
            "--no-default-features",
        ];
        if !features.is_empty() {
            arguments.extend(["--features", features]);
        }
        cargo(&arguments)
    };
    let rendered = [
        ("Cargo.lock", "lock", lock),
        ("Cargo metadata", "recognition", metadata("")),
        ("Cargo metadata", "geometry", metadata("geometry")),
        ("Cargo metadata", "raster", metadata("raster")),
    ];
    for (label, mode, raw) in rendered {
        fs::write(fixture.0.join("evidence"), raw).unwrap();
        let records = run(Command::new(&python)
            .args(["-B", "-c", include_str!("read_cargo_records.py"), label])
            .arg(fixture.0.join("evidence")));
        let pins = [
            "git",
            "recognition",
            &identity,
            "",
            "git",
            "rendering",
            &identity,
            "",
        ];
        let mut optional = pins;
        if mode == "recognition" {
            optional[7] = "optional";
            assert_rejected(invoke(label, &pins, &records), label);
            println!("{mode}: a missing required package is rejected");
        }
        let accepted = invoke(label, &optional, &records);
        assert!(accepted.status.success(), "{accepted:?}");
        assert!(accepted.stdout.is_empty() && accepted.stderr.is_empty());
        let mut wrong = optional;
        wrong[2] = "git+https://github.com/naviapps/other.git";
        assert_rejected(invoke(label, &wrong, &records), label);
        println!("{mode}: exact workspace identity is accepted; wrong origin is rejected");
    }
}

fn assert_rejected(output: Output, label: &str) {
    assert_eq!(output.status.code(), Some(2), "{output:?}");
    assert!(output.stdout.is_empty());
    assert_eq!(
        output.stderr,
        format!("The client {label} must retain the exact public SDK Git identities\n").as_bytes()
    );
}
