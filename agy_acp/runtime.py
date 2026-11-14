"""Process, session and turn management for the Antigravity ACP route.

One ACP prompt turn can span many model calls: the agent calls an AstrBot tool
through the MCP bridge, the call parks, and the running step ends by handing
the tool call to AstrBot. AstrBot runs the tool through its own pipeline and
asks for the next step with the result, which resolves the parked call and
resumes the same ACP turn.
"""

from __future__ import annotations

import asyncio
import collections
import os
import re
import secrets
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator

from . import history as H
from .bridge import McpBridge, error_result
from .context import ContextInspector, ContextPlan, ContextSettings, resolve_plan
from .jsonrpc import JsonRpcClosedError, JsonRpcRemoteError
from .notices import WARNING_PREFIX, classify_agent_notice
from .preflight import (
    APP_DATA_DIR,
    BRIDGE_AGENT,
    PreflightError,
    assert_supported_version,
    build_env,
    has_credential,
    login_command,
    prepare_home,
    read_version,
    resolve_agy_binary,
    sync_oauth_token,
)
from .process import AcpProcess, SessionListener

CLIENT_VERSION = "0.2.0"
CONTINUE_PROMPT = "Continue exactly where your previous output stopped. Do not repeat any earlier text."
#: Wait after switching model/effort on a brand-new session before its first prompt.
COLD_SWITCH_SETTLE = 0.08
#: Models offered when the catalog cannot be read (not signed in yet).
FALLBACK_MODELS = ["gemini-3.8-flash-medium", "gemini-3.8-flash-high", "gemini-4-argon-medium", "gemini-4-argon-high"]

_EFFORT_ALIASES = {
    "low": ["low"],
    "medium": ["medium", "med"],
    "med": ["medium"],
    "high": ["high"],
    "xhigh": ["xhigh", "extra", "extrahigh"],
    "extra": ["extra", "xhigh", "extrahigh"],
    "max": ["max", "maximum"],
}
_EFFORT_SUFFIX = re.compile(r"^(.*?)[\s\-_]*\(?\s*(low|medium|high|xhigh|extra|max)\s*\)?$", re.IGNORECASE)
_LOGIN_URL = re.compile(r"https://accounts\.google\.com/\S+")


class AgyError(Exception):
    """A failure of the Antigravity route with a stable code."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable


@dataclass
class RuntimeConfig:
    """Settings of one runtime (one ``agy acp`` front process)."""

    home: str
    agy_path: str = ""
    proxy: str = ""
    extra_env: dict = field(default_factory=dict)
    client_name: str = "astrbot"
    default_effort: str = ""
    expose_tools: bool = True
    max_live_sessions: int = 6
    idle_session_ttl: float = 900.0
    request_timeout: float = 300.0
    relay_after: float = 150.0
    park_settle: float = 0.25
    park_ttl: float = 900.0
    max_continuations: int = 3
    context_settings: ContextSettings = field(default_factory=ContextSettings)
    replay_images: int = 4
    startup_timeout: float = 120.0

    def signature(self) -> tuple:
        return (self.home, self.agy_path, self.proxy, tuple(sorted((self.extra_env or {}).items())), self.client_name)


@dataclass
class ChatRequest:
    """One model step requested by AstrBot."""

    key: str | None
    system: str
    messages: list[H.Msg]
    tools: list[dict]
    model: str
    effort: str
    resolve_image: H.ImageResolver


@dataclass
class ParkedCall:
    """One tool call the agent issued and AstrBot has to run."""

    id: str
    name: str
    arguments: dict
    future: asyncio.Future


class EventQueue:
    """Single-consumer queue that can be peeked and waited on with a timeout."""

    def __init__(self) -> None:
        self._items: collections.deque = collections.deque()
        self._event = asyncio.Event()

    def push(self, item: tuple) -> None:
        self._items.append(item)
        self._event.set()

    def peek(self) -> tuple | None:
        return self._items[0] if self._items else None

    def shift(self) -> tuple:
        item = self._items.popleft()
        if not self._items:
            self._event.clear()
        return item

    async def wait(self, timeout: float | None = None) -> bool:
        if self._items:
            return True
        try:
            await asyncio.wait_for(self._event.wait(), timeout)
        except asyncio.TimeoutError:
            return False
        return bool(self._items)


class Turn:
    """One running ACP prompt turn."""

    def __init__(self, tag: str) -> None:
        self.tag = tag
        self.events = EventQueue()
        #: Calls handed to AstrBot whose results have not arrived yet.
        self.pending: dict[str, ParkedCall] = {}
        #: Every call the agent issued during this turn.
        self.calls: dict[str, ParkedCall] = {}
        self.tool_steps = 0
        self.continuations = 0
        self.finished = False
        #: A parked call was answered with a still-running notice and the agent has not resumed waiting.
        self.relaying = False
        self.produced = False
        self.retry_notices = 0
        #: Terminal transport or overload failure reported as message text by agy.
        self.agent_failure: AgyError | None = None
        self.prompt_task: asyncio.Task | None = None
        self._notice_hold = ""
        self._next_call = 0

    def park(self, name: str, arguments: dict) -> ParkedCall:
        """Register one agent tool call."""
        held = self.recover_notice_hold()
        if held and not self.relaying:
            self.events.push(("text", held))
        self.agent_failure = None
        self._next_call += 1
        call = ParkedCall(
            id=f"agy_{self.tag}_{self._next_call}",
            name=name,
            arguments=arguments,
            future=asyncio.get_running_loop().create_future(),
        )
        self.calls[call.id] = call
        self.events.push(("park", call))
        return call

    def on_update(self, update: dict) -> None:
        """Route one ACP session update into the event queue."""
        content = update.get("content")
        text = ""
        if isinstance(content, dict) and content.get("type") == "text":
            text = str(content.get("text") or "")
        kind = update.get("sessionUpdate")
        if kind == "agent_message_chunk" and text:
            self._consume_message(text)
        elif kind == "agent_thought_chunk":
            held = self.recover_notice_hold()
            if held and not self.relaying:
                self.events.push(("text", held))
            if text and not self.relaying:
                self.events.push(("thought", text))

    def _consume_message(self, text: str) -> None:
        """Keep a chunk out of the chat while it matches or may still complete an agy status notice."""
        if not self._notice_hold and self.agent_failure is not None and not text.strip():
            return
        combined = self._notice_hold + text
        verdict = classify_agent_notice(combined, False)
        if verdict.kind == "hold":
            self._notice_hold = combined
            return
        self._notice_hold = ""
        if verdict.kind == "drop":
            self.retry_notices += 1
            return
        if verdict.kind == "error":
            self.retry_notices += 1
            self.agent_failure = AgyError(
                verdict.code, verdict.message, retryable=verdict.code in ("TRANSPORT", "RATE_LIMIT")
            )
            return
        if combined.strip():
            self.agent_failure = None
        if not self.relaying:
            self.events.push(("text", combined))

    def recover_notice_hold(self) -> str:
        """Release a held prefix once the message stream moves on."""
        if not self._notice_hold:
            return ""
        held = self._notice_hold
        self._notice_hold = ""
        verdict = classify_agent_notice(held, True)
        if verdict.kind == "drop":
            self.retry_notices += 1
            return ""
        if verdict.kind == "error":
            self.retry_notices += 1
            self.agent_failure = AgyError(
                verdict.code, verdict.message, retryable=verdict.code in ("TRANSPORT", "RATE_LIMIT")
            )
            return ""
        if self.agent_failure is not None and WARNING_PREFIX.match(held):
            return ""
        if held.strip():
            self.agent_failure = None
        return "" if self.relaying else held

    def abandon(self, reason: str) -> None:
        """Abandon every parked call with an error result."""
        for call in self.calls.values():
            if not call.future.done():
                call.future.set_result(error_result(reason))
        self.pending.clear()


class Conversation:
    """One AstrBot conversation bound to at most one live ACP session."""

    def __init__(self, key: str | None) -> None:
        self.key = key
        self.ephemeral = key is None
        self.lock = asyncio.Lock()
        self.session_id: str | None = None
        self.process: AcpProcess | None = None
        self.config_options: list[dict] = []
        self.system_text: str | None = None
        self.tools: list[dict] = []
        self.tool_names: set[str] = set()
        self.tools_digest = ""
        self.tenant_key: str | None = None
        self.tenant_secret: str | None = None
        #: Tool calls the agent reported via session/update: the only trusted permission source.
        self.reported_tool_calls: dict[str, dict] = {}
        #: Fingerprints of the persisted messages this ACP session has absorbed.
        self.known: list[str] | None = None
        self.issued_ids: set[str] = set()
        self.turn: Turn | None = None
        self.dirty = False
        self.opening = False
        self.last_used = time.monotonic()
        self.model: str | None = None
        self.effort: str | None = None

    @property
    def live_turn(self) -> Turn | None:
        turn = self.turn
        return turn if turn is not None and not turn.finished else None


@dataclass
class _Plan:
    kind: str  # continue | input | rebuild
    known: list[str]
    input: list[H.Msg] = field(default_factory=list)
    history: list[H.Msg] = field(default_factory=list)
    results: dict[str, H.Msg] = field(default_factory=dict)
    system_update: str = ""
    reason: str = ""


@dataclass
class Catalog:
    """Models and thinking levels the signed-in account offers."""

    models: list[tuple[str, str]] = field(default_factory=list)
    efforts: list[tuple[str, str]] = field(default_factory=list)
    variants: list[str] = field(default_factory=list)

    def model_ids(self) -> list[str]:
        if self.variants:
            return list(self.variants)
        return [value for value, _name in self.models]


def _find_option(options: list[dict], key: str) -> dict | None:
    for option in options:
        if isinstance(option, dict) and (option.get("id") == key or option.get("category") == key):
            return option
    return None


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _match_option_value(option: dict | None, wanted: str) -> str | None:
    if option is None or not wanted:
        return None
    choices = [c for c in option.get("options") or [] if isinstance(c, dict)]
    for choice in choices:
        if choice.get("value") == wanted:
            return wanted
    target = _norm(wanted)
    for choice in choices:
        if _norm(str(choice.get("value") or "")) == target or _norm(str(choice.get("name") or "")) == target:
            return str(choice.get("value"))
    return None


def _match_effort(option: dict | None, wanted: str) -> str | None:
    if option is None or not wanted:
        return None
    for alias in _EFFORT_ALIASES.get(wanted.lower(), [wanted]):
        found = _match_option_value(option, alias)
        if found is not None:
            return found
    return None


def resolve_model_choice(model_option: dict | None, requested: str) -> tuple[str | None, str | None]:
    """Resolve a requested model id (optionally carrying an effort suffix) against the session's model picker.

    Returns the picker value to select (``None`` keeps the session default) and
    the effort the id itself asked for.
    """
    requested = (requested or "").strip()
    if not requested:
        return None, None
    candidates: list[tuple[str, str | None]] = [(requested, None)]
    match = _EFFORT_SUFFIX.match(requested)
    if match and match.group(1).strip():
        candidates.append((match.group(1).strip(), match.group(2).lower()))
    if model_option is None:
        return None, candidates[-1][1]
    for base, effort in candidates:
        value = _match_option_value(model_option, base)
        if value is not None:
            return value, effort
    offered = ", ".join(str(c.get("value")) for c in model_option.get("options") or [] if isinstance(c, dict))
    raise AgyError("UNKNOWN_MODEL", f"Antigravity 未向当前账号提供模型 “{requested}”。可用模型: {offered or '(无)'}")


class AntigravityRuntime:
    """Owner of one ``agy acp`` front process and its conversations."""

    def __init__(self, config: RuntimeConfig, logger: Any = None) -> None:
        self.config = config
        self._logger = logger
        self._proc: AcpProcess | None = None
        self._start_lock = asyncio.Lock()
        self._catalog_lock = asyncio.Lock()
        self._bridge = McpBridge(self, log=self._warn)
        self._conversations: dict[str, Conversation] = {}
        self._ephemerals: set[Conversation] = set()
        self._tenants: dict[str, Conversation] = {}
        self._issued: dict[str, Conversation] = {}
        self._catalog: Catalog | None = None
        self._sweeper: asyncio.Task | None = None
        self._background: set[asyncio.Task] = set()
        self._binary: str | None = None
        self._version = "unknown"
        self._workspace = ""
        self._context_inspector = ContextInspector()
        self._context_plan: ContextPlan | None = None
        self._last_error = ""
        self._login: tuple[asyncio.subprocess.Process, str] | None = None
        self._stopped = False

    # ------------------------------------------------------------------ logging

    def _info(self, message: str) -> None:
        if self._logger is not None:
            self._logger.info(f"[antigravity-acp] {message}")

    def _warn(self, message: str) -> None:
        if self._logger is not None:
            self._logger.warning(f"[antigravity-acp] {message}")

    def _debug(self, message: str) -> None:
        if self._logger is not None:
            self._logger.debug(f"[antigravity-acp] {message}")

    def _spawn(self, coro: Any, name: str) -> None:
        task = asyncio.create_task(coro, name=name)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    # ---------------------------------------------------------------- lifecycle

    def _env(self) -> dict[str, str]:
        return build_env(self.config.home, self.config.proxy, self.config.extra_env)

    async def context_plan(self) -> ContextPlan:
        binary = resolve_agy_binary(self.config.agy_path)
        settings = self.config.context_settings
        patch = await self._context_inspector.inspect(binary, settings.patch_script)
        return resolve_plan(settings, patch)

    async def ensure_started(self) -> AcpProcess:
        """Start or replace the ACP process with the currently verified context policy."""
        async with self._start_lock:
            try:
                plan = await self.context_plan()
                proc = self._proc
                if proc is not None and proc.alive and self._context_plan == plan:
                    return proc
                if proc is not None:
                    await self.stop()
                    self._catalog = None
                self._stopped = False
                binary = resolve_agy_binary(self.config.agy_path)
                env = self._env()
                version = await read_version(binary, env)
                assert_supported_version(version, binary)
                os.makedirs(self.config.home, exist_ok=True)
                workspace = prepare_home(self.config.home, context_window=plan.settings_value)
            except PreflightError as exc:
                self._last_error = str(exc)
                raise AgyError("PREFLIGHT", str(exc)) from exc
            await self._bridge.start()
            argv = [
                binary, "acp",
                "--gemini-dir", self.config.home,
                "--app-data-dir", APP_DATA_DIR,
                "--agent", BRIDGE_AGENT,
                "--process-per-session",
                "--disable-telemetry",
                "--log-file", os.path.join(self.config.home, "acp.log"),
            ]
            proc = AcpProcess(
                argv, workspace, env,
                client_name=self.config.client_name, client_version=CLIENT_VERSION, log=self._warn,
            )
            proc.on_exit(lambda error, p=proc: self._on_process_exit(p, error))
            try:
                await proc.start(self.config.startup_timeout)
            except Exception as exc:  # noqa: BLE001
                self._last_error = str(exc)
                raise self._map_error(exc) from exc
            self._proc = proc
            self._binary = binary
            self._version = version
            self._workspace = workspace
            self._context_plan = plan
            self._last_error = ""
            if self._sweeper is None or self._sweeper.done():
                self._sweeper = asyncio.create_task(self._sweep_loop(), name="agy-acp:sweeper")
            self._info(
                f"agy acp started (agy {version}, pid {proc.pid}, home {self.config.home}, "
                f"context {plan.tokens} tokens, replay {plan.replay_bytes} bytes)"
            )
            return proc

    async def stop(self) -> None:
        """Stop the front process, every session, and the MCP bridge."""
        self._stopped = True
        sweeper, self._sweeper = self._sweeper, None
        if sweeper is not None:
            sweeper.cancel()
        for conv in [*self._conversations.values(), *self._ephemerals]:
            self._abort_turn(conv, "AstrBot is shutting this session down.")
            self._forget_session(conv)
        self._conversations.clear()
        self._ephemerals.clear()
        self._issued.clear()
        self._tenants.clear()
        login, self._login = self._login, None
        if login is not None and login[0].returncode is None:
            login[0].kill()
        proc, self._proc = self._proc, None
        if proc is not None:
            await proc.stop()
        await self._bridge.close()
        for task in list(self._background):
            task.cancel()

    def _on_process_exit(self, proc: AcpProcess, error: Exception) -> None:
        if self._proc is proc:
            self._proc = None
            if not self._stopped:
                self._warn(f"agy acp process ended: {error}")
        for conv in [*self._conversations.values(), *self._ephemerals]:
            if conv.process is not proc:
                continue
            turn = conv.live_turn
            if turn is not None:
                turn.events.push(("error", AgyError("TRANSPORT", f"agy acp 进程已退出: {error}", retryable=True)))
                turn.abandon("The Antigravity process exited.")
            self._forget_session(conv)

    # ------------------------------------------------------------------ catalog

    def _observe_catalog(self, config_options: Any, available_models: Any = None) -> None:
        options = [o for o in config_options or [] if isinstance(o, dict)]
        model_option = _find_option(options, "model")
        effort_option = _find_option(options, "thought_level")
        catalog = self._catalog or Catalog()
        if model_option is not None:
            catalog.models = [
                (str(c.get("value")), str(c.get("name") or c.get("value")))
                for c in model_option.get("options") or []
                if isinstance(c, dict) and c.get("value")
            ]
        if effort_option is not None:
            catalog.efforts = [
                (str(c.get("value")), str(c.get("name") or c.get("value")))
                for c in effort_option.get("options") or []
                if isinstance(c, dict) and c.get("value")
            ]
        if isinstance(available_models, list):
            variants = [str(m.get("modelId")) for m in available_models if isinstance(m, dict) and m.get("modelId")]
            if variants:
                catalog.variants = variants
        if catalog.models or catalog.variants:
            self._catalog = catalog

    async def list_models(self, refresh: bool = False) -> list[str]:
        """Model ids the signed-in account offers; a static list when the catalog is unavailable."""
        if self._catalog is not None and not refresh:
            return self._catalog.model_ids()
        async with self._catalog_lock:
            if self._catalog is not None and not refresh:
                return self._catalog.model_ids()
            try:
                proc = await self.ensure_started()
                result = await proc.new_session(
                    {"cwd": self._workspace, "mcpServers": []}, SessionListener(on_update=lambda _u: None), 120.0
                )
            except Exception as exc:  # noqa: BLE001
                mapped = self._map_error(exc)
                self._last_error = str(mapped)
                self._warn(f"could not read the model catalog: {mapped}")
                return list(FALLBACK_MODELS)
            models = result.get("models") if isinstance(result.get("models"), dict) else {}
            self._observe_catalog(result.get("configOptions"), models.get("availableModels"))
            self._spawn(proc.close_session(str(result.get("sessionId"))), "agy-acp:close-probe")
            return self._catalog.model_ids() if self._catalog is not None else list(FALLBACK_MODELS)

    async def status(self) -> dict:
        """Diagnostic snapshot for the status command."""
        binary = self._binary
        version = self._version
        error = self._last_error
        if binary is None:
            try:
                binary = resolve_agy_binary(self.config.agy_path)
                version = await read_version(binary, self._env())
            except PreflightError as exc:
                error = str(exc)
        plan = None
        patch = None
        if binary is not None:
            patch = await self._context_inspector.inspect(binary, self.config.context_settings.patch_script)
            try:
                plan = resolve_plan(self.config.context_settings, patch)
            except PreflightError as exc:
                error = str(exc)
        running = self._proc is not None and self._proc.alive
        return {
            "context_window": self.config.context_settings.window,
            "context_tokens": plan.tokens if plan else None,
            "replay_budget_bytes": plan.replay_bytes if plan else None,
            "running_context_tokens": self._context_plan.tokens if running and self._context_plan else None,
            "patch_state": patch.state if patch else "unknown",
            "patch_detail": patch.detail if patch else "找不到 agy 二进制",
            "patch_script": patch.script if patch else self.config.context_settings.patch_script,
            "binary": binary or "(未找到)",
            "version": version,
            "home": self.config.home,
            "proxy": self.config.proxy or "(直连)",
            "logged_in": has_credential(self.config.home),
            "running": self._proc is not None and self._proc.alive,
            "pid": self._proc.pid if self._proc is not None else None,
            "sessions": sum(1 for c in [*self._conversations.values(), *self._ephemerals] if c.session_id),
            "models": self._catalog.model_ids() if self._catalog is not None else [],
            "efforts": [value for value, _name in self._catalog.efforts] if self._catalog is not None else [],
            "last_error": error,
            "login_command": login_command(binary or "agy", self.config.proxy),
        }

    # -------------------------------------------------------------------- login

    async def begin_login(self) -> str:
        """Start ``agy acp --login`` and return the Google authorization URL."""
        binary = resolve_agy_binary(self.config.agy_path)
        previous, self._login = self._login, None
        if previous is not None and previous[0].returncode is None:
            previous[0].kill()
        proc = await asyncio.create_subprocess_exec(
            binary, "acp", "--login",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            env=self._env(),
            cwd=os.path.expanduser("~"),
        )
        assert proc.stdout is not None
        buffer = ""
        deadline = time.monotonic() + 45
        url = ""
        while time.monotonic() < deadline:
            try:
                chunk = await asyncio.wait_for(proc.stdout.read(4096), max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            if not chunk:
                break
            buffer += chunk.decode("utf-8", "replace")
            match = _LOGIN_URL.search(buffer)
            if match and (buffer.endswith("\n") or "paste" in buffer.lower()):
                url = match.group(0)
                break
        if not url:
            if proc.returncode is None:
                proc.kill()
            tail = " ".join(buffer.split())[-300:]
            raise AgyError("AUTH", f"agy 没有给出登录链接。请在服务器终端手动执行登录命令。输出: {tail}")
        self._login = (proc, url)
        return url

    async def complete_login(self, code: str) -> tuple[bool, str]:
        """Finish a pending sign-in with the authorization code from the browser."""
        login, self._login = self._login, None
        if login is None or login[0].returncode is not None:
            return False, "没有正在等待的登录流程，请先执行登录。"
        proc = login[0]
        assert proc.stdin is not None and proc.stdout is not None
        try:
            proc.stdin.write((code.strip() + "\n").encode("utf-8"))
            await proc.stdin.drain()
            out, _ = await asyncio.wait_for(proc.communicate(), 120)
        except asyncio.TimeoutError:
            proc.kill()
            return False, "登录在 120 秒内没有完成。"
        except Exception as exc:  # noqa: BLE001
            return False, f"登录进程异常: {exc}"
        tail = " ".join(line for line in out.decode("utf-8", "replace").split("\n") if "google.Init" not in line).strip()
        if proc.returncode != 0:
            return False, f"登录失败 (exit {proc.returncode}): {tail[-300:]}"
        sync_oauth_token(self.config.home)
        # The running front loaded its credentials at start; restart it so sessions see the new login.
        await self.restart()
        return True, tail[-200:]

    async def restart(self) -> None:
        """Stop the front process; the next request starts a fresh one."""
        self._catalog = None
        proc, self._proc = self._proc, None
        for conv in [*self._conversations.values(), *self._ephemerals]:
            self._abort_turn(conv, "The Antigravity process is restarting.")
            self._forget_session(conv)
        if proc is not None:
            await proc.stop()

    # ------------------------------------------------------------ conversations

    def _acquire(self, request: ChatRequest) -> Conversation:
        # A request that returns results for calls this runtime issued belongs to the
        # conversation that issued them, whatever key it carries.
        for message in reversed(request.messages):
            if message.role != "tool":
                break
            owner = self._issued.get(message.tool_call_id)
            if owner is not None and owner.live_turn is not None:
                return owner
        if request.key is None:
            conv = Conversation(None)
            self._ephemerals.add(conv)
            return conv
        key = f"{request.key}|{H.digest(request.tools)[:8]}"
        conv = self._conversations.get(key)
        if conv is None:
            conv = Conversation(key)
            self._conversations[key] = conv
        return conv

    def _release_tenant(self, conv: Conversation) -> None:
        if conv.tenant_key is not None:
            self._tenants.pop(conv.tenant_key, None)
            self._bridge.forget_tenant(conv.tenant_key)
        conv.tenant_key = None
        conv.tenant_secret = None

    def _forget_session(self, conv: Conversation) -> None:
        """Drop the conversation's binding to its ACP session without talking to the process."""
        self._release_tenant(conv)
        conv.session_id = None
        conv.process = None
        conv.config_options = []
        conv.known = None
        conv.reported_tool_calls = {}
        conv.model = None
        conv.effort = None
        conv.dirty = False

    def _abort_turn(self, conv: Conversation, reason: str) -> None:
        """Cancel the conversation's running turn and release its parked calls."""
        turn = conv.live_turn
        if turn is None:
            return
        turn.finished = True
        if conv.session_id is not None and conv.process is not None and conv.process.alive:
            conv.process.cancel(conv.session_id)
        turn.abandon(reason)
        if turn.prompt_task is not None and not turn.prompt_task.done():
            # Let the prompt request settle with `cancelled`; never leave it awaited by nobody.
            turn.prompt_task.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
        for call_id in list(turn.calls):
            self._issued.pop(call_id, None)
        # The agent may have produced output AstrBot never saw; the session no longer mirrors its history.
        conv.dirty = True

    def _reset_session(self, conv: Conversation) -> None:
        """Close the conversation's ACP session (in the background) and forget it."""
        self._abort_turn(conv, "AstrBot replaced this session.")
        session_id, proc = conv.session_id, conv.process
        self._forget_session(conv)
        if session_id is not None and proc is not None and proc.alive:
            self._spawn(proc.close_session(session_id), "agy-acp:close-session")

    def _drop_conversation(self, conv: Conversation) -> None:
        self._reset_session(conv)
        for call_id in list(conv.issued_ids):
            self._issued.pop(call_id, None)
        self._ephemerals.discard(conv)
        if conv.key is not None and self._conversations.get(conv.key) is conv:
            del self._conversations[conv.key]

    async def reset_conversations(self) -> int:
        """Drop every conversation so the next request of each chat rebuilds its session."""
        convs = [*self._conversations.values(), *self._ephemerals]
        for conv in convs:
            self._drop_conversation(conv)
        return len(convs)

    def _evict_for_capacity(self, keep: Conversation) -> None:
        live = [c for c in [*self._conversations.values(), *self._ephemerals] if c.session_id is not None and c is not keep]
        excess = len(live) + 1 - max(1, self.config.max_live_sessions)
        if excess <= 0:
            return
        idle = sorted((c for c in live if self._is_idle(c)), key=lambda c: c.last_used)
        for conv in idle[:excess]:
            self._debug(f"evicting idle session {conv.session_id} to stay within max_live_sessions")
            self._drop_conversation(conv)

    @staticmethod
    def _is_idle(conv: Conversation) -> bool:
        return conv.live_turn is None and not conv.opening and not conv.lock.locked()

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                now = time.monotonic()
                for conv in [*self._conversations.values(), *self._ephemerals]:
                    age = now - conv.last_used
                    if self._is_idle(conv) and age > self.config.idle_session_ttl:
                        self._drop_conversation(conv)
                    elif conv.live_turn is not None and not conv.lock.locked() and age > self.config.park_ttl:
                        # AstrBot never came back with the tool results of a parked turn.
                        self._drop_conversation(conv)
            except Exception as exc:  # noqa: BLE001
                self._warn(f"sweep failed: {exc!r}")

    # ----------------------------------------------------------------- planning

    def _plan(self, conv: Conversation, request: ChatRequest) -> _Plan:
        fingerprints = [H.fingerprint(m) for m in H.persistent(request.messages)]

        def rebuild(reason: str) -> _Plan:
            history, tail = H.split_trailing_input(request.messages)
            return _Plan("rebuild", fingerprints, input=tail, history=history, reason=reason)

        if conv.session_id is None:
            return rebuild("no session")
        if conv.dirty:
            return rebuild("session out of sync")
        if conv.process is None or not conv.process.alive:
            return rebuild("process restarted")
        if conv.known is None:
            return rebuild("session has no history yet")
        if conv.tools_digest != H.digest(request.tools if self.config.expose_tools else []):
            return rebuild("tool set changed")
        known = conv.known
        if len(known) > len(fingerprints) or fingerprints[: len(known)] != known:
            return rebuild("history was rewritten")
        # Everything after the last known persisted message is the tail of this request.
        index = 0
        remaining = len(known)
        while index < len(request.messages) and remaining > 0:
            if not request.messages[index].transient:
                remaining -= 1
            index += 1
        tail = request.messages[index:]
        results: dict[str, H.Msg] = {}
        skip = 0
        while skip < len(tail):
            message = tail[skip]
            if message.role == "assistant":
                skip += 1
            elif message.role == "tool" and message.tool_call_id in conv.issued_ids:
                results[message.tool_call_id] = message
                skip += 1
            else:
                break
        rest = tail[skip:]
        turn = conv.live_turn
        if turn is not None:
            if turn.pending and not rest and any(call_id in results for call_id in turn.pending):
                return _Plan("continue", fingerprints, results=results)
            return rebuild("a parked turn was not continued")
        if rest and all(m.role in ("user", "system") for m in rest):
            delta = H.system_delta(conv.system_text or "", request.system)
            if delta is None:
                return rebuild("system prompt changed")
            return _Plan("input", fingerprints, input=rest, system_update=delta)
        return rebuild("no new user input")

    # ------------------------------------------------------------------ session

    async def _open_session(self, conv: Conversation, proc: AcpProcess, request: ChatRequest) -> None:
        raw_tools = request.tools if self.config.expose_tools else []
        conv.tools = [H.compact_bridge_tool(tool) for tool in raw_tools]
        conv.tool_names = {tool["name"] for tool in conv.tools}
        conv.tools_digest = H.digest(raw_tools)
        mcp_servers: list[dict] = []
        if conv.tools:
            conv.tenant_key = secrets.token_hex(16)
            conv.tenant_secret = secrets.token_hex(24)
            self._tenants[conv.tenant_key] = conv
            mcp_servers.append(
                {
                    "type": "http",
                    "name": H.MCP_SERVER_NAME,
                    "url": f"{self._bridge.url}/{conv.tenant_key}",
                    "headers": [{"name": "Authorization", "value": f"Bearer {conv.tenant_secret}"}],
                }
            )
        listener = SessionListener(
            on_update=lambda update: self._on_update(conv, update),
            on_permission=lambda req: self._answer_permission(conv, req),
        )
        self._evict_for_capacity(conv)
        conv.opening = True
        try:
            result = await proc.new_session(
                {
                    "cwd": self._workspace,
                    "mcpServers": mcp_servers,
                    "_meta": {"antigravity": {"systemPrompt": {"append": H.bridge_system_prompt(request.system, raw_tools)}}},
                },
                listener,
                120.0,
            )
        except Exception as exc:  # noqa: BLE001
            self._release_tenant(conv)
            raise self._map_error(exc) from exc
        finally:
            conv.opening = False
        conv.session_id = str(result["sessionId"])
        conv.process = proc
        conv.config_options = [o for o in result.get("configOptions") or [] if isinstance(o, dict)]
        conv.system_text = request.system
        conv.known = None
        conv.dirty = False
        conv.model = None
        conv.effort = None
        conv.reported_tool_calls = {}
        models = result.get("models") if isinstance(result.get("models"), dict) else {}
        self._observe_catalog(conv.config_options, models.get("availableModels"))

    async def _apply_model_and_effort(self, conv: Conversation, proc: AcpProcess, model: str, effort: str) -> bool:
        """Point the ACP session at the requested model and thinking level.

        agy replaces the whole option list on each switch, so every step re-reads
        it and retries once when the value did not stick.
        """
        session_id = conv.session_id
        assert session_id is not None
        switched = False
        model_option = _find_option(conv.config_options, "model")
        target_model, model_effort = resolve_model_choice(model_option, model)
        if target_model is not None and model_option is not None:
            if conv.model != target_model and model_option.get("currentValue") != target_model:
                try:
                    conv.config_options = await proc.set_config_option(session_id, str(model_option.get("id") or "model"), target_model)
                    model_option = _find_option(conv.config_options, "model")
                    if model_option is not None and model_option.get("currentValue") not in (None, target_model):
                        conv.config_options = await proc.set_config_option(session_id, str(model_option.get("id") or "model"), target_model)
                except Exception as exc:  # noqa: BLE001
                    raise self._map_error(exc) from exc
                switched = True
            conv.model = target_model
        wanted_effort = model_effort or (effort or "").strip().lower() or (self.config.default_effort or "").strip().lower()
        level_option = _find_option(conv.config_options, "thought_level")
        target_effort = _match_effort(level_option, wanted_effort)
        if wanted_effort and level_option is not None and target_effort is None:
            self._debug(f"thinking level {wanted_effort!r} is not offered for this model; keeping the default")
        if target_effort is not None and level_option is not None:
            if conv.effort != target_effort and level_option.get("currentValue") != target_effort:
                try:
                    conv.config_options = await proc.set_config_option(session_id, str(level_option.get("id") or "thought_level"), target_effort)
                    switched = True
                except JsonRpcRemoteError as exc:
                    self._warn(f"could not set thinking level {target_effort!r}: {exc.rpc_message}")
                except Exception as exc:  # noqa: BLE001
                    raise self._map_error(exc) from exc
            conv.effort = target_effort
        return switched

    def _on_update(self, conv: Conversation, update: dict) -> None:
        if update.get("sessionUpdate") == "config_option_update" and isinstance(update.get("configOptions"), list):
            conv.config_options = [o for o in update["configOptions"] if isinstance(o, dict)]
            self._observe_catalog(conv.config_options)
        if self._record_reported_tool_call(conv, update) == "conflict":
            self._warn(f"agent reported conflicting identity for tool call {update.get('toolCallId')}")
        turn = conv.live_turn
        if turn is not None:
            turn.on_update(update)

    # ------------------------------------------------------------- permissions

    @staticmethod
    def _tool_identity(source: dict) -> tuple[str | None, str | None]:
        meta = source.get("_meta") if isinstance(source.get("_meta"), dict) else {}

        def first(*values: Any) -> str | None:
            for value in values:
                if isinstance(value, str) and value:
                    return value
            return None

        server = first(source.get("mcpServerName"), meta.get("mcpServerName"), meta.get("serverName"), meta.get("server"))
        tool = first(source.get("mcpToolName"), meta.get("mcpToolName"), meta.get("toolName"), meta.get("tool"))
        # agy 1.5.8 reports MCP identity in the explicit tool name, not mcpServerName.
        # Never infer identity from a title or raw arguments, or from native tool kinds.
        name = source.get("name")
        if source.get("kind") == "other" and isinstance(name, str) and name.startswith(H.AGENT_TOOL_PREFIX):
            named_tool = name[len(H.AGENT_TOOL_PREFIX):]
            if named_tool:
                if server not in (None, H.MCP_SERVER_NAME) or tool not in (None, named_tool):
                    return "__mcp_identity_conflict__", None
                server, tool = H.MCP_SERVER_NAME, named_tool
        return server, tool

    def _record_reported_tool_call(self, conv: Conversation, update: dict) -> str | None:
        """Merge one tool identity report; only these reports may authorize a permission request."""
        if update.get("sessionUpdate") not in ("tool_call", "tool_call_update"):
            return None
        call_id = update.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return None
        server, tool = self._tool_identity(update)
        kind = update.get("kind") if isinstance(update.get("kind"), str) else None
        existing = conv.reported_tool_calls.get(call_id)
        merged = dict(existing) if existing is not None else {}
        conflict = False
        for field_name, value in (("kind", kind), ("server", server), ("tool", tool)):
            if value is None:
                continue
            if merged.get(field_name) not in (None, value):
                conflict = True
            else:
                merged[field_name] = value
        if conflict:
            merged["conflict"] = True
        conv.reported_tool_calls[call_id] = merged
        if conflict:
            return "conflict"
        return "created" if existing is None else "merged"

    def _answer_permission(self, conv: Conversation, request: dict) -> dict:
        """Decide one ``session/request_permission``.

        Only a tool call the agent reported itself, carrying this bridge's server
        name, on a tool this session offers, during a live turn may run. Every
        other request (shell, file edits, anything native) is refused.
        """
        options = [o for o in request.get("options") or [] if isinstance(o, dict)]
        tool_call = request.get("toolCall") if isinstance(request.get("toolCall"), dict) else {}

        def pick(kind: str) -> dict | None:
            for option in options:
                if option.get("kind") == kind:
                    return {"outcome": "selected", "optionId": option.get("optionId")}
            return None

        def reject(reason: str) -> dict:
            self._warn(f"refused permission request ({reason}): {tool_call.get('title') or 'unknown'}")
            return pick("reject_once") or {"outcome": "cancelled"}

        if conv.session_id is None or request.get("sessionId") != conv.session_id:
            return reject("session mismatch")
        tenant_key = conv.tenant_key
        if tenant_key is None:
            return reject("no AstrBot tools are mounted for this session")
        if conv.live_turn is None:
            return reject("no live turn")
        call_id = tool_call.get("toolCallId")
        if not isinstance(call_id, str) or not call_id:
            return reject("missing toolCallId")
        reported = conv.reported_tool_calls.get(call_id)
        claimed_server, claimed_tool = self._tool_identity(tool_call)
        if reported is None:
            if claimed_server is not None or claimed_tool is not None:
                return reject(f"tool call {call_id} was never reported by the agent")
            raw_input = tool_call.get("rawInput")
            empty = raw_input is None or (isinstance(raw_input, (str, list, dict)) and not (raw_input.strip() if isinstance(raw_input, str) else raw_input))
            if tool_call.get("kind") == "other" and empty:
                if not self._bridge.has_handshake(tenant_key):
                    return reject("identity-less probe without a completed MCP handshake")
                return pick("allow_once") or {"outcome": "cancelled"}
            return reject("missing structured tool metadata")
        if reported.get("conflict"):
            return reject(f"reported identity of tool call {call_id} conflicts across agent updates")
        if claimed_server is not None and claimed_server != reported.get("server"):
            return reject("MCP server name conflict")
        if claimed_tool is not None and claimed_tool != reported.get("tool"):
            return reject("tool name conflict")
        if reported.get("server") != H.MCP_SERVER_NAME:
            return reject(f"unknown MCP server {reported.get('server') or '(none)'}")
        tool_name = reported.get("tool")
        if tool_name is None or (tool_name not in conv.tool_names and tool_name != H.AWAIT_TOOL_NAME):
            return reject(f"tool is not offered to this session: {tool_name or '(none)'}")
        return pick("allow_once") or {"outcome": "cancelled"}

    # --------------------------------------------------------------- MCP router

    def list_tools(self, tenant_key: str) -> list[dict]:
        conv = self._tenants.get(tenant_key)
        if conv is None:
            return []
        return [*conv.tools, H.AWAIT_TOOL]

    def authorize(self, tenant_key: str, bearer_token: str | None) -> bool:
        conv = self._tenants.get(tenant_key)
        if conv is None or conv.tenant_secret is None or not bearer_token:
            return False
        return secrets.compare_digest(conv.tenant_secret, bearer_token)

    async def call_tool(self, tenant_key: str, conversation_id: str | None, name: str, arguments: dict) -> dict:
        conv = self._tenants.get(tenant_key)
        if conv is None:
            return error_result("No AstrBot turn is waiting for this tool call.")
        if conversation_id is not None and conversation_id != conv.session_id:
            return error_result("Tenant session mismatch.")
        turn = conv.live_turn
        if turn is None:
            return error_result("No AstrBot turn is waiting for this tool call.")
        if name == H.AWAIT_TOOL_NAME:
            call = turn.calls.get(str(arguments.get("call_id") or ""))
            if call is None:
                return error_result(f"Unknown AstrBot call id {arguments.get('call_id')}.")
            turn.relaying = False
            return await self._await_call(turn, call)
        if name not in conv.tool_names:
            return error_result(f"AstrBot has no tool named {name}.")
        # The new CLI includes its display-only summary in MCP arguments.
        # Preserve it only when the actual AstrBot schema declares that parameter.
        schema = next((tool.get("inputSchema", {}) for tool in conv.tools if tool["name"] == name), {})
        if "toolSummary" in arguments and "toolSummary" not in schema.get("properties", {}):
            arguments = {key: value for key, value in arguments.items() if key != "toolSummary"}
        return await self._await_call(turn, turn.park(name, arguments))

    async def _await_call(self, turn: Turn, call: ParkedCall) -> dict:
        try:
            return await asyncio.wait_for(asyncio.shield(call.future), self.config.relay_after)
        except asyncio.TimeoutError:
            # agy abandons one MCP call after 180 seconds; hand the wait back to the model.
            turn.relaying = True
            return {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            f"AstrBot is still running tool call {call.id} ({call.name}). Call "
                            f'{H.AGENT_TOOL_PREFIX}{H.AWAIT_TOOL_NAME} with {{"call_id":"{call.id}"}} now to receive its '
                            "result. Do not repeat the original call and do not answer the user before the result arrives."
                        ),
                    }
                ]
            }

    # --------------------------------------------------------------------- turn

    def _start_prompt(self, conv: Conversation, turn: Turn, proc: AcpProcess, blocks: list[dict]) -> None:
        session_id = conv.session_id
        assert session_id is not None

        async def run() -> None:
            try:
                result = await proc.prompt(session_id, blocks)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                turn.events.push(("error", self._map_error(exc)))
            else:
                turn.events.push(("done", result))

        turn.prompt_task = asyncio.create_task(run(), name="agy-acp:prompt")

    async def _tool_result(self, message: H.Msg, request: ChatRequest) -> dict:
        content: list[dict] = [{"type": "text", "text": message.text or "(empty tool result)"}]
        for ref in message.images:
            image = await request.resolve_image(ref)
            if image is not None:
                content.append({"type": "image", "data": image.data, "mimeType": image.mime_type})
        return {"content": content}

    async def stream(self, request: ChatRequest) -> AsyncGenerator[tuple, None]:
        """Run one model step.

        Yields ``("reasoning", text)`` and ``("text", text)`` deltas and ends with
        either ``("tool_calls", [ParkedCall, ...])`` or ``("finish", {...})``.
        """
        await self.ensure_started()
        conv = self._acquire(request)
        async with conv.lock:
            conv.last_used = time.monotonic()
            try:
                async for event in self._run_step(conv, request):
                    yield event
            finally:
                conv.last_used = time.monotonic()
                turn = conv.live_turn
                parked = turn is not None and bool(turn.pending)
                if turn is not None and not parked:
                    # The step was cancelled or failed mid-stream.
                    self._abort_turn(conv, "AstrBot cancelled this request.")
                if conv.ephemeral and not parked:
                    self._drop_conversation(conv)

    async def _run_step(self, conv: Conversation, request: ChatRequest) -> AsyncGenerator[tuple, None]:
        proc = await self.ensure_started()
        plan = self._plan(conv, request)
        if plan.kind == "continue":
            turn = conv.live_turn
            assert turn is not None
            for call_id, call in list(turn.pending.items()):
                message = plan.results.get(call_id)
                result = await self._tool_result(message, request) if message is not None else error_result(
                    "AstrBot returned no result for this tool call."
                )
                if not call.future.done():
                    call.future.set_result(result)
                turn.pending.pop(call_id, None)
            conv.known = plan.known
        else:
            if plan.kind == "rebuild":
                if conv.session_id is not None:
                    self._debug(f"rebuilding session for {conv.key or 'ephemeral'}: {plan.reason}")
                self._reset_session(conv)
                await self._open_session(conv, proc, request)
                blocks = await H.replay_prompt(
                    plan.history,
                    plan.input,
                    request.resolve_image,
                    max_bytes=self._context_plan.replay_bytes,
                    replay_images=self.config.replay_images,
                )
            else:
                blocks = await H.input_prompt(plan.input, request.resolve_image)
                if plan.system_update:
                    notice = (
                        "<system_update>\nThese system instruction lines changed and supersede their earlier versions:\n"
                        f"{plan.system_update}\n</system_update>\n\n"
                    )
                    blocks.insert(0, {"type": "text", "text": notice})
                conv.system_text = request.system
            fresh = conv.known is None
            if await self._apply_model_and_effort(conv, proc, request.model, request.effort) and fresh:
                await asyncio.sleep(COLD_SWITCH_SETTLE)
            turn = Turn(secrets.token_hex(4))
            conv.turn = turn
            self._start_prompt(conv, turn, proc, blocks)
            conv.known = plan.known
        async for event in self._pump(conv, turn, proc):
            yield event

    async def _pump(self, conv: Conversation, turn: Turn, proc: AcpProcess) -> AsyncGenerator[tuple, None]:
        """Stream one AstrBot step out of the running ACP turn."""
        while True:
            if not await turn.events.wait(self.config.request_timeout):
                raise AgyError(
                    "TIMEOUT", f"Antigravity 在 {self.config.request_timeout:g} 秒内没有任何输出", retryable=not turn.produced
                )
            if (turn.events.peek() or ("",))[0] != "text":
                held = turn.recover_notice_hold()
                if held:
                    turn.produced = True
                    yield ("text", held)
            event = turn.events.shift()
            kind = event[0]
            if kind == "thought":
                yield ("reasoning", event[1])
            elif kind == "text":
                turn.produced = True
                yield ("text", event[1])
            elif kind == "park":
                turn.produced = True
                batch: list[ParkedCall] = [event[1]]
                # Parallel calls of one model response arrive within a short window.
                while await turn.events.wait(self.config.park_settle):
                    nxt = turn.events.peek()
                    if nxt is None or nxt[0] != "park":
                        break
                    turn.events.shift()
                    batch.append(nxt[1])
                for call in batch:
                    turn.pending[call.id] = call
                    conv.issued_ids.add(call.id)
                    self._issued[call.id] = conv
                turn.tool_steps += 1
                yield ("tool_calls", batch)
                return
            elif kind == "done":
                if turn.agent_failure is not None:
                    turn.finished = True
                    conv.dirty = True
                    raise turn.agent_failure
                result = event[1] if isinstance(event[1], dict) else {}
                stop = result.get("stopReason")
                if stop == "max_tokens" and turn.continuations < self.config.max_continuations:
                    turn.continuations += 1
                    self._start_prompt(conv, turn, proc, [{"type": "text", "text": CONTINUE_PROMPT}])
                    continue
                turn.finished = True
                for call_id in list(turn.calls):
                    self._issued.pop(call_id, None)
                if stop == "end_turn" and not turn.produced:
                    conv.dirty = True
                    raise AgyError("EMPTY_RESPONSE", "Antigravity 结束了本轮回答但没有任何可见输出", retryable=True)
                if stop == "cancelled":
                    conv.dirty = True
                    raise AgyError("CANCELLED", "Antigravity 取消了本轮回答")
                if stop == "refusal":
                    conv.dirty = True
                    raise AgyError("REFUSAL", "Antigravity 拒绝了这次请求")
                if stop not in ("end_turn", "max_tokens"):
                    conv.dirty = True
                    raise AgyError("SERVER", f"Antigravity 异常停止: {stop}")
                yield ("finish", {"stop": stop, "usage": result.get("usage"), "steps": turn.tool_steps + turn.continuations + 1})
                return
            elif kind == "error":
                turn.finished = True
                turn.abandon("The Antigravity turn failed.")
                conv.dirty = True
                error = event[1]
                if isinstance(error, AgyError) and error.retryable and turn.produced:
                    error.retryable = False
                raise error

    # ------------------------------------------------------------------- errors

    def _map_error(self, error: BaseException) -> Exception:
        if isinstance(error, AgyError):
            return error
        if isinstance(error, JsonRpcRemoteError):
            message = error.rpc_message
            low = message.lower()
            if "authentication required" in low or "unauthenticated" in low or "not logged in" in low:
                binary = self._binary or "agy"
                return AgyError(
                    "AUTH",
                    "Antigravity 尚未登录。请在 AstrBot 中发送 `/agy login`，或在服务器终端执行: "
                    + login_command(binary, self.config.proxy),
                )
            if re.search(r"quota|limit reached|exhausted|out of credits", low):
                return AgyError("QUOTA", f"Antigravity 额度已用尽: {message}")
            if re.search(r"\b429\b|rate.?limit|overloaded|high traffic|too many requests", low):
                return AgyError("RATE_LIMIT", f"Antigravity 限流: {message}", retryable=True)
            if error.code == -32004 or ("session" in low and "not found" in low):
                return AgyError("SESSION_LOST", f"Antigravity 会话已失效: {message}", retryable=True)
            return AgyError("SERVER", f"Antigravity 返回错误: {message}")
        if isinstance(error, (JsonRpcClosedError, ConnectionError)):
            return AgyError("TRANSPORT", f"与 agy acp 进程的连接中断: {error}", retryable=True)
        if isinstance(error, TimeoutError):
            return AgyError("TIMEOUT", f"Antigravity 响应超时: {error}", retryable=True)
        if isinstance(error, PreflightError):
            return AgyError("PREFLIGHT", str(error))
        return error if isinstance(error, Exception) else RuntimeError(str(error))
