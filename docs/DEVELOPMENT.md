# Development

## Structure

```text
main.py                 AstrBot plugin lifecycle and admin commands
_conf_schema.json       WebUI context configuration
metadata.yaml           AstrBot plugin identity
agy_acp/
  provider.py           AstrBot provider registration and response mapping
  runtime.py            ACP process/session/turn lifecycle
  process.py            ACP protocol-facing process wrapper
  jsonrpc.py            Bidirectional JSON-RPC transport
  bridge.py             Per-session authenticated loopback MCP endpoint
  history.py            History normalization and replay
  context.py            Window policy and read-only patch inspection
  preflight.py          Isolated agent/settings and credential-file preparation
  notices.py            Agent retry/failure notice handling
  safety.py             Exact default safety-prefix compatibility adaptation
tests/                  Fake CLI, policy tests, and provider smoke test
tools/                  Optional local diagnostics/configuration helpers
```

## Data flow

```text
AstrBot agent runner
  -> ProviderAntigravityACP
  -> ACP JSON-RPC over stdio
  -> official agy acp --process-per-session
  -> official model service

Model tool request
  -> per-session loopback MCP endpoint
  -> parked tool call returned to AstrBot
  -> AstrBot executes its tool
  -> result returned to the same ACP turn
```

The isolated `astrbot-bridge` agent has no built-in filesystem or terminal tools. Its settings require review of tool calls. Actual AstrBot tool execution is handed back to AstrBot; permission decisions must not be inferred from tool titles or user prompt text.

The authenticated MCP handshake can also be used as evidence for an empty capability-loading probe during a live tool-enabled turn. It is not a blanket grant to native tool execution.

## Tests

Full runtime/provider tests are designed for a POSIX environment: they launch the fake CLI through its shebang, and the provider smoke test creates a temporary symlink. Use Linux or WSL rather than assuming Windows-native process tests work.

Use an existing AstrBot environment, or create a dedicated development virtual environment:

```bash
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-dev.txt
python tests/context_tests.py
python tests/run_tests.py
python tests/provider_smoke.py
```

For context policy tests alone, the standard library is sufficient:

```bash
python tests/context_tests.py
```

The fake CLI performs no Google authentication and contacts only the temporary test MCP server. Test logs and temporary roots are not release data. No real tokens or server access are needed for CI.

[GitHub Actions 配置示例](tests.workflow.yml) 可用于在 Python 3.11–3.13 上测试上下文策略和 fake ACP，并在 Python 3.12 / AstrBot 4.29.4 上运行 provider smoke test。首次公开发布时，推送账号没有 `workflow` scope，因此该配置仅作为文档保留，仓库暂未启用 CI。具备相应权限后可将其放到 `.github/workflows/tests.yml`；不要把示例的存在当作 GitHub 测试已经通过。

Passing fake tests does not prove upstream long-context quality or other AstrBot version compatibility.

## Optional helpers

| Script | Purpose |
|---|---|
| `tools/agy_configure.py` | Add provider source/model entries to a stopped AstrBot's config; writes a timestamped backup |
| `tools/acp_probe.py` | Initialize official ACP in a separate local directory and optionally send one prompt; it does not import login credentials into that directory |
| `tools/webchat.py` | Minimal older WebUI API client; API/auth behavior may differ across AstrBot versions |

Run each with `--help` first. `webchat.py` takes a dashboard password on the command line, so prefer the real WebUI or a private local wrapper if command history/process-list exposure matters. Its MD5 fallback is legacy compatibility, not a supported password-hash login mechanism for current AstrBot. Never record real passwords in committed scripts.

## Contribution guidelines

- Keep runtime changes covered by fake CLI tests.
- Separate model context tokens from replay byte budgets.
- Re-check the upstream patcher's output and binary compatibility before changing verification rules.
- Preserve unrelated provider settings, credential isolation, and the native-tool permission boundary.
- Do not commit official CLI binaries, credentials, deployment copies, database files, raw logs, or private infrastructure information.
- New platform support should be marked tested only after executing the complete integration path there.

See [THIRD_PARTY_NOTICES.md](../THIRD_PARTY_NOTICES.md) before reusing upstream code.
