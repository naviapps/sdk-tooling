use std::{
    env,
    error::Error,
    ffi::OsString,
    fs::{self, File, Metadata},
    io::{self, Write},
    ops::Range,
    path::{Path, PathBuf},
    process::ExitCode,
};

use toml_edit::Document;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const CLI_PREFIX: &str = "uvx --from git+https://github.com/naviapps/sdk-tooling.git@";
const WORKFLOW_PATH: &str = ".github/workflows/check.yml";
const WORKFLOW_PREFIX: &str = "naviapps/sdk-tooling/.github/workflows/check-sdk.yml@";
const RUST_TEMPLATES: [(&str, &[u8]); 2] = [
    (
        ".editorconfig",
        include_bytes!("../templates/.editorconfig"),
    ),
    (
        "rust-toolchain.toml",
        include_bytes!("../templates/rust-toolchain.toml"),
    ),
];
const QUALITY_TEMPLATES: [(&str, &[u8]); 2] = [
    ("ruff.toml", include_bytes!("../templates/ruff.toml")),
    (
        ".swiftlint.yml",
        include_bytes!("../templates/.swiftlint.yml"),
    ),
];

#[derive(Default)]
struct Options {
    check: bool,
    quality: bool,
    revision: Option<String>,
    roots: Vec<PathBuf>,
}

impl Options {
    fn parse(args: impl IntoIterator<Item = OsString>) -> Result<Self> {
        let mut options = Self::default();
        let mut args = args.into_iter();
        while let Some(argument) = args.next() {
            match argument.to_str() {
                Some("--check") => options.check = true,
                Some("--quality") => options.quality = true,
                Some("--revision") => {
                    options.revision = Some(
                        args.next()
                            .and_then(|value| value.into_string().ok())
                            .ok_or("An immutable tooling revision is required")?,
                    );
                }
                Some("--") => {
                    options.roots.extend(args.map(PathBuf::from));
                    break;
                }
                _ => return Err("Unknown sync option. Pass checkout paths after --.".into()),
            }
        }
        if options.roots.is_empty() {
            return Err("Select at least one SDK checkout".into());
        }
        if match &options.revision {
            Some(revision) => !immutable_revision(revision),
            None => !options.check,
        } {
            return Err("An immutable tooling revision is required".into());
        }
        Ok(options)
    }
}

fn immutable_revision(revision: &str) -> bool {
    revision.len() == 40
        && revision
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

fn editorconfig_headers(config: &[u8]) -> impl Iterator<Item = (usize, &[u8])> {
    let mut offset = 0;
    config
        .split_inclusive(|&byte| byte == b'\n')
        .filter_map(move |line| {
            let start = offset;
            offset += line.len();
            let header = line.trim_ascii();
            (header.starts_with(b"[") && header.ends_with(b"]")).then_some((start, header))
        })
}

fn editorconfig(template: &[u8], current: &[u8]) -> Vec<u8> {
    let headers: Vec<_> = editorconfig_headers(template)
        .map(|(_, header)| header)
        .collect();
    let mut sections = editorconfig_headers(current).peekable();
    let mut desired = template.to_vec();
    while let Some((start, header)) = sections.next() {
        if !headers.contains(&header) {
            let end = sections.peek().map_or(current.len(), |(offset, _)| *offset);
            desired.truncate(desired.trim_ascii_end().len());
            desired.extend_from_slice(b"\n\n");
            desired.extend_from_slice(current[start..end].trim_ascii_end());
            desired.push(b'\n');
        }
    }
    desired
}

fn metadata(path: &Path) -> io::Result<Option<Metadata>> {
    match fs::symlink_metadata(path) {
        Ok(metadata) => Ok(Some(metadata)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    }
}

fn is_linked(info: &Metadata) -> bool {
    #[cfg(windows)]
    {
        use std::os::windows::fs::MetadataExt;
        // Directory junctions are reparse points as well.
        const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;
        info.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0
    }
    #[cfg(not(windows))]
    info.file_type().is_symlink()
}

fn ordinary_contents(path: &Path) -> Result<Option<Vec<u8>>> {
    match metadata(path)? {
        Some(info) if info.is_file() && !is_linked(&info) => Ok(Some(fs::read(path)?)),
        Some(_) => Err(format!(
            "Refusing to replace a linked or non-ordinary SDK configuration: {}",
            path.display()
        )
        .into()),
        None => Ok(None),
    }
}

fn tooling_pin(source: &[u8]) -> Result<Range<usize>> {
    let source = std::str::from_utf8(source)?;
    let document = Document::parse(source)?;
    let run = document
        .get("tasks")
        .and_then(|tasks| tasks.get("sdk-tooling"))
        .and_then(|task| task.get("run"))
        .ok_or("SDK checkout requires a [tasks.sdk-tooling] run command in mise.toml")?;
    let immutable = "SDK tooling task must run the shared CLI at a canonical immutable revision";
    let revision = run
        .as_str()
        .and_then(|command| {
            command
                .strip_prefix(CLI_PREFIX)?
                .strip_suffix(" sdk-tooling")
        })
        .filter(|revision| immutable_revision(revision))
        .ok_or(immutable)?;
    let value = run.span().ok_or(immutable)?;
    let start = source[value.clone()].find(revision).ok_or(immutable)? + value.start;
    Ok(start..start + revision.len())
}

fn workflow_source(root: &Path) -> Result<Option<Vec<u8>>> {
    let path = root.join(WORKFLOW_PATH);
    let Some(info) = metadata(&path)? else {
        return Ok(None);
    };
    if !info.is_file() || is_linked(&info) || path.canonicalize()? != path {
        return Err(format!("Expected an ordinary SDK workflow: {}", path.display()).into());
    }
    Ok(Some(fs::read(path)?))
}

fn workflow_pin(source: &[u8]) -> Result<Option<Range<usize>>> {
    let mut offset = 0;
    let mut jobs = false;
    let mut pin = None;
    for line in std::str::from_utf8(source)?.split_inclusive('\n') {
        let start = offset;
        offset += line.len();
        let text = line.trim_ascii();
        if text.is_empty() || text.starts_with('#') {
            continue;
        }
        if line.starts_with(text) {
            jobs = text.split_ascii_whitespace().next() == Some("jobs:");
        }
        let Some(reference) = line
            .strip_prefix("    uses:")
            .and_then(|value| value.split_ascii_whitespace().next())
            .filter(|_| jobs)
        else {
            continue;
        };
        if let Some(revision) = reference
            .trim_matches(['\'', '"'])
            .strip_prefix(WORKFLOW_PREFIX)
        {
            if pin.is_some() || !immutable_revision(revision) {
                return Err(
                    "Shared SDK check workflow requires one canonical immutable revision".into(),
                );
            }
            let start = start + line.find(WORKFLOW_PREFIX).unwrap() + WORKFLOW_PREFIX.len();
            pin = Some(start..start + revision.len());
        }
    }
    Ok(pin)
}

fn ordinary_file(path: &Path) -> io::Result<bool> {
    Ok(metadata(path)?.is_some_and(|info| info.is_file() && !is_linked(&info)))
}

fn templates(options: &Options, rust: bool) -> Vec<(&'static str, &'static [u8])> {
    let mut configs = Vec::new();
    if rust {
        configs.extend(RUST_TEMPLATES);
    }
    if options.quality {
        configs.extend(QUALITY_TEMPLATES);
    }
    configs
}

struct Replacement {
    path: PathBuf,
    previous: Vec<u8>,
    desired: Vec<u8>,
}

impl Replacement {
    fn pin(path: PathBuf, previous: Vec<u8>, span: Range<usize>, revision: &str) -> Self {
        let mut desired = previous.clone();
        desired[span].copy_from_slice(revision.as_bytes());
        Self {
            path,
            previous,
            desired,
        }
    }

    fn write(&self, previous: &[u8], desired: &[u8]) -> Result<()> {
        let parent = self
            .path
            .parent()
            .ok_or("SDK configuration has no parent")?;
        if parent.canonicalize()? != parent {
            return Err(
                format!("SDK configuration directory changed: {}", parent.display()).into(),
            );
        }
        // Use the same exclusive sibling convention as the existing tooling
        // writer. An existing pending file belongs to another invocation.
        let file_name = self
            .path
            .file_name()
            .ok_or("Missing configuration filename")?;
        let mut temporary_name = file_name.to_owned();
        temporary_name.push(".pending");
        let temporary = self.path.with_file_name(temporary_name);
        let mut file = File::create_new(&temporary).map_err(|error| {
            format!(
                "Cannot stage configuration {}: {error}",
                self.path.display()
            )
        })?;
        let updated: Result<()> = (|| {
            if ordinary_contents(&self.path)?.as_deref() != Some(previous) {
                return Err(format!("SDK configuration changed: {}", self.path.display()).into());
            }
            file.write_all(desired)?;
            if let Some(previous) = metadata(&self.path)? {
                file.set_permissions(previous.permissions())?;
            }
            drop(file);
            fs::rename(&temporary, &self.path)?;
            Ok(())
        })();
        if updated.is_err() {
            let _ = fs::remove_file(&temporary);
        }
        updated
    }
}

fn apply(replacements: &[Replacement]) -> Result<()> {
    for (index, replacement) in replacements.iter().enumerate() {
        if let Err(error) = replacement.write(&replacement.previous, &replacement.desired) {
            let mut failure = format!("Cannot update {}: {error}", replacement.path.display());
            for installed in replacements[..index].iter().rev() {
                if let Err(error) = installed.write(&installed.desired, &installed.previous) {
                    failure.push_str(&format!(
                        "\nCannot restore {}: {error}",
                        installed.path.display()
                    ));
                }
            }
            return Err(failure.into());
        }
    }
    Ok(())
}

fn plan(options: &Options) -> Result<Vec<Replacement>> {
    let mut replacements = Vec::new();
    let mut seen = Vec::new();
    for path in &options.roots {
        let root = path.canonicalize()?;
        if seen.contains(&root) {
            continue;
        }
        seen.push(root.clone());

        let mise = root.join("mise.toml");
        let rust = ordinary_file(&root.join("Cargo.toml"))?;
        if !rust && !ordinary_file(&root.join("Package.swift"))? {
            return Err(format!(
                "Select an SDK checkout with Cargo.toml or Package.swift: {}",
                root.display()
            )
            .into());
        }
        let current_mise = ordinary_contents(&mise)?
            .ok_or_else(|| format!("SDK checkout requires mise.toml: {}", root.display()))?;
        let span = tooling_pin(&current_mise)?;
        let actual = std::str::from_utf8(&current_mise[span.clone()])?;
        let revision = options.revision.as_deref().unwrap_or(actual);
        if options.check && actual != revision {
            return Err(format!(
                "SDK tooling pin differs from the requested revision: {} (expected {revision}, found {actual})",
                mise.display()
            ).into());
        }
        for (name, template) in templates(options, rust) {
            let path = root.join(name);
            let Some(previous) = ordinary_contents(&path)? else {
                continue;
            };
            let desired = if name == ".editorconfig" {
                editorconfig(template, &previous)
            } else {
                template.to_vec()
            };
            if previous != desired {
                replacements.push(Replacement {
                    path,
                    previous,
                    desired,
                });
            }
        }
        if let Some(workflow) = workflow_source(&root)?
            && let Some(pin) = workflow_pin(&workflow)?
            && workflow[pin.clone()] != *revision.as_bytes()
        {
            let path = root.join(WORKFLOW_PATH);
            if options.check {
                let actual = std::str::from_utf8(&workflow[pin])?;
                return Err(format!(
                    "Shared SDK check workflow pin differs from the CLI revision: {} (expected {revision}, found {actual})",
                    path.display()
                ).into());
            }
            replacements.push(Replacement::pin(path, workflow, pin, revision));
        }
        if let Some(revision) = &options.revision
            && actual != revision
        {
            replacements.push(Replacement::pin(mise, current_mise, span, revision));
        }
    }
    Ok(replacements)
}

fn run() -> Result<()> {
    let options = Options::parse(env::args_os().skip(1))?;
    let replacements = plan(&options)?;
    if options.check && !replacements.is_empty() {
        let names: Vec<_> = replacements
            .iter()
            .map(|replacement| replacement.path.display().to_string())
            .collect();
        return Err(format!("SDK tooling templates differ: {}", names.join(", ")).into());
    }
    apply(&replacements)
}

fn main() -> ExitCode {
    match run() {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("{error}");
            ExitCode::FAILURE
        }
    }
}
