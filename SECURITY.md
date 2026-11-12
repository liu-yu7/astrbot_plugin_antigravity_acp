# Security and privacy

## Reporting

Please do not post credentials, OAuth authorization codes, tokens, raw chat histories, private server addresses, or unredacted logs in public Issues.

For a suspected vulnerability, use this repository's **Security → Advisories → Report a vulnerability** when available. Only the latest release is maintained; there is no promised response-time SLA.

## Trust boundaries

- Authentication and model requests are performed by the official local agy process.
- The plugin copies a file-backed official OAuth credential into its isolated directory without parsing its contents. The copy must be protected like the original credential.
- Chat content, images, system instructions, and tool results reach the official upstream service. This is not an offline model adapter.
- The bridge binds to `127.0.0.1` and uses per-session bearer secrets. The configured agent has no built-in terminal or filesystem tools; unauthorized native tool calls are refused.
- Approved AstrBot tools retain their own privileges. The plugin is not an operating-system sandbox or a defense against malicious local processes running as the same user.
- A configured patch checker executes as the AstrBot user. Dry-run flags do not sandbox arbitrary Python code. Use only a reviewed, trusted local script.
- Official CLI logs and AstrBot history may contain sensitive content. Log redaction is the administrator's responsibility.

## Deployment recommendations

Run AstrBot as a dedicated user, restrict access to its data directory, keep WebUI authentication enabled, and avoid exposing its dashboard directly to the Internet without appropriate access controls. Review tool capabilities before making them available to a public chat.

Back up before upgrading agy or applying binary patches. Check the actual version and patch compatibility after an upgrade. The plugin does not automatically download or apply patches.

No independent comprehensive security audit is claimed. Automated permission tests do not guarantee security against every upstream or local failure mode.
