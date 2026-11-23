# SDK tooling reference

[Developer setup](../README.md#developer-bootstrap).

## Quality and documentation checks

### Run static checks

The shared driver selects Git
source and new files, excludes ignored output, vendored code, binary artifacts
and original notices, and rejects linked inputs. It never launches the product,
repairs an artifact or installs tools implicitly.
Directory exclusions preserve case and apply only to directories, so source
targets such as `Sources/Target` and extensionless tool files remain eligible.

```sh
mise run lint
mise run format
```

To run the Markdown, structured data and workflow gate:

```sh
mise exec python -- python3 -B -m sdk_tooling quality check --language data
```

Native CI follows source, test, template and build-policy changes;
documentation-only changes do not start workers.

### Source selection

Direct CI workspaces without Git metadata can supply `NAVI_CI_SOURCE_FILES` and
`NAVI_CI_SOURCE_FILES_SHA256` together. The normalized absolute path must name the ordinary
workspace-root `.navi-ci-source-files.json`; its SHA-256 binds the exact UTF-8 JSON bytes, bounded
to 16 MiB, with shape `{"version":1,"files":["sorted/relative/name.py"]}`.
Parent directory aliases resolving to that same workspace are accepted, including
macOS `/tmp` and `/private/tmp`; the manifest itself must not be a symlink.
The nonempty, sorted, unique POSIX list (at most 100,000 names) records CI's
captured tracked and unignored working files, including dirty edits. It applies the same directory, DocC and
linked-input policy as Git selection. Missing selected inputs, invalid metadata
or a mismatched hash fail without falling back to Git. The metadata file itself
is transport data and cannot be selected as source. Both variables absent retain
normal Git discovery, including skipped deleted files. Existing pinned consumers
must adopt a tooling revision with this support before using direct CI selection.

The digest binds selection metadata, not source contents. Checks and formatting
read the current selected file bytes, so formatting and subsequent edits remain
possible. Files added after capture require a fresh direct-workspace invocation;
the selection is never expanded by a recursive scan. Source immutability and
producer acceptance remain the caller's separate checks.

### Language and tool policy

#### Python and Swift

Python uses Ruff's default error checks, import hygiene and mutable-default detection;
assigning a local lambda remains a style choice. Swift uses swift-format's official
defaults for layout and identifier style, plus SourceKit-independent bug checks
selected from SwiftLint's built-in rules, including unused parameters. SwiftLint
does not add metrics, TODO policy or encoding preferences.

Whole-program unused Swift declarations require separate clean-build analysis and
are not an incremental gate. The configured SwiftLint analyzer rules can inspect a
complete verbose compiler log:

```sh
swiftlint analyze --config .swiftlint.yml --compiler-log-path BUILD_LOG --reporter json
```

Use the same source, build configuration and Xcode as the log. Keep its referenced
response files and build products available until analysis finishes; the log alone
is not a self-contained input. A partial log only covers the compilation commands
it contains. Public SDK declarations are retained. Review dynamic/Objective-C/Codable
findings before deleting code; analyzer output is a review report, not evidence that
all configurations or external callers are covered.

#### C, shell and workflows

C uses clang-format. On macOS and Linux, dprint formats owned `.sh` files with a
checksummed shfmt Wasm plugin. Bash syntax checks and ShellCheck validate the same
files.

Actionlint checks workflow definitions and runs ShellCheck for inline shell and
Pyflakes for inline Python when the repository pins them for the current platform.
Selected `.github/actionlint.yaml` (or `.yml`, with `.yaml` taking precedence) is
passed explicitly so custom labels work with or without Git metadata.
The quality gate passes the verified executable paths to actionlint and fails if
a required linter is missing or has the wrong version. It explicitly disables
undeclared or out-of-scope integrations. Repositories with embedded Python use the
Pyflakes hook; ordinary Python files remain with Ruff.

#### Markdown and structured data

Dprint formats owned Markdown, JSON, TOML and YAML with checksummed Wasm plugins,
without requiring Node in SDK repositories. Markdown inside `.docc` catalogs is left
to DocC validation to preserve double-backtick symbol links; other catalog assets
remain eligible for static checks.

#### Rust and managed code

Rust formatting stays with rustfmt; cargo-machete checks all owned Cargo projects
for unused dependencies without fetching packages or rewriting lockfiles. Its
Windows ARM64 developer CLI uses the upstream x64 binary under Windows emulation;
this is independent of native ARM64 SDK acceptance. Compiler warnings, Clippy and
managed build analyzers remain in their acceptance gates.

SDKs with C# include `--dotnet` for folder whitespace checks without restoring
packages. Managed acceptance opts into `verify_style` after its normal restore and
before building, packaging or running tests. It checks unused imports/private code
and non-public unused parameters in the selected project and its evaluated project
references, preserving build properties without another restore. Public APIs, native callbacks and dynamic entry points
are not inferred to be dead code from a missing local reference.

### Synchronize SDK policy

Run the locked Rust tool from the tooling checkout whose templates you intend
to adopt. It needs the checkout's pinned Rust and Cargo. On Windows, use a
Visual Studio developer shell for the selected Rust target.

```sh
mise exec -- cargo run --quiet --locked --manifest-path tools/template-sync/Cargo.toml -- \
  --revision FULL_COMMIT_SHA -- /absolute/path/to/sdk
mise exec -- cargo run --quiet --locked --manifest-path tools/template-sync/Cargo.toml -- \
  --check -- /absolute/path/to/sdk
```

Put tool options before the `--` that introduces the SDK paths. Relative paths
resolve from the working directory. To use a prepared Cargo cache without
network access, add `--offline` to the Cargo options.

Add `--quality` to select the shared static-check entry point and quality
templates. The tool preserves SDK-specific EditorConfig sections. Reapply SDK
tool additions after synchronizing `mise.toml`. Select `--swift-format` only for
SDKs that adopt that explicit formatting policy.

The tool checks all selected checkout paths before replacing files
individually and updates each pin after its templates. Pass
`--revision FULL_COMMIT_SHA` with `--check` to verify that exact pin without
rewriting it. Without a revision, the check accepts any canonical immutable pin.
Git attributes keep source line endings canonical while preserving artifact and
third-party notice bytes verbatim, including when a developer enables autocrlf.

### DocC catalog acceptance

`docs.check_catalogs` generates module-specific DocC archives in caller-selected
order, preserves dependency links, and invokes SDK-owned declaration checks. Every
conversion enables DocC's external link support and verifies both link metadata
files before another module uses it; a zero exit code with missing or malformed
link metadata is a failed check. Optional retained archives replace prior output
only after all conversions pass.

## Native artifacts and provenance

### macOS builder specifications

SDK-owned native specifications construct `native.NativeBuilder`. Cargo manifests
and module maps supply package, library and module names; the SDK explicitly
selects its C symbol prefix, minimum OS and component receipt. The module map
selects macOS public headers. SDKs with several native libraries declare a shared
`notice_packages` union so one library never prunes another library's notices.
The builder checks locked dependencies/notices, native CPU/OS/export contracts and
local anonymous signatures. It stages and verifies complete output before replacing the previous
artifact directory and rolls back failed publication. A per-checkout lock excludes
concurrent publishers without locking independent SDKs. A lock acquisition failure
reports the lock path and preserves filesystem errors such as I/O failures.

An application-owned Rust library can use the same builder with an explicit FFI
path, `specification` script and scoped `notice_directory` such as
`ThirdParty/ProductCore`. Only that notice tree contributes to its receipt and
framework; unrelated application notices remain untouched. Cargo package versions
may be declared in the FFI manifest or inherited from the workspace. Registry and
Rust-runtime retention accept the same optional notice directory and reject
parent paths or symlinked ancestors.

If artifact publication cannot restore the previous directory, the error identifies
the retained previous artifacts beside the destination. Inspect and recover that
tree after resolving the filesystem error before removing the rollback directory.

### Production source receipts

The macOS producer uses its pinned Rust compiler to build the dependency-rule
reader. It reads the first Make rule from every selected target's compiler
output in one invocation. The receipt records the reader's source hash and the
selected production paths.
Changes to a separate test-only Rust file do not invalidate an accepted framework;
editing a production module, its module declarations, manifests, notices or build
code does.
Inline tests share their production file's fingerprint. Selected native dependencies'
local build-script package trees remain conservative because scripts can inspect
directory inputs; unrelated workspace packages do not add their source trees.
Schema-3 receipts record validated relative roots and directory inventories,
including empty directories. Verification rescans their files and directories
without running Rust or Cargo, rejecting additions, removals and edits. Input trees reject symlinks,
repository-root/output overlap and custom build scripts outside their package root.
Older receipts require a rebuild with the pinned tooling. The full source and
selected directory inventories are also checked across each build. A new tooling
revision alone does not invalidate native output when its actual build modules
are unchanged. Production and build-tool hashes remain recorded separately from
the tooling revision so a test/package-driver update need not rebuild frameworks.

### Upstream notices

The native builder stores the original license/notice bodies verbatim in each
framework's `Resources/ThirdPartyNotices.txt`. Original `ThirdParty/` files remain
in the SDK and supply managed packages. Registry retention verifies locked archives
and the selected normal/build dependency graph; pruning is limited to verified
previously generated registry notice directories. It preserves manual upstream notices.
Maintainers explicitly provision matching public Rust notices with
`cargo.retain_runtime_notices(root, env, source=verified_public_source)`.
Normal native builds validate the retained `ThirdParty/Rust/provenance.json` and
four notice files without installed Rust documentation or downloading Rust source.

### Windows builds and inspection

The native Windows verifier and `windows.inspect_pe` read PE metadata directly
from the DLL. They check the target CPU, ordered public C names, and the complete
export address table. A named alias may share an ordinal, and unused ordinal slots
may be zero. Each populated slot must have an approved public name. Forwarded
exports and delay imports fail verification. The native verifier rejects the
separately installed VC/UCRT runtime dependencies. SDKs can supply a narrower
import policy to the portable verifier.

Windows builds use `windows.build_library` for native MSVC or explicitly selected
cargo-xwin compilation. Native selection verifies that the developer shell puts
the chosen toolchain's `cl.exe` and `link.exe` first on its search path and that
their PE machine matches the worker architecture. Multi-library SDKs select separate
`output_root` paths under their `.native-build` directory. The lock remains per checkout. SDK callbacks
supply source-policy fingerprints, portable PE/import inspection and extra package
layout. Shared code checks compiler pins, static CRT flags, source/dependency drift,
owned output membership and transactional publication. If a failed publication
cannot restore the previous artifacts, the error identifies a retained rollback
directory beside the destination. The failed build's staging cleanup does not
remove it. After resolving the filesystem error, inspect and recover that tree
before removing the rollback directory. Receipts use
`native-build.json` and record the backend; cross compilation never establishes
Windows runtime acceptance. `verify_artifact` always rechecks retained bytes and
source/tool policy; the caller supplies `inspect` for PE inspection and
`environment` for dependency-source verification. It never executes Windows code.
SDKs still own native input/rendering fixtures.

Windows receipts use schema 2; macOS framework receipts use schema 3.

Tooling can select x64 with ARM64-hosted MSVC through
`windows.native_msvc_environment("arm64", target_architecture="x64")`. The
helper selects the matching Visual Studio components and checks that the
compiler and linker are ARM64 executables from `HostARM64/x64`. It verifies the
first VC and Windows SDK libraries found on `LIB` against the selected x64
directories and sets the target-specific Cargo linker and C compiler paths.
For an SDK build that also needs .NET, use
`windows.native_environment("arm64", target_architecture="x64")`. Set
`DOTNET_BIN` to the absolute path of the installed x64 .NET 10 executable. The
helper validates the executable's PE machine and SDK version and selects the
corresponding .NET root for child processes. Pass the resulting environment and
tools to `windows.build_library(..., architecture="x64", native_host="arm64")`.
The builder rechecks the selected ARM64 host and its x64 MSVC environment,
requires the pinned ARM64 Rust compiler and installed x64 standard library, and
records their host and target triples in `toolchain`. Runtime acceptance remains
the SDK's responsibility. SDK entry points use the host architecture by default.

For hosted Windows checks, the SDK's tooling loader verifies the pinned checkout
and calls `hosted_windows` with the SDK's acceptance command. The launcher gives
the command the verified tooling checkout and revision, the .NET executable on
`PATH`, and an evidence directory under `RUNNER_TEMP`. The workflow selects the
Python, Rust and .NET versions, and the SDK's command verifies the architecture
and artifacts.

### NuGet package publication

`bindings.pack_native` also owns the shared receipt-checked NuGet packing command;
SDK entry points select the project, version, authors and exact notice/file set.
It preserves private feed selection and rejects input changes before publication.
The native producer lock covers receipt capture and verification. Staging uses
the library digest from that receipt, and the package audit checks each selected
member against its staged digest before publication.
The package and its JSON receipt are staged before replacement; a failed
replacement restores both previous files. If restoration itself fails, the error
identifies a retained rollback directory beside the package. Preserve that directory
until both accepted files have been recovered.
See [consumer verification](#consumer-verification) for independent package and runtime checks.

## Consumer verification

### Managed and C binding fixtures

`bindings` provides explicit C invocation, managed run/pack, native staging,
package byte/content audits and isolated package-consumer execution. SDKs supply
their fixture sources, selected runtime members, native overrides, dependency
feeds and test arguments. Clean NuGet consumers ignore parent-directory MSBuild
and central package settings, and discard inherited restore sources and caches.
Before building or running, the gate verifies NuGet resolved only from the
temporary feed and that every restored package archive matches a supplied file
by SHA-256. Both Windows managed and NuGet consumers check the Windows host and
Python executable with `IsWow64Process2` and the Python PE machine, then require
matching .NET 10 before restoring packages. Set `DOTNET_BIN` to the matching
executable if several architectures are installed. Windows managed acceptance
selects the verified host's runtime directory by default and stages its DLL
beside a disposable copy of the managed fixture, preserving its build output.

The standalone managed and NuGet fixtures also accept
`--windows-architecture x64` on Windows ARM64. Select the x64 .NET 10 executable
by absolute path with `DOTNET_BIN`, for example
`C:\vm-runner\tools\dotnet-x64\dotnet.exe` on a worker provisioned with both SDKs.
The tooling checks the executable's PE machine and SDK before restoring,
then uses the `win-x64` runtime for dependency resolution. The managed fixture
checks its staged DLL before building and runs the copy through the selected
x64 .NET executable. For an accepted x64 DLL in another directory, use
`--native-root`. The NuGet fixture uses its fresh local package feed and cache.
Each fixture reports successful execution under emulation after it exits.

The standalone Windows C fixture selects verified native MSVC tools and checks
the staged SDK DLL and linked fixture executable match the selected architecture.
It launches the fixture with the DLL beside it. The default target matches the
native host. To run an x64 DLL on native ARM64 Windows, pass
`--windows-architecture x64` and select an accepted x64 DLL with `--library`.
Keep the MSVC import library beside the DLL. This uses ARM64-hosted MSVC and
reports successful execution under x64 emulation.
Their Makefiles select the default fixtures; clean SwiftPM/NuGet consumers and
owned OS-effect fixtures remain SDK acceptance policy.

See [NuGet packing and recovery](#nuget-package-publication).

### External SwiftPM consumers

`consumers.check_swift` builds and runs an SDK-owned external Swift package in a
fresh temporary directory. The SDK supplies `root`, explicit installable `inputs`,
a `fixture` package directory and its `executables`. The fixture's `Package.swift`
references the staged SDK at `../sdk` and selects the SDK's products, tools version
and minimum platform; its Swift sources own the API and native assertions. Tooling
does not duplicate that policy or rebuild native artifacts. Include every required
manifest target path, native artifact, resource and retained notice in `inputs`.
Relative framework symlinks survive, while links outside the staged tree fail.
Developer tools, Rust crates and generated output roots are excluded, along with
recursive `.git`, `.build`, `.native-build`, `.swiftpm` and `__pycache__` directories.

The shared gate runs Xcode's Swift with complete concurrency checking and treats
warnings as errors by default. `--build-system swiftbuild` selects Swift Build.
`--allow-warnings` omits the global warning flag when it conflicts with a dependency's
compiler flags. The gate also uses a system-only PATH, isolated SwiftPM
config/security/cache, module cache, temporary and scratch directories, and no
inherited compiler or native-loader overrides. It preserves the caller's `HOME`
and existing `CFFIXED_USER_HOME` so Xcode-managed developer components stay outside
the disposable consumer tree.
It checks the resolved SwiftPM graph before building and rejects dependencies
outside the staged SDK, consumer and fresh dependency checkout directories.
It carries `Package.resolved` from the SDK to the consumer root and enforces those
pins. SDK/fixture `.swiftpm/configuration/mirrors.json` files are preserved; conflicting policy files
fail. `--mirror-config` supplies an explicit mirrors file. `--git-config` or the
job's explicit `GIT_CONFIG_GLOBAL` supplies a self-contained Git configuration, copied
into the temporary directory so CI's immutable dependency URL rewrites survive.
User/global Git and SwiftPM settings are otherwise isolated. All temporary state is
removed after build or runtime failure. Mirror-based execution proves standalone
source/artifact consumption, separately from remote publication or feed reachability.

### Installed Rust and C consumers

`installed_consumers.check_installed` runs independent Git Rust and copied-framework
C fixtures. SDKs select their canonical repository, public Rust packages, framework
and header. The Rust fixture owns `fixtures/rust-consumer/Cargo.toml.in` and `src/`;
the C fixture owns `fixtures/c-consumer/main.c`. Both receive an absolute empty
directory for runtime data unless the C caller supplies fixture-specific arguments.
Their code owns the public API assertions.

The producer must be a clean committed SDK tree, including no untracked files.
`--revision` defaults to that HEAD; an explicit revision must equal HEAD.

With mise-managed tools, start producer verification from the SDK root with
`mise exec --` before the SDK's check command, for example
`mise exec -- make test-installed-consumers`. This keeps the selected Rust tools
on `PATH` when the check enters the external Cargo client's directory. Installing
or using the SDK does not require this check or mise.

#### Git and registry Rust packages

The Rust gate makes a complete local mirror of the exact committed SDK, resolves
the ordinary Git dependency through a job-private file rewrite and checks its
revision in both a fresh consumer lock and Cargo metadata. Public registry
resolution may use the network; execution is locked and offline. A shallow
checkout requires a complete explicit `--git-source` repository or bundle.
An SDK that uses additional private Git packages declares each package's
canonical URL and full revision through `rust_git_dependencies` and repeats each
exact pin in the fixture manifest. Packages from the same repository share a
mirror and must use the same URL and revision. In CI, the Rust verifier selects
the exact `NAVI_CI_SOURCES` row and its positional bundle. The gate then checks
the selected bundle's checksum and advertised HEAD before Cargo starts.
Outside CI, pass `--git-dependency-source OWNER/REPO=PATH` once per
repository, using a complete checkout, mirror, or bundle. A single repository
also accepts the unqualified `--git-dependency-source PATH` form.

The fresh, file-only Git configuration contains the declared mirrors. The lock
and metadata accept Git packages only from the exact declared SDK revisions,
including workspace crates from those revisions.

The Rust consumer compiles the package identity verifier from the selected
tooling checkout with the pinned Rust compiler before creating the fresh Cargo
home. The verifier uses the Rust standard library. The launcher retains the
client's lock and metadata and checks its package manifest paths. The SDK's
fixture still owns its API and feature assertions.

For a client that needs one public registry library, declare its exact version
and explicit features through `rust_registry_dependencies` and use the same entry
in the fixture manifest. Cargo's own lock and metadata must record the selected
version from crates.io. This uses the same fresh client cache, initial fetch and
locked offline execution as the other Rust fixtures.

Omit the C framework and header for a Rust-only check. A feature fixture may
declare `rust_modes` as named pairs of enabled client features and required SDK
packages. The gate prepares one mirror, cache and lockfile, then verifies and
runs each mode with locked offline Cargo commands. The SDK's `rust_mode_check`
owns feature-specific assertions on the selected target's metadata. Optional packages
remain pinned in the common lockfile; metadata and assertion results are retained
separately for each mode.

On a native Windows worker, pass `--windows-architecture arm64` or
`--windows-architecture x64` to the SDK's installed consumer command to run the
same external Cargo fixture. This selects only the Rust consumer by default.
Prepare native Python, the SDK-pinned native Rust compiler, and Visual Studio
C++ tools with the Windows SDK. On Windows ARM64, `--windows-architecture x64`
uses ARM64-hosted MSVC and the pinned x64 Rust standard library. Cargo resolves
metadata for x64 and produces the fixture executable. The Rust verifier selects
the root package's `default-run` target or its sole binary and identifies the
unique matching Cargo build artifact. The launcher checks the executable's path
and PE machine before running it. The report records the Rust host and target
triples and adds the execution mode after the fixture passes.

#### Copied macOS C framework

The macOS C gate validates the retained native receipt, copies the selected
framework and notices, and checks the compiler's dependency trace for the copied
header and absence of producer checkout paths. It checks that the installed
payload stays intact through compilation and execution, and never rebuilds it.
SDKs with distinct framework receipts can select one tracked C source with
`c_fixture` and one tracked JSON receipt under `Artifacts` with `c_receipt`.
These are SDK-relative paths and must resolve inside their declared roots.
Defaults remain `fixtures/c-consumer/main.c` and `Artifacts/native-build.json`.

#### Retained evidence

Choose a fresh `--evidence` path outside the SDK. The report and selected Rust
sidecars refuse existing files or links. A failed run retains the report and any
lock or metadata already produced. Each resolved Rust feature mode records
whether its graph checks and runtime execution completed, even if a later mode
fails. C-only runs do not write Rust sidecars.
Temporary SDK and client state is removed.

A receipt or cached test success does not establish execution on another platform.
These gates do not test remote publication, feed reachability or private Git authentication.
