# SDK tooling

Own reusable build, artifact, package and consumer-execution mechanisms. SDKs own their APIs, native layouts, fixture programs, dependency policy and acceptance semantics. Hosts own product policy; vm-runner owns VM, credentials, isolation, transport and cleanup.

Keep Python entry points standard-library-only and use immutable tooling revisions. The maintainer template sync command uses the locked Rust tool under `tools/template-sync`. Normal SwiftPM/NuGet consumers must not need these developer tools. Validate actual source and artifact bytes; never reuse a test success as a cache result. Preserve explicit target/ABI/minimum-OS, source/notice provenance, safe publication and failure recovery.

Use Git on main and commit coherent, verified changes. The parent owns publication, CI/service and native VM operations. Do not change user/QA state, credentials, global tool configuration or sealed evidence. Use login:false and English maintained docs/comments.
