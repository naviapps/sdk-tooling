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

The SDK's `.swiftlint.yml` points to the [shared rules](../.swiftlint.yml)
at the root of SDK Tooling's `main` branch. The
[former rules path](../crates/template-sync/templates/.swiftlint.yml)
forwards to the same rules for existing SDK checkouts.
Direct SwiftLint commands and the quality check
use that parent configuration. Keep `strict: true` in the SDK config because
SwiftLint reads strict mode from the local file. SwiftLint tries to fetch the
rules on each run and can use a cached copy when the server is unreachable.
A cold cache needs network access. Add `/.swiftlint/RemoteConfigCache/` to the
SDK's `.gitignore` to keep the cache out of Git. For a direct offline run, pass
`--config` with the path to the shared rules in a local SDK Tooling checkout.

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

#### Shell and workflows

For repositories with shell scripts, configure the shfmt Wasm plugin in dprint.
Bash syntax checks and ShellCheck validate the same scripts.

Jactionlint checks selected workflows and composite actions with its correctness
profile. It runs ShellCheck for inline shell and Ruff's `F` rules for inline
Python when the repository pins them for the current platform. The quality gate
passes the verified executable paths and the selected `.github/jactionlint.yaml`
(or `.yml`) for runner labels, including in Gitless snapshots. Undeclared or
out-of-scope integrations are disabled. A missing or mismatched tool fails the
check.

#### Markdown and structured data

Dprint formats owned Markdown, JSON, TOML and YAML with version-pinned Wasm
plugins from the npm registry. Declare fixture exclusions in `.dprint.json` so
the editor, direct dprint commands and the shared quality gate use the same policy.

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
`mise.toml`. Synchronization updates that commit SHA and the existing
literal job reference to `naviapps/sdk-tooling/.github/workflows/check-sdk.yml`
in `.github/workflows/check.yml`. It preserves other workflow contents and
leaves SDKs with their own workflows free to use the CLI pin on its own. Each
SDK owns its dprint configuration and Git attributes.

Rust SDKs receive updates for their existing EditorConfig and Rust toolchain
files. SDK-specific EditorConfig sections are retained. Use `--quality` to
update existing Ruff configuration and set existing `.swiftlint.yml` files to
reference the shared SwiftLint rules.

The tool validates the selected checkout paths before updating files and attempts
to restore earlier updates if one fails. `--check` requires an immutable CLI revision and the same
revision in the shared workflow when present. Add `--revision FULL_COMMIT_SHA`
to require every selected checkout to use that revision.

## Shared source workflow

Reference `.github/workflows/check-sdk.yml` at a reviewed SDK Tooling commit
from the SDK repository's check workflow. The SDK owns triggers, its Make
targets and acceptance fixtures. The called workflow selects the checks and
configures GitHub-hosted runners.

The default runs the same Swift tests and standalone consumer on macOS 26
and 27. Source checks run on macOS 27. Override `source-macos` to select 26 or
`source-target` to select an SDK-specific Make target. Supply
`package-documentation` when a Markdown file is a packaging input.

Set `windows` to `rust` for x64 workspace tests, `bindings` to also test the
managed bindings declared in `[checks]`, or `managed` to install .NET for an
SDK-specific source check. Set `windows-fixture` to run the checkout's
`.github/check-windows.ps1` after shared checks. That fixture always runs in
`managed` mode. Use `platform` to select `all`, `macos`, or `windows`.

The optional `native-aot` input runs the SDK's manual win-x64 package check.
Declare the Rust, .NET and cbindgen pins in the SDK. The launcher provisions
cbindgen, and the SDK's `distribution.verify_windows` module prepares its
headers and runs the `--native-aot` acceptance.

## Distribution CLI

Install SDK Tooling at the [reviewed revision](../README.md#use-a-pinned-revision).
The CLI accepts a checkout and an optional checkout-relative manifest path.
The default manifest is `sdk-tooling.toml`. It contains the SDK's `[native]`,
`[checks]` and `[distribution]` declarations. Include the sections the SDK
uses. Historical checkouts can use `native.toml`, `sdk-checks.toml` and
`sdk-distribution.toml` with their pinned producer revision.

```toml
[distribution.macos]
commands = [
  ["xcodebuild", "-version"],
  ["sdk-tooling", "xcframework", "--verify"],
  ["sdk-tooling", "swift-consumer", "--distribution"],
]

[distribution.windows]
commands = [["python", "distribution/check_windows.py", "{temp}"]]

[distribution.nuget]
pack = "distribution/pack_nuget.py"
```

Keep only the platforms and operations the SDK supports. The host tables are
`macos`, `windows`, and `linux`. Each command is an argument array executed
from the SDK root. `sdk-tooling` uses the already selected CLI. `python` runs
a checkout-owned fixture with that CLI's interpreter. `{temp}` expands to a
fresh directory shared by the commands in one check. `{tooling}` expands to
the selected CLI's package root. Accepted arguments retain leading and trailing spaces.

```sh
sdk-tooling distribution plan --root /path/to/sdk
sdk-tooling distribution check --root /path/to/sdk
sdk-tooling distribution pack-nuget --root /path/to/sdk --runtime osx-arm64
```

Use `--manifest release.toml` to select a different file. `plan` reads the
configuration and prints JSON. `check` executes the current host's commands.
Review the checkout before executing commands from it and provision its native
toolchains on the host. These commands can run from a local checkout with any
remote or without a Git remote.

`pack-nuget` runs the SDK's packer with the selected runtime arguments.
A packer receiving `--result` writes the resulting package filenames to a
new file. Use `sdk-tooling nuget-consumer PACKAGE.nupkg` from the SDK root
to exercise its configured consumer with the resulting package. For native
frameworks, use `sdk-tooling xcframework` to build or `sdk-tooling
xcframework-archive --output PATH` to archive verified results.

For example, after checkout and toolchain setup, a GitHub Actions job can invoke
the same host check. Set `SDK_TOOLING_REVISION` to a reviewed full commit SHA:

```yaml
- name: Verify distribution
  run: |
    uvx --from "git+https://github.com/naviapps/sdk-tooling.git@$SDK_TOOLING_REVISION" \
      sdk-tooling distribution check
```

## Hosted distribution

Start from the [workflow command](../README.md#sdk-distribution). The dispatcher
checks out the requested commit from `main`, reads the SDK's immutable
`[tasks.sdk-tooling]` pin in `mise.toml`, and runs the platform commands in
`[distribution]` in `sdk-tooling.toml` as argument arrays:

```toml
[distribution.macos]
commands = [
  ["cargo", "fetch", "--locked"],
  ["make", "check-distribution"],
]
```

The macOS dispatcher uses the SDK's mise environment and passes `SDK_TOOLING`
to Make. An explicit Make argument can override it. The `sdk-tooling` command
uses the dispatcher's Python interpreter with the pinned checkout. On Windows,
`python` also uses that interpreter, and `global.json` selects the x64 .NET SDK
when present. Commands can substitute `{tooling}` for the pinned checkout and
`{temp}` for a shared temporary directory. Use task-scoped dependencies and
pin checksums for downloaded assets.

The SDK receives its own commit in `GITHUB_SHA`. The dispatcher clears inherited
checkout credentials and VM Runner source settings. The Windows fixture launcher
sets `DOTNET_BIN` and `VM_RUNNER_ARTIFACTS_DIR` for the installed SDK and
temporary output. Reproduce a previous run with both its dispatcher revision and
its producer pin.

Manual distribution checks on `main` can reuse the Windows x64 cbindgen
executable for matching Rust and cbindgen pins. The dispatcher validates its
architecture and version before saving it, then runs the SDK's build and tests
in the fresh checkout. Older tooling revisions install the generator for that
run.

### Framework and NuGet artifacts

For macOS SDKs with `[native]`, the workflow checks the committed inputs,
regenerates the XCFrameworks, and runs the Swift tests and standalone SwiftPM
consumer against the regenerated binaries. It uploads
[framework ZIPs and receipts](#archive-regenerated-xcframeworks). SwiftPM path
targets still load their artifacts from the SDK checkout.

When a plan builds separate Windows NuGet packages, it can export the accepted
packages and receipts. Download them from the successful run with
`gh run download RUN_ID --repo naviapps/sdk-tooling --pattern '*-nuget-windows-*'`.
Workflow artifacts are retained for seven days.

A combined NuGet plan declares both platforms, `global.json`, `[native]`,
and a packer such as `pack = "distribution/pack_nuget.py"` under `[distribution.nuget]`.
The Windows plan exports `sdk-windows` and the workflow restores its tree under
`.native-build/windows` on macOS. The packer accepts repeated `--runtime` values
and reports its primary package filename to `--result`, followed by any
dependency package filenames separated by LF. Keep those packages and their
`.nupkg.json` receipts under `.native-build/packages`, covering `osx-arm64`,
`osx-x64` and `win-x64`.

The workflow checks the same package set on macOS ARM64, Rosetta x64 and
Windows x64, with optional [Windows on Arm coverage](../README.md#windows-on-arm).
A downloaded combined artifact has its primary package at the root and
dependencies under `dependencies/`. Place them in one local feed.
When the Windows plan exports a separate adapter under `sdk-nuget-companions`,
use the adapter and combined core from the same successful run. The
[package consumer](#installed-nuget-consumers) verifies the supplied archives
and the workflow rechecks package and receipt bytes after execution.

## Native artifacts and provenance

### macOS builder specifications

Declare each product in `sdk-tooling.toml`:

```toml
[[native.artifacts]]
id = "sdk"
symbol_prefix = "example_"
minimum_macos = "26.0"
```

Run `sdk-tooling xcframework` to build or add `--verify` to check the retained
output. Use `--product <id>` for one declaration. Multi-product SDKs also declare
their FFI paths, receipt filenames and shared retained notice inputs.

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

See [recover previous artifacts](#recover-previous-artifacts) if rollback fails.

### Generated C headers and module maps

Pin `cargo:cbindgen` in the SDK's `mise.toml` and declare the Swift module in the
FFI crate's `Cargo.toml`:

```toml
[package.metadata.c-bindings]
module = "CExample"
generated = true
```

Ignore the exact generated source-side header and module map files. The
XCFramework build and C fixtures generate these inputs before use.
`c-headers check` compares freshly generated portable headers and module maps
with the bundled copies and prepares the ignored files for C compilation.
`xcframework --verify` performs the same comparison before checking the
framework and receipt.
Run `mise run sdk-tooling -- c-headers generate` to prepare the declarations
without rebuilding the framework. Add `--product ID` to select one product
from `[native]`.

The command uses the FFI crate's `cbindgen.toml` and `src/lib.rs`. For a
configuration with `parse_deps = true` under `[parse]`, it selects the named
Cargo crate using locked dependency metadata. A product with
`symbol_prefix = "example_"` produces
`include/example.h`. A separate `cbindgen-windows.toml` processes
`src/windows.rs` into `include/windows/example_windows.h`. The C module map
references the portable header. Set `umbrella = false` to emit a direct header
declaration.

Keep ABI versions, types and safety contracts with the Rust exports. SDK-owned
C or native packaging scripts that bypass the shared commands call
`sdk_tooling.c_headers.prepare_generated(root)` before reading their inputs.
The hosted Windows launcher provisions cbindgen from the SDK's exact pin for
these native checks. Consumers load the retained SwiftPM XCFramework and use
its bundled declarations.

### Production source receipts

A unified manifest contributes a digest of its parsed `[native]` section to
the build inputs. Changes to fixtures or distribution commands are outside this
digest. Native production sources, dependencies, generator code and artifact
bytes retain their independent provenance checks.

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
The default graph includes both macOS targets and Windows x64. Explicit Windows
builds select the Windows target from the build, including on ARM64 hosts.
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
owned output membership and transactional publication. Receipts use
`native-build.json` and record the backend. Windows runtime acceptance is a
separate check. `verify_artifact` always rechecks retained bytes and
source/tool policy; the caller supplies `inspect` for PE inspection and
`environment` for dependency-source verification. It never executes Windows code.
SDKs still own native input/rendering fixtures. See
[recover previous artifacts](#recover-previous-artifacts) if rollback fails.

SDKs with one native Windows x64 product can use
`windows_delivery.X64Acceptance` for the host toolchain, locked Rust checks,
receipt-verified DLL staging and C fixture execution. The SDK selects its
fixtures, managed build properties and package acceptance rules.

When an SDK builds a native package for a pinned dependency, use
`adapters.pinned_tooling(dependency, checkout)` to load the build modules
from that dependency's declared Tooling commit. Use the same revision to
verify its receipt, including when building and packaging on different hosts.
To use a trusted fork, pass `tooling_repository="owner/name"`. The dependency's
mise task must match that repository and select a full commit SHA.

Windows receipts use schema 2; macOS framework receipts use schema 3.

For a local x64 build on Windows 11 ARM64, install ARM64 Python, the SDK-pinned
ARM64 Rust toolchain with the `x86_64-pc-windows-msvc` target, Visual Studio C++
components for both ARM64 and x64, and the Windows SDK. The SDK's Windows driver
builds x64 binaries and runs x64 managed consumers under emulation.

Select ARM64-hosted MSVC for the x64 target with
`windows.native_msvc_environment("arm64", target_architecture="x64")`.
The helper selects the matching Visual Studio components and checks that
the compiler and linker are ARM64 executables from `HostARM64/x64`. It verifies
the first VC and Windows SDK libraries on `LIB` against the selected x64
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

The launcher provisions the pinned cbindgen and generates opted-in headers
before starting the driver. If the driver already generates them, write
`sdk-tooling windows --driver-prepares-headers` before the script or `-m` module.
The launcher still provisions cbindgen.

### NuGet package publication

`nuget.pack_native` builds receipt-checked NuGet packages with the standard
MSBuild Pack target. SDK entry points select the project, runtimes and exact
notice/file set. The project owns package metadata and layout. The packer
compares its emitted dependency requirements to the evaluated project.
When an entry point supplies explicit package sources, the packer resolves
dependencies from those sources with a fresh staging cache. It rejects input
changes before publication.
The `managedInputs` receipt includes checkout-local MSBuild, NuGet and Roslyn
directory settings. The packer discovers standard settings for the managed
project and analyzer settings for explicitly listed linked C# source files.
Declare custom imports and other additional inputs in `input_files`.
MSBuild writes new packages with a fixed ZIP timestamp. Identical native and
managed inputs under the same packaging toolchain can reproduce the unsigned
archive bytes. Separately rebuilding native inputs can change identifiers,
debug metadata or signatures.
The native producer lock covers receipt capture and verification. Staging checks
the library digest from that receipt. On macOS the packer extracts the selected
runtime's slice from the staged universal library, ad-hoc signs the standalone
dylib and verifies its architecture and signature.
The package receipt records both the verified native source digest and the digest
of each delivered member, and the package audit checks those delivered bytes.
When one SDK builds multiple Windows products, its packer can select the
product's accepted output directory with `windows_output_root` and the exact
verified inventory with `windows_source_inputs`.
The packer stages the package and its JSON receipt before replacing the accepted pair.
See [consumer verification](#consumer-verification) for independent package and runtime checks.

### Recover previous artifacts

An unsuccessful replacement restores the previous accepted outputs. If rollback
also fails, the error identifies a retained rollback directory beside the
destination. The build's temporary staging cleanup preserves that directory.

Stop publishers for the affected output and resolve the reported filesystem
error. Recover a native output directory together with its build receipt, or a
NuGet package with its matching `.nupkg.json` receipt. Verify the recovered
outputs against the SDK revision that produced them. For NuGet, check the archive
digest against its receipt. Remove the rollback directory after verification.

## Consumer verification

Declare each SDK's fixture inputs in `[checks]` in `sdk-tooling.toml`. Run SDK-owned scripts
with `sdk-tooling python SCRIPT` or modules with `sdk-tooling python -m MODULE`.

### Contributor workflows

SDKs can call [check selection](../.github/workflows/plan-checks.yml) once per
workflow to run Markdown formatting and local link checks for documentation-only
changes. Set `package-documentation` for Markdown that is read by a package
build. Source changes, manual runs and an unavailable diff use the full checks.
Use a distinct concurrency group for each push. A documentation-only push must
leave source verification for an earlier push running. Pull request runs can
supersede each other because they compare the current merge tree with its base.

Run the relevant checks locally while iterating. For shared changes, verify the
affected SDKs with the updated Tooling checkout before pushing the related
commits together. Use distribution checks for changes to package contents,
packaging or installation.

The [shared source workflow](#shared-source-workflow) calls the
[Swift matrix](../.github/workflows/check-swift.yml) and the
[Windows x64 check step](../.github/actions/test-rust-windows/action.yml).

### C ABI and .NET project checks

For Windows contributor checks, `test-windows-bindings` builds the SDK's
workspace `cdylib` packages for `x86_64-pc-windows-msvc`. It reads the DLL paths
from Cargo's compiler artifact messages and stages those DLLs beside each
.NET fixture in its temporary output directory. It runs the x64 apphost when
the build produces one, or uses the selected x64 .NET to run the assembly.
Declare the packages and projects in `sdk-tooling.toml`:

```toml
[checks.windows_bindings]
packages = ["example-ffi"]
projects = ["bindings/dotnet/tests/Example.Tests/Example.Tests.csproj"]
```

If `projects` is omitted, the command uses `[bindings].dotnet_project`. Optional
`arguments` are passed to each fixture. On a prepared Windows host, run
`sdk-tooling test-windows-bindings` with a tooling revision that provides the
command. These source fixtures run independently of
the release package and native build receipts.

`c_compiler` invokes Clang or MSVC for C callers. `dotnet` runs, builds and
packs managed projects. `artifacts.stage_native` stages verified native bytes.
`check-binding c` and `check-binding dotnet` run the SDK's C ABI and managed
project fixtures. The `[checks.bindings]` section selects their
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
`[checks.nuget]` section. `consumers.nuget` runs each fixture
against a private temporary feed. The restored packages must match the supplied
archives by SHA-256. These consumer projects ignore parent-directory MSBuild
and central package settings and discard inherited restore sources and caches.
They also ignore inherited .NET startup hooks and runtime lookup overrides.
Every archive passed with `--dependency` must appear in the restored graph.

See [NuGet packing and recovery](#nuget-package-publication).

### External SwiftPM consumers

Run `mise run sdk-tooling -- swift-consumer` from the SDK checkout. The `[checks.swift]`
section declares `inputs`, `executable` or `executables`, and
an optional `fixture` path that defaults to `fixtures/consumers/swift`. Include
every required target path, native artifact, resource and retained notice in
`inputs`. The fixture's `Package.swift` selects products from the staged SDK at
`../sdk`. The fixture owns its platform requirements and API assertions.

```toml
[checks.swift]
inputs = ["Package.swift", "Sources", "Tests", "LICENSE"]
executable = "ExampleConsumer"
```

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
`verify_dependencies = true` in `[checks.swift]`. Full verification builds the entire
fixture, rejects resolved dependencies outside the fresh install, and enforces
the SDK's `Package.resolved` at the consumer root when present. It retains SDK and
fixture `.swiftpm/configuration/mirrors.json` files and rejects conflicting policy.
`--mirror-config` and `--git-config` also select full verification with an
explicit policy file. Full verification can use the job's `GIT_CONFIG_GLOBAL`
for dependency URL rewrites. Both checks remove their temporary state when done.

### Installed Rust and C consumers

`rust-c-consumers` runs independent Git Rust and copied-framework C fixtures from
the SDK's `[checks.rust_c]` declaration. SDKs select their canonical repository, public Rust
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
An SDK that uses additional Git packages lists their names in
`rust_git_dependencies` and pins their canonical URLs and full revisions in
the Cargo workspace. In the fixture manifest, repeat the URL and supply the
full revision or use `rev = "@GIT_REVISION_example-sdk@"` for the `example-sdk`
dependency. Tooling fills the token from the declared workspace pin.
Packages from the same repository share a mirror and must use the same URL
and revision. In CI, the Rust verifier selects the exact
`VM_RUNNER_SOURCE_MANIFEST` row and its positional bundle. The gate then
checks the selected bundle's checksum and advertised HEAD before Cargo starts.
Outside CI, pass `--git-dependency-source OWNER/REPO=PATH` once per
repository, using a complete checkout, mirror, or bundle. A single repository
also accepts the unqualified `--git-dependency-source PATH` form.

On Windows, if Cargo reports that a checkout path is too long, set `TEMP`
and `TMP` to a short path to an existing writable directory for the check.

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
