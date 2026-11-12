"""Newline-delimited JSON-RPC 2.0 peer over a pair of asyncio byte streams.

ACP (Agent Client Protocol) v1 frames one JSON message per line on stdio. The
peer multiplexes outgoing requests, dispatches incoming requests and
notifications, and fails every pending request when the transport closes.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from typing import Any, Callable


class JsonRpcRemoteError(Exception):
    """A request the remote peer answered with a JSON-RPC error."""

    def __init__(self, method: str, code: int, message: str, data: Any = None) -> None:
        super().__init__(f"{method}: {message}")
        self.method = method
        self.code = code
        self.rpc_message = message
        self.data = data


class JsonRpcClosedError(Exception):
    """The transport closed before a pending request settled."""


class JsonRpcPeer:
    """Bidirectional newline-delimited JSON-RPC 2.0 endpoint."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        on_request: Callable[[str, Any], Any],
        on_notification: Callable[[str, Any], None],
        on_close: Callable[[Exception], None] | None = None,
        on_protocol_error: Callable[[str], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_request = on_request
        self._on_notification = on_notification
        self._on_close = on_close
        self._on_protocol_error = on_protocol_error
        self._next_id = 1
        self._pending: dict[int, tuple[str, asyncio.Future]] = {}
        self._closed: Exception | None = None
        self._write_lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._reader_task: asyncio.Task | None = None

    def start(self) -> None:
        """Begin reading frames from the remote peer."""
        if self._reader_task is None:
            self._reader_task = asyncio.create_task(self._read_loop(), name="agy-acp:jsonrpc-read")

    @property
    def is_closed(self) -> bool:
        return self._closed is not None

    async def request(self, method: str, params: Any, timeout: float | None = None) -> Any:
        """Send one request and await its result."""
        if self._closed is not None:
            raise JsonRpcClosedError(f"{method}: transport closed ({self._closed})")
        rid = self._next_id
        self._next_id += 1
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = (method, fut)
        try:
            await self._write({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
            if timeout is not None and timeout > 0:
                try:
                    return await asyncio.wait_for(fut, timeout)
                except asyncio.TimeoutError:
                    raise TimeoutError(f"{method}: no response within {timeout:g}s") from None
            return await fut
        finally:
            self._pending.pop(rid, None)
            if not fut.done():
                fut.cancel()

    def notify(self, method: str, params: Any) -> None:
        """Send one notification (best effort)."""
        if self._closed is not None:
            return
        try:
            self._writer.write(self._encode({"jsonrpc": "2.0", "method": method, "params": params}))
        except Exception as exc:  # noqa: BLE001 - any transport failure closes the link
            self.close(exc)

    def close(self, error: Exception) -> None:
        """Close the peer and fail every pending request."""
        if self._closed is not None:
            return
        self._closed = error
        for _rid, (method, fut) in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(JsonRpcClosedError(f"{method}: {error}"))
        self._pending.clear()
        if self._reader_task is not None and self._reader_task is not asyncio.current_task():
            self._reader_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        if self._on_close is not None:
            try:
                self._on_close(error)
            except Exception:  # noqa: BLE001 - close observers must not break shutdown
                pass

    @staticmethod
    def _encode(message: dict) -> bytes:
        return (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")

    async def _write(self, message: dict) -> None:
        data = self._encode(message)
        async with self._write_lock:
            if self._closed is not None:
                raise JsonRpcClosedError(f"transport closed ({self._closed})")
            try:
                self._writer.write(data)
                await self._writer.drain()
            except Exception as exc:  # noqa: BLE001
                self.close(exc)
                raise JsonRpcClosedError(str(exc)) from exc

    def _protocol_error(self, detail: str) -> None:
        if self._on_protocol_error is not None:
            try:
                self._on_protocol_error(detail)
            except Exception:  # noqa: BLE001
                pass

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                line = line.strip()
                if line:
                    self._on_line(line)
            self.close(JsonRpcClosedError("ACP stream ended"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - e.g. a frame larger than the stream limit
            self.close(exc)

    def _on_line(self, line: bytes) -> None:
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError("not an object")
        except Exception:  # noqa: BLE001 - a malformed frame is dropped, the link stays usable
            self._protocol_error(f"unparsable frame: {line[:200]!r}")
            return
        method = message.get("method")
        mid = message.get("id")
        if not isinstance(method, str):
            if not isinstance(mid, int) or isinstance(mid, bool):
                self._protocol_error(f"response without numeric id: {line[:200]!r}")
                return
            entry = self._pending.pop(mid, None)
            if entry is None:
                return
            req_method, fut = entry
            if fut.done():
                return
            error = message.get("error")
            if error is not None:
                err = error if isinstance(error, dict) else {}
                code = err.get("code")
                msg = err.get("message")
                fut.set_exception(
                    JsonRpcRemoteError(
                        req_method,
                        code if isinstance(code, int) else -32603,
                        msg if isinstance(msg, str) else "unknown error",
                        err.get("data"),
                    )
                )
            else:
                fut.set_result(message.get("result"))
            return
        if mid is None:
            try:
                self._on_notification(method, message.get("params"))
            except Exception as exc:  # noqa: BLE001
                self._protocol_error(f"notification handler for {method} failed: {exc!r}")
            return
        task = asyncio.create_task(self._answer(mid, method, message.get("params")), name="agy-acp:jsonrpc-answer")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _answer(self, mid: Any, method: str, params: Any) -> None:
        try:
            try:
                result = self._on_request(method, params)
                if inspect.isawaitable(result):
                    result = await result
                reply: dict = {"jsonrpc": "2.0", "id": mid, "result": result}
            except JsonRpcRemoteError as exc:
                reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": exc.code, "message": exc.rpc_message}}
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                reply = {"jsonrpc": "2.0", "id": mid, "error": {"code": -32603, "message": str(exc)}}
            await self._write(reply)
        except JsonRpcClosedError:
            pass
