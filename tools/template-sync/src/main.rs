use std::{
    collections::BTreeMap,
    env,
    error::Error,
    ffi::{OsStr, OsString},
    fs::{self, File, Metadata},
    io::{self, Write},
    path::{Path, PathBuf},
    process::ExitCode,
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;

const REPOSITORY: &str = "naviapps/sdk-tooling";
const BASE: [(&str, &str); 4] = [
    ("_tooling.py", "tools/_tooling.py"),
    (".editorconfig", ".editorconfig"),
    (".gitattributes", ".gitattributes"),
    ("rust-toolchain.toml", "rust-toolchain.toml"),
];
const QUALITY: [(&str, &str); 6] = [
    ("ruff.toml", "ruff.toml"),
    (".clang-format", ".clang-format"),
    (".dprint.json", ".dprint.json"),
    (".swiftlint.yml", ".swiftlint.yml"),
    ("mise.toml", "mise.toml"),
    ("quality.py", "tools/quality.py"),
];

#[derive(Default)]
struct Options {
    check: bool,
    swift_format: bool,
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
                Some("--swift-format") => options.swift_format = true,
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
            return Err("Select at least one Rust SDK checkout".into());
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

fn check_pin(path: &Path, current: Option<Vec<u8>>, revision: Option<&str>) -> Result<()> {
    let pin: Option<BTreeMap<String, String>> =
        current.and_then(|data| serde_json::from_slice(&data).ok());
    let Some(actual) = pin
        .as_ref()
        .filter(|map| map.len() == 1)
        .and_then(|map| map.get(REPOSITORY))
        .filter(|value| immutable_revision(value))
    else {
        return Err(format!(
            "SDK tooling pin is not canonical and immutable: {}",
            path.display()
        )
        .into());
    };
    if let Some(expected) = revision
        && actual != expected
    {
        return Err(format!(
            "SDK tooling pin differs from the requested revision: {} (expected {expected}, found {actual})",
            path.display()
        )
        .into());
    }
    Ok(())
}

struct Replacement {
    path: PathBuf,
    previous: Option<Vec<u8>>,
    desired: Vec<u8>,
}

impl Replacement {
    fn write(self) -> Result<()> {
        let parent = self
            .path
            .parent()
            .ok_or("SDK configuration has no parent")?;
        if metadata(parent)?.is_some_and(|info| !info.is_dir() || is_linked(&info)) {
            return Err(
                format!("SDK configuration directory changed: {}", parent.display()).into(),
            );
        }
        fs::create_dir_all(parent)?;
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
            if ordinary_contents(&self.path)? != self.previous {
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

fn plan(options: &Options, templates: &Path) -> Result<Vec<Replacement>> {
    let mut mappings = BASE.to_vec();
    if options.swift_format {
        mappings.push((".swift-format", ".swift-format"));
    }
    if options.quality {
        mappings.extend(QUALITY);
    }
    let configs: Vec<_> = mappings
        .into_iter()
        .map(|(source, relative)| Ok((relative, fs::read(templates.join(source))?)))
        .collect::<io::Result<_>>()?;
    let mut replacements = Vec::new();
    let mut seen = Vec::new();
    for path in &options.roots {
        let root = path.canonicalize()?;
        if seen.contains(&root) {
            continue;
        }
        seen.push(root.clone());
        let tools = root.join("tools");
        if !root.join("Cargo.toml").is_file()
            || metadata(&tools)?.is_some_and(|info| !info.is_dir() || is_linked(&info))
        {
            return Err("Select a Rust SDK checkout with ordinary tool files".into());
        }
        for (relative, template) in &configs {
            let path = root.join(relative);
            let previous = ordinary_contents(&path)?;
            let desired = if path.file_name() == Some(OsStr::new(".editorconfig")) {
                editorconfig(template, previous.as_deref().unwrap_or_default())
            } else {
                template.clone()
            };
            if previous.as_deref() != Some(&desired) {
                replacements.push(Replacement {
                    path,
                    previous,
                    desired,
                });
            }
        }
        let pin = tools.join("tooling-sources.json");
        let current = ordinary_contents(&pin)?;
        if options.check {
            check_pin(&pin, current, options.revision.as_deref())?;
        } else if let Some(revision) = &options.revision {
            let desired = format!("{{\n  \"{REPOSITORY}\": \"{revision}\"\n}}\n").into_bytes();
            if current.as_deref() != Some(&desired) {
                replacements.push(Replacement {
                    path: pin,
                    previous: current,
                    desired,
                });
            }
        }
    }
    Ok(replacements)
}

fn run() -> Result<()> {
    let options = Options::parse(env::args_os().skip(1))?;
    let templates = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../templates");
    let replacements = plan(&options, &templates)?;
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
