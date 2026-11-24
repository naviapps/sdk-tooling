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

fn editorconfig(template: &[u8], current: &[u8]) -> Vec<u8> {
    let headers: Vec<&[u8]> = template
        .split(|&byte| byte == b'\n')
        .map(|line| line.strip_suffix(b"\r").unwrap_or(line))
        .filter(|line| line.starts_with(b"[") && line.ends_with(b"]"))
        .collect();
    let sections = current
        .windows(2)
        .enumerate()
        .filter(|&(_, window)| window == b"\n[")
        .map(|(index, _)| index + 1);
    let mut starts = std::iter::once(0)
        .chain(sections)
        .chain(std::iter::once(current.len()))
        .peekable();
    let mut desired = template.to_vec();
    while let Some(start) = starts.next() {
        let Some(&end) = starts.peek() else { break };
        let section = &current[start..end];
        let header = section
            .split(|&byte| matches!(byte, b'\r' | b'\n'))
            .next()
            .unwrap_or_default();
        if header.starts_with(b"[") && !headers.contains(&header) {
            desired.truncate(desired.trim_ascii_end().len());
            desired.extend_from_slice(b"\n\n");
            desired.extend_from_slice(section.trim_ascii_end());
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

fn tooling_pin(source: &[u8]) -> Result<(&str, Range<usize>)> {
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
    let span = start..start + revision.len();
    Ok((&source[span.clone()], span))
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
    fn write(self) -> Result<()> {
        let parent = self
            .path
            .parent()
            .ok_or("SDK configuration has no parent")?;
        if !metadata(parent)?.is_some_and(|info| info.is_dir() && !is_linked(&info)) {
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
            if ordinary_contents(&self.path)?.as_deref() != Some(self.previous.as_slice()) {
                return Err(format!("SDK configuration changed: {}", self.path.display()).into());
            }
            file.write_all(&self.desired)?;
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
        let (actual, span) = tooling_pin(&current_mise)?;
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
        if let Some(revision) = &options.revision
            && actual != revision
        {
            if options.check {
                return Err(format!(
                    "SDK tooling pin differs from the requested revision: {} (expected {revision}, found {actual})",
                    mise.display()
                ).into());
            }
            let mut desired = current_mise.clone();
            desired[span].copy_from_slice(revision.as_bytes());
            replacements.push(Replacement {
                path: mise,
                previous: current_mise,
                desired,
            });
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
    for replacement in replacements {
        replacement.write()?;
    }
    Ok(())
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
