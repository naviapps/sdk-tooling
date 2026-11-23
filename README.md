# SDK tooling

Developer tools for native SDK build, packaging and consumer verification. The
entry points use standard-library Python. The macOS producer uses Rust to read
compiler dependency information. Maintainers use a Rust command to synchronize
SDK template files and the pinned tooling revision.
SDKs retain their APIs, layout specifications, fixture
programs, acceptance policy and manifests. `vm-runner` owns VM lifecycle,
credentials, source delivery and cleanup. Normal SwiftPM and NuGet consumers use
the SDK's distributed artifacts and do not install these developer tools.

## Documentation

See the [reference](docs/REFERENCE.md) for static checks, SDK policy synchronization,
native artifacts and consumer verification.

Each SDK selects its public fixtures and acceptance commands in its own
check and distribution configurations.

## Use a pinned revision

Run the CLI from the SDK checkout. It requires Python 3.13 or newer when
installed, and no third-party Python runtime packages.

Set `SDK_TOOLING_REVISION` to the full commit SHA and run:

```sh
uvx --from "git+https://github.com/naviapps/sdk-tooling.git@$SDK_TOOLING_REVISION" sdk-tooling --help
```

Place the file selections and fixtures for public API checks in
`sdk-checks.toml` at the SDK root. A minimal Swift consumer declaration uses a
SwiftPM fixture whose local SDK dependency is at `../sdk`:

```toml
[swift]
inputs = ["Package.swift", "Sources", "Tests", "LICENSE"]
executable = "ExampleConsumer"
```

`swift-consumer` stages the declared SDK inputs and
`fixtures/consumers/swift` in a temporary directory. Add `--distribution`
to verify the full resolved dependency graph. SDK-owned sections also select
the inputs for `docs`, `rust-c-consumers`, `nuget-consumer` and `check-binding`.

Native SDKs describe their artifacts in `native.toml`. Each product identifies
its C symbol namespace and minimum supported macOS version. Add the FFI path,
receipt filename and notice packages for multi-product SDKs.

```toml
[[artifacts]]
id = "sdk"
symbol_prefix = "example_"
minimum_macos = "26.0"
```

`sdk-tooling xcframework` builds the declared macOS frameworks and records
their source specification. Use `xcframework --verify` to verify a checkout's
existing frameworks, or `--product <id>` to select one declaration.
`sdk-tooling python distribution/verify_windows.py` runs an SDK-owned check
with the same selected toolkit. Module commands use `python -m` after the CLI
command, for example `sdk-tooling python -m unittest discover -s distribution/tests`.

## Developer bootstrap

Git and mise must already be available. Review this repository's
[`mise.toml`](mise.toml) before trusting its tool configuration and tasks, then run
from the repository root:

```sh
mise trust mise.toml
mise install
mise current
mise run check
```

`mise install` may download the pinned tools. `mise run check` selects the pinned
Python and Rust and tests both command paths without pip dependencies or .NET.
The template sync crate may fetch its locked public Cargo dependencies on first use.
To opt in to the clean NuGet consumer tests, use the .NET SDK pinned in
[`global.json`](global.json):

```sh
SDK_TOOLING_TEST_DOTNET=1 MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS=dotnet mise run check
```

The [CI workflow](.github/workflows/check.yml) also runs these tests on standard
macOS ARM64 and Windows x64 runners.

After changing a workflow's action dependencies, regenerate its
`actions.lock` with `gh actions-lock` and review the resolved commits.
Run `gh actions-lock --verify` to check it.

Run `make clean` in an SDK checkout to remove the local build outputs listed
by its Makefile. In this tooling checkout, use `mise run clean`. Finish local
builds and tests before cleaning. The cleaner preserves tracked artifacts,
dependency locks, shared caches and native build recovery directories. When
an output contains a native publisher lock, it must acquire that lock before
removing any output.

Public SDKs run contributor checks in their own repositories. They can call the
[Swift matrix](.github/workflows/check-swift.yml) and the
[Windows x64 check step](.github/actions/test-rust-windows/action.yml) at an
immutable SDK Tooling revision. The Swift matrix runs the SDK's `make test` and
`make test-consumer` targets on macOS 26 and 27. It runs `make check-source` on
macOS 27 by default. SDKs can select macOS 26 with `source-macos` or choose a
Make target that prepares and runs their source checks with `source-target`.
Windows checks can opt in to installing the x64 .NET SDK declared in the SDK's
`global.json`. Set `test-bindings: "true"` on the Windows check step to run the
contributor fixtures declared in [`sdk-checks.toml`](docs/REFERENCE.md#c-abi-and-net-project-checks).

The shared
[SDK distribution checks](.github/workflows/check-sdk-distribution.yml) are run
by maintainers before publishing a native SDK. Run the dispatch from the selected
SDK checkout at the intended commit on `main`:

```sh
gh workflow run check-sdk-distribution.yml --repo naviapps/sdk-tooling \
  --field sdk=configuration-storage --field "revision=$(git rev-parse HEAD)"
```

The workflow reads `sdk-distribution.toml` from a clean SDK checkout at the
requested commit and starts only the declared platform jobs. Each platform has
a `commands` array of argument arrays. For example:

```toml
[macos]
commands = [
  ["cargo", "fetch", "--locked"],
  ["make", "check-distribution"],
]
```

For a macOS SDK with `native.toml`, the job checks the committed distribution,
then builds the declared XCFrameworks once with the SDK's pinned tooling. The
workflow runs the Swift tests and the independent SwiftPM consumer once against
the regenerated binaries.
The job uploads a ZIP for each framework, the build receipts and the checksum
index as a seven-day Actions artifact. The archives are verification output.
SDKs that use SwiftPM path-based binary targets continue to supply their
frameworks in the source checkout.

For a Windows-only NuGet build, the job uploads accepted packages and their
package receipts from the SDK's hosted acceptance output when the driver
exports them. These seven-day Actions artifacts let maintainers recover the
exact packages used by the independent consumer. For example:

```sh
gh run download RUN_ID --repo naviapps/sdk-tooling --pattern 'desktop-actions-nuget-windows-*'
```

To verify one cross-platform NuGet package, add `[nuget]` with a
`pack = "distribution/pack_nuget.py"` entry. Both platform plans must be
present. The Windows plan exports its accepted native build at
`sdk-windows/win-x64`. The packer accepts repeated `--runtime` options and
reports the generated package filename to `--result`. The macOS distribution
job combines the accepted Windows build and the regenerated macOS framework
into one package and runs it with arm64 .NET and a separate x64 .NET under
Rosetta. Each macOS and Windows consumer verifies the same bytes
before and after running. Use `--field windows-on-arm=true` at dispatch to run
the x64 package with x64 .NET on GitHub's Windows on Arm runner too.

If the SDK distributes a separate Windows adapter, its Windows plan can export
the verified adapter package and receipt under `sdk-nuget-companions` in the
hosted acceptance output. Download the adapter and the combined core package
from the same successful run and put both packages in one local feed.

On macOS the dispatcher runs the commands through the SDK's mise environment.
The `sdk-tooling` command runs the pinned checkout with the dispatcher's Python
interpreter. The dispatcher also supplies this runner as `SDK_TOOLING` to Make,
including Make launched by a mise task. An explicit Make argument can override
it. Use task-scoped mise tools for release-only dependencies and pin the checksum
for downloaded assets. On Windows, `python` uses the dispatcher's interpreter
and `global.json` selects the x64 .NET SDK when present. Commands can use `{tooling}` for
the pinned tooling checkout or `{temp}` for a temporary directory shared across
commands.

The tooling revision comes from the `[tasks.sdk-tooling]` task in the SDK's
`mise.toml`:

```toml
[tasks.sdk-tooling]
run = "uvx --from git+https://github.com/naviapps/sdk-tooling.git@<full-sha> sdk-tooling"
```

The dispatcher keeps its own checkout so workflow changes can ship independently.
The hosted job sets `GITHUB_SHA` to the selected SDK commit and uses Git to select
the source files. It clears inherited VM Runner snapshot settings and checkout
credentials. The Windows fixture launcher supplies `DOTNET_BIN` and
`VM_RUNNER_ARTIFACTS_DIR` from the runner's x64 tool and temporary directory.
To reproduce checks for an older SDK commit, use the dispatcher and producer
revisions from the original workflow run and follow the SDK's instructions at
that commit.

### SDK checkouts

Review the SDK's `mise.toml`, select full Xcode, then run its check target:

```sh
export DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer
mise trust mise.toml
mise install
mise current
mise exec -- /bin/sh -c 'python3 --version; xcrun --find swift-format'
mise exec -- "$(/usr/bin/xcrun --find make)" check
```

Adjust `DEVELOPER_DIR` for your installation; formatting and compilation must use
the same Xcode. The explicit Make path uses that Xcode. `mise exec --` keeps the
selected tools on `PATH`, including in external consumers. The template's
`activate_aggressive` setting also keeps them ahead of system tools in Make recipes;
older checkouts need `MISE_ACTIVATE_AGGRESSIVE=1` before `mise exec`.
Check Python against `mise.toml` and the formatter with `xcrun swift-format --version`.
This tooling repository uses `mise run check` and has no Makefile.

SDKs that declare `native.toml` use the pinned CLI from `mise.toml` for local
distribution checks and SDK-owned Python fixtures. A prepared VM can use a
checksummed source bundle and local Git mirror supplied by its host.

### Tool ownership

| Scope               | Tools and version authority                                                                                                                                                                                                                                             |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Repository checks   | Each repository's `mise.toml` selects Python, formatters and linters. `global.json` pins the .NET SDK where applicable. `uv` supplies the PyPI tool backend.                                                                                                            |
| Rust producers      | `rust-toolchain.toml` selects Rust, rustfmt and Clippy. Install only the standard-library targets needed by the job: both Darwin targets for universal frameworks, the appropriate MSVC target for Windows, or the WebAssembly target for browser bindings.             |
| macOS SDKs          | Select full Xcode for Swift, SwiftPM, swift-format, Clang, `xcodebuild` and Mach-O inspection. Use the macOS signing tools for framework checks. Do not install separate copies of these tools through mise. The pinned C formatter is independent of Xcode's compiler. |
| Native Windows SDKs | Use target-native Python, Rust, .NET 10 and Visual Studio C++ tools with the Windows SDK. The Windows driver selects the Visual Studio environment and checks the host and target architectures.                                                                        |
| Optional jobs       | Web bindings and release packaging have separate prerequisites. Windows DLLs are built and exercised on Windows.                                                                                                                                                        |

## SDK repository layout

Keep each language's build conventions and place tests with their owning interface:

- `Sources/<Target>` and `Tests/<TargetTests>` contain Swift targets and tests.
  Document API contracts on the public declarations and link usage guides from the SDK README.
- `crates/<role>` contains independent Rust packages. Each package owns its
  `src`, `tests`, `examples` and `benches`; C ABI callers live in the FFI package's
  `tests/c` directory.
- `bindings/dotnet/src/<Project>` and `bindings/dotnet/tests/<Project>` contain
  managed implementations and their tests. User-facing examples belong in
  `bindings/dotnet/examples`.
- Optional `bindings/web` contains the independent browser adapter and its tests.
- `fixtures/consumers/<language>` contains independent package and public interface
  consumers. The verification driver can generate a .NET project from the
  consumer source here. Each consumer selects its inputs explicitly.
- `fixtures/acceptance/{macos,windows,browser}` contains disposable hosts that
  exercise real OS or browser behavior.
- `distribution` contains SDK-specific packaging and verification code and tests
  for that code. The SDK selects the commands in `sdk-distribution.toml`.
- `Artifacts` contains consumer-required native binaries and provenance;
  `ThirdParty` retains original upstream notices. These are distributed inputs.
- `docs` contains cross-language guides. Reusable build mechanisms stay in this
  repository, and VM lifecycle and isolation stay in `vm-runner`.

In this repository, `sdk_tooling` is the installed Python package, `crates`
contains its Rust helpers and the template synchronizer, and `ci` contains the
GitHub distribution dispatcher and its source checkout entry point.

Do not add empty directories for unsupported languages or split small modules
merely to match a larger SDK. Keep generated output such as `.native-build`,
`target`, `bin` and `obj` ignored. In particular, a live build lock is generated
runtime state, not a tracked source file; never unlink an active lock when cleaning
or changing its tracking status.

## Ownership boundaries

Do not add product policy, provider calls, SDK-to-SDK discovery, host settings,
VM operations or release authorization here. Keep repository files that are
necessary for standalone SDK use; consolidate their maintenance and mechanisms.

## License

SDK Tooling is available under the [MIT License](LICENSE).
