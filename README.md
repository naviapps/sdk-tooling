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

Each SDK's README, Makefile and `tools` entry points select its supported interfaces,
fixtures and acceptance commands.

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

SDK entry points load an immutable revision from `tools/tooling-sources.json`.
`templates/_tooling.py` validates source/cache bytes against that Git tree before
import. The first developer use fetches the selected revision; subsequent uses
reuse the verified cache. `NAVI_SDK_TOOLING_SOURCE` selects an explicit clean
checkout of that same revision. `NAVI_SDK_TOOLING_CACHE` selects the cache root;
`NAVI_SDK_TOOLING_OFFLINE=1` rejects a missing cache instead of using the network.
GitHub-hosted SDK workflows resolve public tooling at the same immutable
revision. A prepared VM can use a checksummed source bundle and local Git
mirror supplied by its host.

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
