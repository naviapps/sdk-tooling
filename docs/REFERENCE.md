# SDK tooling reference

[Developer setup](../README.md#developer-bootstrap).

## Quality checks

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

Direct CI workspaces without Git metadata can supply `VM_RUNNER_SOURCE_FILES` and
`VM_RUNNER_SOURCE_FILES_SHA256` together. The normalized absolute path must name the ordinary
workspace-root `.navi-ci-source-files.json`; its SHA-256 binds the exact UTF-8 JSON bytes, bounded
to 16 MiB, with shape `{"version":1,"files":["sorted/relative/name.py"]}`.
Parent directory aliases resolving to that same workspace are accepted, including
macOS `/tmp` and `/private/tmp`; the manifest itself must not be a symlink.
The nonempty, sorted, unique POSIX list (at most 100,000 names) records CI's
captured tracked and unignored working files, including dirty edits. It applies the same directory and
linked-input policy as Git selection. Missing selected inputs, invalid metadata
or a mismatched hash fail without falling back to Git. The metadata file itself
is transport data and cannot be selected as source. Both variables absent retain
normal Git discovery, including skipped deleted files. Existing pinned consumers
must adopt a tooling revision with this support before using direct CI selection.

SDK fixtures can use `sdk_tooling.vm_environment` to read `VM_RUNNER_*` settings.
Use `artifacts_dir` and `source_manifest` for absolute paths, `source_files` for
the source inventory and digest, and `source_identity` for the captured commit
and workspace fingerprint. `set_value` replaces or removes both names in a child
environment. Readers accept the names used by older pinned VM Runner versions
and fail if both names are present with different values.

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

Jactionlint checks selected workflows and composite actions with its correctness
profile. It runs ShellCheck for inline shell and Ruff's `F` rules for inline
Python when the repository pins them for the current platform. The quality gate
passes the verified executable paths and the selected `.github/jactionlint.yaml`
(or `.yml`) for runner labels, including in Gitless snapshots. Undeclared or
out-of-scope integrations are disabled. A missing or mismatched tool fails the
check.

#### Markdown and structured data

Dprint formats owned Markdown, JSON, TOML and YAML with checksummed Wasm plugins,
without requiring Node in SDK repositories.

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
mise exec -- cargo run --quiet --locked -p sdk-template-sync -- \
  --revision FULL_COMMIT_SHA -- /absolute/path/to/sdk
mise exec -- cargo run --quiet --locked -p sdk-template-sync -- \
  --check -- /absolute/path/to/sdk
```

Put tool options before the `--` that introduces the SDK paths. Relative paths
resolve from the working directory. To use a prepared Cargo cache without
network access, add `--offline` to the Cargo options.

Select a Rust or Swift SDK with a pinned `[tasks.sdk-tooling]` command in
`mise.toml`. Synchronization replaces its literal commit SHA and preserves
the rest of the mise configuration. Each SDK owns its dprint configuration
and Git attributes.

Rust SDKs receive updates for their existing EditorConfig and Rust toolchain
files. SDK-specific EditorConfig sections are retained. Use `--quality` to
update existing Ruff, SwiftLint and C formatting policies. Use
`--swift-format` to adopt or update the shared Swift formatting file.

The tool validates all selected checkout paths before replacing files and
writes each updated pin after its templates. A check without `--revision`
accepts any canonical immutable pin. Add `--revision FULL_COMMIT_SHA` to
require every selected checkout to use that revision.

## Native artifacts and provenance

### macOS builder specifications

SDK-owned native specifications describe the shared `ffi.FfiProduct`.
Cargo manifests and module maps supply package, library and module names.
`xcframework.XcframeworkBuilder` uses the declaration to build the macOS
artifact. The SDK selects its C symbol prefix, minimum macOS version and
component receipt. SDKs with several native libraries declare a shared
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

The macOS and Windows producers record Cargo `compiler-artifact` messages in
`cargoBuild`, grouped by requested target triple. Each entry identifies its
verified package, Cargo target kind and name, host or target compilation, and
enabled features. Build scripts and procedural macros compile on the host.
Different feature sets for the same package stay separate. Paths and Cargo's
cache freshness flag are omitted so the record describes the same build on
another checkout. The selected linked library must appear in the build output.
The `dependencies` section separately records the locked dependency graph and
source bytes. Verification checks each recorded package and feature against that
graph. It does not rerun the compilation to reproduce the recorded feature sets.

Each public ABI getter has a version macro in its C header. The producer links
a small C caller to the selected library and checks the returned version against
that header. macOS runs the caller for the host architecture and inspects both
slices of the universal framework. Windows runs it for the DLL's target
architecture, including x64 emulation on ARM64. The receipt records the expected
versions and the architecture that ran the caller.

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

### Archive regenerated XCFrameworks

The macOS distribution workflow uses the CLI to archive verified framework
outputs and their receipts. Each ZIP includes the named XCFramework at its root
and retains its internal framework symlinks and executable permissions.
`xcframeworks.json` records the SDK revision and the size and SHA-256 digest of
each ZIP and receipt. Download the verification output from the completed run:

```sh
gh run download RUN_ID --repo naviapps/sdk-tooling --pattern '*-xcframeworks-*'
```

For a local run, rebuild the native artifacts with the SDK's pinned tooling and
give the archive command a new destination outside the SDK checkout:

```sh
sdk-tooling xcframework-archive --output /absolute/path/to/archives
```

### Upstream notices

The native builder stores the original license/notice bodies verbatim in each
framework's `Resources/ThirdPartyNotices.txt`. Original `ThirdParty/` files remain
in the SDK and supply managed packages. Registry retention verifies locked archives
and the selected normal/build dependency graph. It only prunes generated registry
notice directories whose files match the hashes in `registry-notices.json`.
Use the tooling pinned by an older SDK revision to verify its historical index.
Manual upstream notices stay in place.
For initial retention, pass a verified public Rust release tree or prepared
source manifest directory to
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

Windows builds use a shared `FfiProduct` and `windows.build_library` for native
MSVC compilation. Native selection verifies that the developer shell puts
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
`native-build.json` and record the backend. Windows runtime acceptance is a
separate check. `verify_artifact` always rechecks retained bytes and
source/tool policy; the caller supplies `inspect` for PE inspection and
`environment` for dependency-source verification. It never executes Windows code.
SDKs still own native input/rendering fixtures.

SDKs with one native Windows x64 product can use
`windows_delivery.X64Acceptance` for the host toolchain, locked Rust checks,
receipt-verified DLL staging and C fixture execution. The SDK selects its
fixtures, managed build properties and package acceptance rules.

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

`nuget.pack_native` builds receipt-checked NuGet packages with the standard
MSBuild Pack target. SDK entry points select the project, runtimes and exact
notice/file set. The project owns package metadata and layout. The packer
compares its emitted dependency requirements to the evaluated project.
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

### C ABI and .NET project checks

For Windows contributor checks, `test-windows-bindings` builds the SDK's
workspace `cdylib` packages for `x86_64-pc-windows-msvc`. It reads the DLL paths
from Cargo's compiler artifact messages and runs each .NET fixture with those
DLLs in its temporary output directory. Declare the packages and projects in
`sdk-checks.toml`:

```toml
[windows_bindings]
packages = ["example-ffi"]
projects = ["bindings/dotnet/tests/Example.Tests/Example.Tests.csproj"]
```

If `projects` is omitted, the command uses `[bindings].dotnet_project`. On a
prepared Windows host, run `sdk-tooling test-windows-bindings` with a tooling
revision that provides the command. These source fixtures run independently of
the release package and native build receipts.

`c_compiler` invokes Clang or MSVC for C callers. `dotnet` runs, builds and
packs managed projects. `artifacts.stage_native` stages verified native bytes.
`check-binding c` and `check-binding dotnet` run the SDK's C ABI and managed
project fixtures. The `[bindings]` section in `sdk-checks.toml` selects their
inputs. Both Windows managed and NuGet consumers check the Windows host and
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
The SDK selects its default fixtures and native acceptance policy.

### Installed NuGet consumers

`nuget-consumer` selects a package ID and independent fixtures from the
`[nuget]` section in `sdk-checks.toml`. `consumers.nuget` runs each fixture
against a private temporary feed. The restored packages must match the supplied
archives by SHA-256. These consumer projects ignore parent-directory MSBuild
and central package settings and discard inherited restore sources and caches.

See [NuGet packing and recovery](#nuget-package-publication).

### External SwiftPM consumers

Run `mise run sdk-tooling -- swift-consumer` from the SDK checkout. The `[swift]`
section in `sdk-checks.toml` declares `inputs`, `executable` or `executables`, and
an optional `fixture` path that defaults to `fixtures/consumers/swift`. Include
every required target path, native artifact, resource and retained notice in
`inputs`. The fixture's `Package.swift` selects products from the staged SDK at
`../sdk`. The fixture owns its platform requirements and API assertions.
Relative framework symlinks are retained. Links outside the staged tree fail.
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

The contributor check runs the declared executables with the fixture manifest
and `Sources`. Select full dependency verification with `--distribution` or set
`verify_dependencies = true` in `[swift]`. Full verification builds the entire
fixture, rejects resolved dependencies outside the fresh install, and enforces
the SDK's `Package.resolved` at the consumer root when present. It retains SDK and
fixture `.swiftpm/configuration/mirrors.json` files and rejects conflicting policy.
`--mirror-config` and `--git-config` also select full verification with an
explicit policy file. Full verification can use the job's `GIT_CONFIG_GLOBAL`
for dependency URL rewrites. Both checks remove their temporary state when done.

### Installed Rust and C consumers

`rust-c-consumers` runs independent Git Rust and copied-framework C fixtures from
the SDK's `[rust_c]` declaration. SDKs select their canonical repository, public Rust
packages, framework and header. The Rust fixture owns
`fixtures/consumers/rust/Cargo.toml.in` and `src/`.
The C fixture owns `fixtures/consumers/c/main.c`. Both receive an absolute empty
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
the exact `VM_RUNNER_SOURCE_MANIFEST` row and its positional bundle. The gate then checks
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

The macOS C gate validates the retained native receipt and copies the selected
framework and notices. It compiles at the minimum macOS version recorded in both
the receipt and framework, then checks the compiler's dependency trace for the
copied header and absence of producer checkout paths. The installed payload must
stay intact through compilation and execution. The gate uses the copied binary.
SDKs with distinct framework receipts can select one tracked C source with
`c_fixture` and one tracked JSON receipt under `Artifacts` with `c_receipt`.
These are SDK-relative paths and must resolve inside their declared roots.
The defaults are `fixtures/consumers/c/main.c` and `Artifacts/native-build.json`.

#### Retained evidence

Choose a fresh `--evidence` path outside the SDK. The report and selected Rust
sidecars refuse existing files or links. A failed run retains the report and any
lock or metadata already produced. Each resolved Rust feature mode records
whether its graph checks and runtime execution completed, even if a later mode
fails. C-only runs do not write Rust sidecars.
Temporary SDK and client state is removed.

A receipt or cached test success does not establish execution on another platform.
These gates do not test remote publication, feed reachability or private Git authentication.
