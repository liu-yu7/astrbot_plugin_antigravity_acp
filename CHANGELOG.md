# Changelog

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
