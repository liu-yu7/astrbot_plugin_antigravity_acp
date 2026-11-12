"""Startup preparation: locate the official ``agy`` binary and lay out the
isolated Antigravity home that AstrBot's ACP sessions use.

The isolated home keeps the server user's own Antigravity rules, skills and
MCP servers out of chat sessions and installs the ``astrbot-bridge`` agent,
which removes every built-in tool (shell, file edits) so the model can act
only through the tools AstrBot itself exposes. A chat bot must never hand a
coding agent's terminal to whoever is talking to it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

BRIDGE_AGENT = "astrbot-bridge"
#: Minimum official CLI version supporting ``agy acp --process-per-session``.
MIN_AGY_VERSION = (1, 4, 12)
TOKEN_FILE = "antigravity-oauth-token"
APP_DATA_DIR = "agy"

_VERSION_RE = re.compile(r"^(?:antigravity(?:\s+cli)?\s+)?v?(\d+)\.(\d+)\.(\d+)\b", re.IGNORECASE)
_PROXY_KEYS = ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy")


class PreflightError(RuntimeError):
    """The Antigravity CLI is missing, too old, or its home cannot be prepared."""


def resolve_agy_binary(configured: str = "") -> str:
    """Locate the ``agy`` executable: configured path, ``~/.local/bin/agy``, then ``PATH``."""
    configured = (configured or "").strip()
    if configured:
        path = os.path.expanduser(configured)
        if not os.path.isabs(path):
            raise PreflightError(f"agy_path 必须是绝对路径，当前为: {configured}")
        if not os.path.exists(path):
            raise PreflightError(f"在 {path} 找不到 agy 可执行文件")
        return path
    exe = "agy.exe" if sys.platform == "win32" else "agy"
    candidates: list[str] = []
    if sys.platform == "win32":
        local = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        candidates.append(os.path.join(local, "agy", "bin", exe))
    candidates.append(str(Path.home() / ".local" / "bin" / exe))
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    found = shutil.which("agy")
    if found:
        return found
    raise PreflightError(
        "未找到 Antigravity CLI (agy)。请先安装: curl -fsSL https://antigravity.google/cli/install.sh | bash，"
        "并运行一次 `agy acp --login` 完成登录"
    )


async def read_version(binary: str, env: dict[str, str] | None = None) -> str:
    """Read the agy version (diagnostic only)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            binary,
            "--version",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        out, err = await asyncio.wait_for(proc.communicate(), 30)
        text = (out.decode("utf-8", "replace").strip() or err.decode("utf-8", "replace").strip()).splitlines()
        for line in text:
            if "logging before google.Init" not in line and line.strip():
                return line.strip()
    except Exception:  # noqa: BLE001 - the version is diagnostic only
        pass
    return "unknown"


def assert_supported_version(version: str, binary: str) -> None:
    """Reject outdated ``agy`` binaries that predate ACP multi-session support."""
    match = _VERSION_RE.match(version.strip())
    if match is None:
        return
    found = tuple(int(part) for part in match.groups())
    if found < MIN_AGY_VERSION:
        need = ".".join(str(part) for part in MIN_AGY_VERSION)
        raise PreflightError(f"{binary} 的版本 {version.strip()} 过旧（需要 >= {need}），请运行 `agy update` 升级")


def token_sources(user_home: str | None = None) -> list[Path]:
    """Credential files a terminal ``agy`` / ``agy acp --login`` writes on a file-backed host."""
    home = Path(user_home) if user_home else Path.home()
    return [
        home / ".gemini" / "antigravity-cli" / TOKEN_FILE,
        home / ".gemini" / "antigravity" / TOKEN_FILE,
    ]


def sync_oauth_token(home: str, user_home: str | None = None) -> bool:
    """Seed or refresh the isolated credential file from the user's CLI home.

    Returns True when the isolated home holds a credential afterwards.
    """
    app_data = Path(home) / APP_DATA_DIR
    dst = app_data / TOKEN_FILE
    newest: Path | None = None
    newest_mtime = -1.0
    for candidate in token_sources(user_home):
        try:
            stat = candidate.stat()
        except OSError:
            continue
        if stat.st_size > 0 and stat.st_mtime > newest_mtime:
            newest, newest_mtime = candidate, stat.st_mtime
    try:
        dst_stat = dst.stat()
    except OSError:
        dst_stat = None
    if newest is None:
        return dst_stat is not None and dst_stat.st_size > 0
    if dst_stat is not None and dst_stat.st_size > 0 and dst_stat.st_mtime >= newest_mtime:
        return True
    app_data.mkdir(parents=True, exist_ok=True)
    shutil.copy2(newest, dst)
    try:
        os.chmod(dst, 0o600)
    except OSError:
        pass
    return True


def has_credential(home: str, user_home: str | None = None) -> bool:
    """Whether a stored login exists for this home (isolated copy or the user's own)."""
    dst = Path(home) / APP_DATA_DIR / TOKEN_FILE
    for candidate in [dst, *token_sources(user_home)]:
        try:
            if candidate.stat().st_size > 0:
                return True
        except OSError:
            continue
    return False


def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content:
            return
    except OSError:
        pass
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    try:
        os.chmod(temporary, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _read_json(path: Path) -> dict:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except (OSError, ValueError):
        return {}


def _seed_settings(path: Path, context_window: str | None = None) -> None:
    settings = _read_json(path)
    if context_window is not None:
        settings["contextWindow"] = context_window
    # Every tool call must ask the client for review; the client (this plugin)
    # only ever approves calls to AstrBot's own MCP bridge.
    settings["toolPermission"] = "request-review"
    settings["agentMode"] = ""
    _write_if_changed(path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")


def prepare_home(home: str, user_home: str | None = None, *, context_window: str | None = None) -> str:
    """Write the isolated Antigravity home: bridge agent, settings, credential copy.

    ``home`` is passed to agy as ``--gemini-dir``. Returns the empty workspace
    directory used as every session's cwd.
    """
    root = Path(home)
    agent_root = root / "config" / "plugins" / BRIDGE_AGENT
    _write_if_changed(agent_root / "plugin.json", json.dumps({"name": BRIDGE_AGENT}, indent=2) + "\n")
    _write_if_changed(
        agent_root / "agents" / f"{BRIDGE_AGENT}.md",
        "\n".join(
            [
                "---",
                f"name: {BRIDGE_AGENT}",
                'description: "AstrBot chat model bridge: no built-in tools, AstrBot tools only"',
                "tools: []",
                "excludeDefaultComponents: true",
                "mainAgent: true",
                "---",
                "Follow the appended system instructions exactly. Act only through the tools you are given.",
                "",
            ]
        ),
    )
    app_data = root / APP_DATA_DIR
    (app_data / "conversations").mkdir(parents=True, exist_ok=True)
    (root / "antigravity" / "conversations").mkdir(parents=True, exist_ok=True)
    sync_oauth_token(home, user_home)
    _seed_settings(app_data / "settings.json", context_window)
    _seed_settings(root / "antigravity-cli" / "settings.json", context_window)
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    return str(workspace)


def build_env(home: str, proxy: str = "", extra_env: dict | None = None) -> dict[str, str]:
    """Environment of every agy process started by this plugin."""
    env = dict(os.environ)
    for key, value in (extra_env or {}).items():
        if isinstance(key, str) and key and value is not None:
            env[key] = str(value)
    proxy = (proxy or "").strip()
    if proxy:
        for key in _PROXY_KEYS:
            env[key] = proxy
        # The MCP tool bridge lives on loopback and must never be proxied.
        no_proxy = [item for item in (env.get("NO_PROXY") or env.get("no_proxy") or "").split(",") if item.strip()]
        for host in ("127.0.0.1", "localhost", "::1"):
            if host not in no_proxy:
                no_proxy.append(host)
        env["NO_PROXY"] = env["no_proxy"] = ",".join(no_proxy)
    if sys.platform.startswith("linux"):
        # Without a session bus the keyring implementation cannot reach a desktop
        # secret service and falls back to the credential file, which is the only
        # store a systemd service and an SSH login can both read.
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={os.path.join(home, '.no-dbus-session')}"
    return env


def login_command(binary: str, proxy: str = "") -> str:
    """Copy-pasteable terminal command that signs in on this machine."""
    prefix = ""
    if sys.platform.startswith("linux"):
        prefix += "DBUS_SESSION_BUS_ADDRESS=unix:path=/nonexistent "
    if proxy:
        prefix += f"HTTPS_PROXY={proxy} HTTP_PROXY={proxy} "
    return f"{prefix}{binary} acp --login"
