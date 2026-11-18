# Changelog

## 0.2.2 — 2026-11-18

### Fixed

- Project MCP tool enums that agy 1.5.8 cannot convert to Gemini schemas: numeric `enum: [0]` otherwise becomes an empty protobuf enum string and rejects the entire request with `INVALID_ARGUMENT (400)`. Keep integer/number types; numeric singletons use exact minimum/maximum bounds. Leave ordinary nonempty string enums unchanged.
- Keep every original projected enum contract in the model's system instructions, including booleans, mixed/nullable values and nested schemas. Host tool metadata and runtime validators remain unchanged; this does not disable tools, permissions or memory retrieval.
- Close the runtime async stream before returning a tool-call or final response, and close it deterministically on cancellation/errors. Completed requests no longer retain conversation locks or keyless sessions while awaiting async-generator finalization.
- Preserve explicit `RESOURCE_EXHAUSTED (code 429)` retry notices as actionable errors and stop resubmitting the same rejected request. Hold split notices until their bodies arrive; do not misclassify quoted error codes as failures.
- Restate only the exact stock AstrBot 4.29.4 Linux host-tools paragraph for ACP compatibility. Keep file/execution permissions, POSIX syntax, session control actions, real line-feed input and the prohibition on detached long-running commands. Preserve surrounding safety policy, skill inventory/rules, persona and tool contracts; leave custom/other-OS templates unchanged.

### Tests

- 20 runtime regressions, 8 dependency-free schema tests, 14 context tests, and the AstrBot provider smoke test pass on Linux arm64 / Python 3.12 / AstrBot 4.29.4.
- Cover numeric zero, nested schemas, defaults/examples, compaction, original allowed-value preservation, native ToolSet conversion and MCP session reuse.
- Reproduce the old adapter's delayed lock release; cover deterministic closure, keyless cleanup, split 429 notices, early failure without replay and recovery on unrelated requests.
- Verify exact host-template matching against AstrBot and preservation of every host constraint.
- Real agy 1.5.8 Gemini validation includes a numeric-enum old/new comparison, two full WebChat turns with fallback disabled, and an echo-tool roundtrip preserving integer zero. Full-window 512K loading and other platforms remain untested.

## 0.2.1 — 2026-11-14

### Fixed

- Disable native agy background auto-updates for managed ACP processes with `AGY_CLI_DISABLE_AUTO_UPDATE=true`, so verified context patches are not silently overwritten. Explicit administrator upgrades remain available and require patch re-verification.
- Support agy 1.5.8 explicit MCP tool names while rejecting conflicting identities and native-tool impersonation. Remove display-only `toolSummary` arguments unless the AstrBot tool schema declares them.
- Treat split `Agent execution terminated due to error.` notices as upstream failures rather than successful assistant text; do not blindly retry these unspecified failures.
- Adapt only the exact known AstrBot default Safe Mode system-prompt prefix to equivalent safety wording for ACP compatibility. Preserve every safety constraint, custom suffix, persona, tool contract, and user message; leave unknown/custom templates unchanged. This addresses the reproduced template-specific 429, not every upstream rate-limit or quota error.

### Tests

- Add regression coverage for managed environment overrides, agy 1.5.8 MCP metadata and tool arguments, split terminal failures, and safety-policy preservation.
- Check the compatibility template against the real AstrBot 4.29.4 constant in the provider smoke test.

## 0.2.0 — 2026-11-12

### Added

- WebUI plugin configuration for Default / 256K / 512K / 900K context windows, defaulting to 512K.
- Configurable replay byte budget; automatic 512K replay budget is 1,024,000 bytes.
- Read-only checks of all six required context / user-input patch targets using a trusted local script.
- Patch-check cache invalidation when the agy executable or checker changes.
- Context, replay budget, patch state, and current-process configuration in `/agy status`.
- Context policy tests and runtime/provider synchronization tests.

### Changed

- Synchronize this adapter's persisted model context limits and live provider configuration copies.
- Replace the ACP process when a verified context policy changes.
- Use atomic writes for isolated settings while preserving unrelated settings.
- Reject unverified explicit windows above 256K rather than silently advertising them as usable.

### Public repository preparation

- Added installation, context, troubleshooting, development, and security documentation.
- Added MIT licensing with upstream attribution, dependency manifests, and a GitHub Actions configuration example (not enabled in the initial public release).
- Added repository metadata and exclusions for credentials, caches, logs, and runtime data.
- Runtime behavior remains the deployed 0.2.0 implementation.

## 0.1.0 — 2026-11-11

- Initial AstrBot provider adapter backed by the official Antigravity CLI over ACP.
- Streaming text / reasoning, image input, session reuse and history reconstruction.
- Per-session loopback MCP bridge, parked tool calls, result relay, cancellation, and permission checks.
- Administrator login, status, model-list, reset, and restart commands.
- Fake ACP end-to-end tests and an AstrBot provider smoke test.
