"""One long-lived ``agy acp`` front process.

With ``--process-per-session`` the front serves every ACP session from an
isolated worker, so several chats run concurrently on one stdio link. This
module owns the link, routes per-session notifications, and refuses every
agent-to-client capability AstrBot does not provide (file system, terminal).
"""

from __future__ import annotations

import asyncio
import collections
from dataclasses import dataclass
from typing import Any, Callable

from .jsonrpc import JsonRpcPeer, JsonRpcRemoteError

# One ACP frame can carry a whole base64 image or a large tool result.
STREAM_LIMIT = 256 * 1024 * 1024
_STDERR_NOISE = ("logging before google.Init",)


@dataclass
class SessionListener:
    """Per-session observer of agent notifications and permission requests."""

    on_update: Callable[[dict], None]
    on_permission: Callable[[dict], dict | None] | None = None


class AcpProcess:
    """One running ``agy acp`` front with its initialized link."""

    def __init__(
        self,
        argv: list[str],
        cwd: str,
        env: dict[str, str],
        *,
        client_name: str,
        client_version: str,
        log: Callable[[str], None] | None = None,
    ) -> None:
        self._argv = argv
        self._cwd = cwd
        self._env = env
        self._client_name = client_name
        self._client_version = client_version
        self._log = log
        self._proc: asyncio.subprocess.Process | None = None
        self._peer: JsonRpcPeer | None = None
        self._listeners: dict[str, SessionListener] = {}
        self._exit_error: Exception | None = None
        self._exit_listeners: list[Callable[[Exception], None]] = []
        self._stderr_tail: collections.deque[str] = collections.deque(maxlen=40)
        self._tasks: list[asyncio.Task] = []
        self.initialize_result: dict | None = None

    # ------------------------------------------------------------------ state

    @property
    def alive(self) -> bool:
        return self._peer is not None and not self._peer.is_closed and self._exit_error is None

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None else None

    def on_exit(self, listener: Callable[[Exception], None]) -> None:
        """Observe process exit or link closure."""
        self._exit_listeners.append(listener)

    def stderr_tail(self, lines: int = 5) -> str:
        return " | ".join(list(self._stderr_tail)[-lines:])

    # -------------------------------------------------------------- lifecycle

    async def start(self, timeout: float = 120.0) -> None:
        """Spawn the process and complete the ACP ``initialize`` handshake."""
        self._proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=self._cwd,
            env=self._env,
            limit=STREAM_LIMIT,
        )
        assert self._proc.stdin is not None and self._proc.stdout is not None
        self._peer = JsonRpcPeer(
            self._proc.stdout,
            self._proc.stdin,
            on_request=self._on_request,
            on_notification=self._on_notification,
            on_close=self._fail,
            on_protocol_error=lambda detail: self._warn(f"acp: dropped frame: {detail}"),
        )
        self._peer.start()
        self._tasks.append(asyncio.create_task(self._pump_stderr(), name="agy-acp:stderr"))
        self._tasks.append(asyncio.create_task(self._watch_exit(), name="agy-acp:wait"))
        try:
            self.initialize_result = await self._peer.request(
                "initialize",
                {
                    "protocolVersion": 1,
                    "clientCapabilities": {},
                    "clientInfo": {"name": self._client_name, "version": self._client_version},
                },
                timeout,
            )
        except BaseException:
            await self.stop()
            raise

    async def stop(self) -> None:
        """Terminate the process."""
        self._fail(RuntimeError("ACP process stopped"))
        if self._peer is not None:
            self._peer.close(RuntimeError("ACP process stopped"))
        proc = self._proc
        if proc is not None and proc.returncode is None:
            try:
                proc.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
        current = asyncio.current_task()
        for task in self._tasks:
            if task is not current:
                task.cancel()
        self._tasks.clear()

    # --------------------------------------------------------------- requests

    async def new_session(self, params: dict, listener: SessionListener, timeout: float = 120.0) -> dict:
        """Create one ACP session and bind its listener."""
        result = await self._link().request("session/new", params, timeout)
        result = result if isinstance(result, dict) else {}
        session_id = result.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            raise RuntimeError("agy acp returned no sessionId for session/new")
        self._listeners[session_id] = listener
        return result

    async def set_config_option(self, session_id: str, config_id: str, value: str) -> list[dict]:
        """Select one configuration option value; returns the complete option list."""
        result = await self._link().request(
            "session/set_config_option",
            {"sessionId": session_id, "configId": config_id, "value": value},
            60.0,
        )
        options = result.get("configOptions") if isinstance(result, dict) else None
        return options if isinstance(options, list) else []

    async def prompt(self, session_id: str, prompt: list[dict]) -> dict:
        """Send one prompt; resolves when the agent ends the turn."""
        result = await self._link().request("session/prompt", {"sessionId": session_id, "prompt": prompt})
        return result if isinstance(result, dict) else {}

    def cancel(self, session_id: str) -> None:
        """Request cancellation of the running turn."""
        if self._peer is not None:
            self._peer.notify("session/cancel", {"sessionId": session_id})

    async def close_session(self, session_id: str) -> None:
        """Close one session and detach its listener."""
        self._listeners.pop(session_id, None)
        if not self.alive:
            return
        try:
            await self._link().request("session/close", {"sessionId": session_id}, 15.0)
        except Exception:  # noqa: BLE001 - a session that cannot close is released with its worker
            pass

    # --------------------------------------------------------------- internal

    def _link(self) -> JsonRpcPeer:
        if self._peer is None or self._peer.is_closed or self._exit_error is not None:
            detail = f": {self._exit_error}" if self._exit_error is not None else ""
            raise ConnectionError(f"ACP link unavailable{detail}")
        return self._peer

    def _warn(self, message: str) -> None:
        if self._log is not None:
            self._log(message)

    def _fail(self, error: Exception) -> None:
        if self._exit_error is not None:
            return
        self._exit_error = error
        for listener in list(self._exit_listeners):
            try:
                listener(error)
            except Exception:  # noqa: BLE001
                pass

    async def _pump_stderr(self) -> None:
        assert self._proc is not None and self._proc.stderr is not None
        try:
            while True:
                line = await self._proc.stderr.readline()
                if not line:
                    return
                text = line.decode("utf-8", "replace").rstrip()
                if text and not any(noise in text for noise in _STDERR_NOISE):
                    self._stderr_tail.append(text[:500])
        except (asyncio.CancelledError, ValueError):
            return

    async def _watch_exit(self) -> None:
        assert self._proc is not None
        code = await self._proc.wait()
        tail = self.stderr_tail()
        error = RuntimeError(f"agy acp exited (code={code})" + (f": {tail}" if tail else ""))
        if self._peer is not None:
            self._peer.close(error)
        self._fail(error)

    def _on_notification(self, method: str, params: Any) -> None:
        if method != "session/update" or not isinstance(params, dict):
            return
        session_id = params.get("sessionId")
        update = params.get("update")
        if not isinstance(session_id, str) or not isinstance(update, dict):
            return
        listener = self._listeners.get(session_id)
        if listener is not None:
            listener.on_update(update)

    def _on_request(self, method: str, params: Any) -> Any:
        if method == "session/request_permission" and isinstance(params, dict):
            listener = self._listeners.get(str(params.get("sessionId")))
            outcome = None
            if listener is not None and listener.on_permission is not None:
                outcome = listener.on_permission(params)
            return {"outcome": outcome or {"outcome": "cancelled"}}
        # File-system, terminal, and elicitation capabilities are never advertised.
        self._warn(f"acp: refused agent request {method}")
        raise JsonRpcRemoteError(method, -32601, f"{method} is not supported by AstrBot")
