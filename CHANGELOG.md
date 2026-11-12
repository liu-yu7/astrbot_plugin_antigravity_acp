# Changelog

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
