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

```sh
uvx --from git+https://github.com/naviapps/sdk-tooling.git@ --help <full-sha >sdk-tooling
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
`fixtures/swift-consumer` in a temporary directory. Add `--distribution`
to verify the full resolved dependency graph. The `docs` and
`installed-consumers` commands use their corresponding SDK-owned sections.
Tooling receives the SDK's own API names, source paths and test clients from
that description.

Native SDKs describe their artifacts in `native.toml`. Each product identifies
its C symbol namespace and minimum supported macOS version. Add the FFI path,
receipt filename and notice packages for multi-product SDKs.

```toml
[[artifacts]]
id = "sdk"
symbol_prefix = "example_"
minimum_macos = "26.0"
```

`sdk-tooling native` builds the declared frameworks and records the source
specification in their receipts. Use `native --verify` to verify a checkout's
existing frameworks, or `--product <id>` to select one declaration.
`sdk-tooling python fixtures/distribution/example.py` runs an SDK-owned fixture
with the same selected toolkit. Module commands use `python -m` after the CLI
command, for example `sdk-tooling python -m unittest discover -s fixtures/distribution/tests`.

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
The clean NuGet consumer test is skipped unless `NAVI_TEST_DOTNET=1` requires the
native .NET SDK, as the CI workflow does. GitHub Actions uses standard macOS
Arm64 and Windows x64 runners for these checks. See the
[workflow](.github/workflows/check.yml) for the runtime versions.

After changing a workflow's action dependencies, regenerate its
`actions.lock` with `gh actions-lock` and review the resolved commits.
Run `gh actions-lock --verify` to check it.

Public SDKs run their contributor checks in their own repositories. The shared
[SDK distribution checks](.github/workflows/check-sdk-distribution.yml) are run
by maintainers before publishing a native SDK. Select the SDK and the full
commit SHA from its `main` branch:

```sh
gh workflow run check-sdk-distribution.yml --repo naviapps/sdk-tooling \
  --field sdk=configuration-storage --field revision="$SDK_COMMIT"
```

The jobs read `sdk-distribution.toml` from a clean SDK checkout at the requested
commit. Each platform has a `commands` array of argument arrays. A project can
declare either platform or both. For example:

```toml
[macos]
commands = [
  ["cargo", "fetch", "--locked"],
  ["make", "check-distribution"],
]
```

On macOS the dispatcher runs the commands through the SDK's mise environment
when it has a `mise.toml`. On Windows the command name `python` uses the runner's
pinned interpreter, and a `[tools].dotnet` pin selects the x64 .NET SDK. Commands can use `{tooling}` for the pinned tooling checkout
or `{temp}` for a temporary directory shared across commands. An optional
`[[macos.archives]]` or `[[windows.archives]]` table provides an HTTPS `url`,
a verified `sha256` and a directory named by `bin` inside the tar archive to
prepend to `PATH`.

The tooling revision comes from the `[tasks.sdk-tooling]` task in the SDK's
`mise.toml`:

```toml
[tasks.sdk-tooling]
run = "uvx --from git+https://github.com/naviapps/sdk-tooling.git@<full-sha> sdk-tooling"
```

Repositories without the task can declare an exact revision in
`tools/tooling-sources.json`. The dispatcher keeps its own checkout so workflow
changes can ship independently. SDK commands receive the exact selected SDK
commit and run after the setup steps remove their checkout credentials.

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

SDKs that declare `native.toml` use the fixed CLI from `mise.toml` for local
distribution checks and SDK-owned Python fixtures. Older SDK revisions that use
their own source loader expect `NAVI_SDK_TOOLING_SOURCE` to select a clean
checkout at the corresponding pin. A prepared VM can use a checksummed source
bundle and local Git mirror supplied by its host.

### Tool ownership

| Scope               | Tools and version authority                                                                                                                                                                                                                                                   |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Repository checks   | The repository's `mise.toml` selects Python and the applicable formatters and linters. The shared Rust SDK template also selects .NET for managed checks; the Swift-only SDK does not require it. `uv` supplies the PyPI tool backend, not an application dependency.         |
| Rust producers      | `rust-toolchain.toml` selects Rust, rustfmt and Clippy. Install only the standard-library targets needed by the job: both Darwin targets for universal frameworks, the appropriate MSVC target for Windows, or the WebAssembly target for browser bindings.                   |
| macOS SDKs          | Select full Xcode for Swift, SwiftPM, swift-format, DocC, Clang, `xcodebuild` and Mach-O inspection. Use the macOS signing tools for framework checks. Do not install separate copies of these tools through mise. The pinned C formatter is independent of Xcode's compiler. |
| Native Windows SDKs | Use target-native Python, Rust, .NET 10 and Visual Studio C++ tools with the Windows SDK. The Windows driver selects the Visual Studio environment and checks the host and target architectures.                                                                              |
| Optional jobs       | Web bindings, Windows cross compilation and release packaging have separate prerequisites. The explicit cross driver requires `cargo-xwin` and LLVM `clang-cl`; it does not replace native Windows acceptance.                                                                |

### Prepared CI tools

On a prepared macOS worker, `python3 -B -m sdk_tooling quality prepared-mise`
(or `python3 -B tools/quality.py prepared-mise` in an SDK) verifies the worker
manifest and pinned launcher checksum/version, then prints the absolute mise path.
The manifest digest comes from controller-verified `NAVI_CI_PREPARED_MANIFEST_SHA256`,
falling back to the pinned legacy digest when absent. A prepared worker can
use this launcher for static tools.

## SDK repository layout

Keep each language's build conventions and place tests with their owning interface:

- `Sources/<Target>` and `Tests/<TargetTests>` contain Swift targets and tests.
  Target-specific API documentation stays in the target's DocC catalog.
- `crates/<role>` contains independent Rust packages. Each package owns its
  `src`, `tests`, `examples` and `benches`; C ABI callers live in the FFI package's
  `tests/c` directory.
- `bindings/dotnet/src/<Project>` and `bindings/dotnet/tests/<Project>` contain
  explicit managed projects. Package consumer source without a project may occupy
  a named test directory when its project is generated by the verification driver.
  User-facing examples belong in `bindings/dotnet/examples`.
- Optional `bindings/web` contains the independent browser adapter and its tests.
- `fixtures` contains shared cross-language data or disposable native host
  applications, rather than production APIs. Each consumer selects its inputs
  explicitly; fixtures must not rely on an incidental working directory.
- `Artifacts` contains consumer-required native binaries and provenance;
  `ThirdParty` retains original upstream notices. These are distributed inputs.
- `tools` contains thin SDK policy and entry points; `docs` contains cross-language
  guides. Reusable build mechanisms stay in this repository, and VM/runner
  management stays in `vm-runner`.

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
