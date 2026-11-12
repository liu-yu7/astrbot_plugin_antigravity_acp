"""Loopback Streamable HTTP MCP server that exposes AstrBot's tools to the
official Antigravity agent and parks each ``tools/call`` until AstrBot has run
the tool through its own pipeline.

Calls answer with a server-sent-event stream so response headers leave
immediately; the agent's HTTP client otherwise abandons a request after 60
seconds without headers. Comment frames and MCP progress notifications keep
long tool runs visibly alive.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable, Protocol

from aiohttp import web

MCP_PROTOCOL_VERSION = "2025-06-18"
CONVERSATION_META_KEY = "antigravity.google/conversation_id"


class BridgeRouter(Protocol):
    """Owner-side routing of tool listings and calls."""

    def list_tools(self, tenant_key: str) -> list[dict]: ...

    async def call_tool(self, tenant_key: str, conversation_id: str | None, name: str, arguments: dict) -> dict: ...

    def authorize(self, tenant_key: str, bearer_token: str | None) -> bool: ...


def error_result(text: str) -> dict:
    """MCP tool result reporting a failure to the agent."""
    return {"content": [{"type": "text", "text": text}], "isError": True}


class McpBridge:
    """Loopback MCP endpoint shared by every ACP session of one runtime."""

    def __init__(self, router: BridgeRouter, *, keepalive: float = 15.0, log: Callable[[str], None] | None = None) -> None:
        self._router = router
        self._keepalive = keepalive
        self._log = log
        self._runner: web.AppRunner | None = None
        self._port = 0
        #: Tenant keys whose agent completed the MCP ``initialize`` handshake.
        self._handshakes: set[str] = set()
        self._calls: set[asyncio.Task] = set()

    @property
    def listening(self) -> bool:
        return self._runner is not None

    @property
    def url(self) -> str:
        """Base URL of the endpoint; append ``/<tenant>`` for a session-scoped endpoint."""
        return f"http://127.0.0.1:{self._port}/mcp"

    async def start(self) -> None:
        """Bind the loopback listener on an ephemeral port."""
        if self._runner is not None:
            return
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/mcp", self._handle_root)
        app.router.add_route("*", "/mcp/{tenant}", self._handle)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        self._port = int(runner.addresses[0][1])
        self._runner = runner

    async def close(self) -> None:
        """Close every open call stream and the listener."""
        runner, self._runner = self._runner, None
        for task in list(self._calls):
            task.cancel()
        self._calls.clear()
        self._handshakes.clear()
        if runner is not None:
            await runner.cleanup()

    def has_handshake(self, tenant_key: str | None) -> bool:
        """Whether one tenant's agent proved it holds the session's bearer credential."""
        return tenant_key is not None and tenant_key in self._handshakes

    def forget_tenant(self, tenant_key: str | None) -> None:
        if tenant_key is not None:
            self._handshakes.discard(tenant_key)

    # ---------------------------------------------------------------- handlers

    async def _handle_root(self, _request: web.Request) -> web.StreamResponse:
        # The root path is tenant-less and never exposes tools/list or tools/call.
        return web.Response(status=401)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        tenant_key = request.match_info.get("tenant") or ""
        authorization = request.headers.get("Authorization", "")
        token = authorization[len("Bearer "):] if authorization.startswith("Bearer ") else None
        if not tenant_key or not self._router.authorize(tenant_key, token):
            return web.Response(status=401)
        if request.method == "DELETE":
            return web.Response(status=200)
        if request.method != "POST":
            return web.Response(status=405, headers={"Allow": "POST, DELETE"})
        try:
            body = json.loads(await request.read())
        except Exception:  # noqa: BLE001 - an unparsable body cannot be answered with a request id
            return self._json(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
        messages = body if isinstance(body, list) else [body]
        requests = [
            m for m in messages if isinstance(m, dict) and isinstance(m.get("method"), str) and m.get("id") is not None
        ]
        if not requests:
            return web.Response(status=202)  # notifications and client responses only
        call = next((r for r in requests if r["method"] == "tools/call"), None)
        if call is not None and len(requests) == 1:
            return await self._handle_call(request, call, tenant_key)
        replies = [self._answer(r, tenant_key) for r in requests]
        return self._json(200, replies if isinstance(body, list) else replies[0])

    @staticmethod
    def _json(status: int, value: Any) -> web.Response:
        return web.Response(status=status, text=json.dumps(value, ensure_ascii=False), content_type="application/json")

    def _answer(self, request: dict, tenant_key: str) -> dict:
        rid = request.get("id")
        method = request["method"]

        def reply(result: Any) -> dict:
            return {"jsonrpc": "2.0", "id": rid, "result": result}

        if method == "initialize":
            params = request.get("params") if isinstance(request.get("params"), dict) else {}
            version = params.get("protocolVersion")
            # The authorized handshake is the only accepted loading-probe evidence.
            self._handshakes.add(tenant_key)
            return reply(
                {
                    "protocolVersion": version if isinstance(version, str) else MCP_PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": "astrbot", "version": "0.1.0"},
                    "instructions": "Tools of the AstrBot chat session. Every call is executed by AstrBot under its own permission policy.",
                }
            )
        if method == "ping":
            return reply({})
        if method == "tools/list":
            return reply({"tools": self._router.list_tools(tenant_key)})
        if method == "resources/list":
            return reply({"resources": []})
        if method == "resources/templates/list":
            return reply({"resourceTemplates": []})
        if method == "prompts/list":
            return reply({"prompts": []})
        if method == "tools/call":
            return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32600, "message": "tools/call must be sent alone"}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}}

    async def _handle_call(self, request: web.Request, call: dict, tenant_key: str) -> web.StreamResponse:
        params = call.get("params") if isinstance(call.get("params"), dict) else {}
        meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
        name = params.get("name") if isinstance(params.get("name"), str) else ""
        arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
        progress_token = meta.get("progressToken")
        conversation_id = meta.get(CONVERSATION_META_KEY)
        conversation_id = conversation_id if isinstance(conversation_id, str) else None

        response = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
            },
        )
        await response.prepare(request)
        task = asyncio.create_task(
            self._router.call_tool(tenant_key, conversation_id, name, arguments), name="agy-acp:mcp-call"
        )
        self._calls.add(task)
        task.add_done_callback(self._calls.discard)
        ticks = 0
        try:
            while True:
                done, _pending = await asyncio.wait({task}, timeout=self._keepalive)
                if done:
                    break
                ticks += 1
                await response.write(b": keepalive\n\n")
                if isinstance(progress_token, (str, int)) and not isinstance(progress_token, bool):
                    await response.write(
                        self._event(
                            {
                                "jsonrpc": "2.0",
                                "method": "notifications/progress",
                                "params": {
                                    "progressToken": progress_token,
                                    "progress": ticks,
                                    "message": "AstrBot is running the tool",
                                },
                            }
                        )
                    )
            try:
                result = task.result()
            except asyncio.CancelledError:
                result = error_result("The tool call was cancelled.")
            except Exception as exc:  # noqa: BLE001
                result = error_result(str(exc))
            await response.write(self._event({"jsonrpc": "2.0", "id": call.get("id"), "result": result}))
            await response.write_eof()
        except asyncio.CancelledError:
            task.cancel()
            raise
        except (ConnectionError, RuntimeError) as exc:
            # The agent disconnected or cancelled the call; the parked call itself stays owned by the turn.
            task.cancel()
            if self._log is not None:
                self._log(f"mcp: call {name} ended after the agent disconnected ({exc})")
        return response

    @staticmethod
    def _event(value: Any) -> bytes:
        return f"event: message\ndata: {json.dumps(value, ensure_ascii=False)}\n\n".encode("utf-8")
