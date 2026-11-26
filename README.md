# SDK tooling

Developer tools for native SDK build, packaging and consumer verification.
The CLI uses standard-library Python with Rust helpers for compiler provenance
and template synchronization. SwiftPM and NuGet consumers install the SDK's
distributed artifacts directly.

## Documentation

- [Check source and synchronize SDK policy][quality checks]
- [Configure distribution and find its artifacts][hosted distribution]
- [Build and verify SDK packages on Windows on Arm](#windows-on-arm)
- [Verify installed SwiftPM, NuGet, Rust and C consumers][consumer verification]
- [Recover artifacts after a failed replacement][artifact recovery]

## Use a pinned revision

Run the CLI from the SDK checkout. It requires Python 3.13 or newer when
installed, and no third-party Python runtime packages.

Set `SDK_TOOLING_REVISION` to the full commit SHA and run:

```sh
uvx --from "git+https://github.com/naviapps/sdk-tooling.git@$SDK_TOOLING_REVISION" sdk-tooling --help
```

The CLI can run in a checkout owned by any organization. Declare [native products][native products],
[consumer checks][consumer verification] and [distribution commands][distribution CLI] in `sdk-tooling.toml`.

## Developer bootstrap

These commands require Git, mise and a clone of the
[SDK Tooling repository](https://github.com/naviapps/sdk-tooling). Review
`mise.toml` before trusting its tool configuration and tasks, then run from the
clone root:

```sh
mise trust mise.toml
mise install
mise current
mise run check
```

`mise install` may download the pinned tools. `mise run check` selects the pinned
Python and Rust and tests both command paths without pip dependencies or .NET.
The template sync crate may fetch its locked public Cargo dependencies on first use.
To opt in to the clean NuGet consumer tests, use the .NET SDK pinned in `global.json`:

```sh
SDK_TOOLING_TEST_DOTNET=1 MISE_IDIOMATIC_VERSION_FILE_ENABLE_TOOLS=rust,dotnet mise run check
```

The [CI workflow][checks workflow] also runs these tests on standard
macOS ARM64 and Windows x64 runners.

After changing a workflow's action dependencies, regenerate its
`actions.lock` with `gh actions-lock` and review the resolved commits.
Run `gh actions-lock --verify` to check it.

Run `make clean` in an SDK checkout to remove the local build outputs listed
in its Makefile. In this tooling checkout, use `mise run clean`. Finish local
builds and tests before cleaning. The cleaner preserves native build recovery
directories and acquires native publisher locks before removing output.

## SDK distribution

Run the [distribution workflow][distribution workflow]
from the SDK checkout at the intended commit on `main`:

```sh
gh workflow run check-sdk-distribution.yml --repo naviapps/sdk-tooling \
  --field sdk=configuration-storage --field "revision=$(git rev-parse HEAD)"
```

See [hosted distribution][hosted distribution] for the plan,
runtime checks and downloadable artifacts.

## SDK checkouts

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
The SDK's `make check` uses the pinned shared CLI for quality and standalone
consumer checks. Use the SDK's language test commands for a focused test run.
The [shared source workflow][shared source workflow] runs the
same source and consumer checks on GitHub-hosted runners.

SDKs that declare `[native]` use the pinned CLI from `mise.toml` for local
distribution checks and SDK-owned Python fixtures. A prepared VM can use a
checksummed source bundle and local Git mirror supplied by its host.

### Tool ownership

| Scope               | Tools and version authority                                                                                                                                                                                                                                 |
| ------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Repository checks   | `mise.toml` selects Python, formatters and linters. `global.json` pins .NET where applicable. `uv` runs the pinned CLI.                                                                                                                                     |
| Rust producers      | `rust-toolchain.toml` selects Rust, rustfmt and Clippy. Install only the standard-library targets needed by the job: both Darwin targets for universal frameworks, the appropriate MSVC target for Windows, or the WebAssembly target for browser bindings. |
| macOS SDKs          | Select full Xcode for Swift, SwiftPM, swift-format, Clang, `xcodebuild` and Mach-O inspection. Use the macOS signing tools for framework checks. Do not install separate copies of these tools through mise.                                                |
| Native Windows SDKs | Use Python and Rust for the host architecture, Visual Studio C++ tools with the Windows SDK, and the x64 .NET SDK pinned in `global.json` for x64 SDKs.                                                                                                     |
| Optional jobs       | Web bindings and release packaging have separate prerequisites. Windows DLLs are built and exercised on Windows.                                                                                                                                            |

### Windows on Arm

For local x64 builds on Windows 11 ARM64, follow the [shared Windows setup]
and run the `mise run sdk-tooling -- python` command documented by the SDK.
The `sdk-tooling windows` launcher uses the hosted runner's `RUNNER_TEMP`.

For a combined NuGet package in hosted CI, add `--field windows-on-arm=true`
to the [distribution command](#sdk-distribution) to test the same `win-x64`
package on a Windows ARM64 runner.

## SDK repository layout

Keep each language's build conventions and place tests with their owning interface:

- `Sources/<Target>` and `Tests/<TargetTests>` contain Swift targets and tests.
  Document API contracts on the public declarations and link usage guides from the SDK README.
- `crates/<role>` contains independent Rust packages. Each package owns its
  `src`, `tests`, `examples` and `benches`; C ABI callers live in the FFI package's
  `tests/c` directory.
- `bindings/dotnet/src/<Project>` and `bindings/dotnet/tests/<Project>` contain
  managed implementations and their tests. Document API details in C# XML
  comments. Use the .NET guide for package installation and runnable examples.
  A guide included in NuGet should link to repository resources by absolute URL.
- Optional `bindings/web` contains the independent browser adapter and its tests.
- `fixtures/consumers/<language>` contains independent package and public interface
  consumers. The verification driver can generate a .NET project from the
  consumer source here. Each consumer selects its inputs explicitly.
- `fixtures/acceptance/{macos,windows,browser}` contains disposable hosts that
  exercise real OS or browser behavior.
- `distribution` contains SDK-specific packaging and verification code and tests
  for that code. The SDK selects the commands in `[distribution]`.
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

SDKs own their APIs, dependency discovery, fixtures and acceptance policy.
VM Runner owns host settings, credentials, source delivery, isolation and
cleanup. Hosts own product policy, provider calls and release authorization.

## License

SDK Tooling is available under the [MIT License](LICENSE).

[quality checks]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#quality-checks
[hosted distribution]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#hosted-distribution
[consumer verification]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#consumer-verification
[artifact recovery]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#recover-previous-artifacts
[native products]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#macos-builder-specifications
[distribution CLI]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#distribution-cli
[checks workflow]: https://github.com/naviapps/sdk-tooling/blob/main/.github/workflows/check.yml
[distribution workflow]: https://github.com/naviapps/sdk-tooling/blob/main/.github/workflows/check-sdk-distribution.yml
[shared source workflow]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#shared-source-workflow
[shared Windows setup]: https://github.com/naviapps/sdk-tooling/blob/main/docs/REFERENCE.md#windows-builds-and-inspection
