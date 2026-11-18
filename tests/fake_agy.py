#!/usr/bin/env python3
"""Scripted stand-in for ``agy acp`` used by the tests (stdlib only).

Speaks ACP v1 over stdio, connects to the session's HTTP MCP server like the
official agent, and follows directives embedded in prompt text:

    CALL <tool> <json>     report the tool call, ask permission, call one MCP tool
    PCALL <tool> <json> ;; <tool> <json>   call several tools concurrently
    DENYCALL <tool> <json> a native-looking tool whose rawInput merely embeds the name
    SLEEP <ms>             wait before ending the turn
    MAXTOK                 end the first segment with max_tokens
    NCALL <tool> <json>    use agy 1.5.8 explicit-name MCP metadata
    NOTICE                 stream an agent-side transport retry notice first
    EXECUTION_FAILURE      stream a split terminal error with an end_turn result

The lifecycle log (``$FAKE_AGY_LOG``, JSONL) records session/new, session/load,
session/close and every prompt, so tests can prove session reuse.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import urllib.request

LOG = os.environ.get("FAKE_AGY_LOG")

ALL_MODELS = [
    {"value": "gemini-4-argon", "name": "Gemini 4 Argon"},
    {"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"},
    {"value": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"},
]
AVAILABLE = [
    {"modelId": f"{model}-{level}", "name": f"{model} ({level})"}
    for model in ("gemini-4-argon", "gemini-3.8-flash")
    for level in ("low", "medium", "high")
] + [{"modelId": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"}]


def log_event(event: dict) -> None:
    if LOG:
        with open(LOG, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class FakeAgy:
    def __init__(self) -> None:
        self.sessions: dict[str, dict] = {}
        self.next_session = 1
        self.rpc_id = 100
        self.tool_seq = 0
        self.pending_requests: dict[int, asyncio.Future] = {}

    # ------------------------------------------------------------- transport

    def update(self, session: dict, update: dict) -> None:
        send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session["id"], "update": update}})

    def config_options(self, session: dict) -> list[dict]:
        return [
            {
                "id": "model",
                "name": "Model",
                "category": "model",
                "type": "select",
                "currentValue": session["model"],
                "options": ALL_MODELS,
            },
            {
                "id": "thought_level",
                "name": "Thinking Level",
                "category": "thought_level",
                "type": "select",
                "currentValue": session["level"],
                "options": [{"value": "low", "name": "Low"}, {"value": "medium", "name": "Medium"}, {"value": "high", "name": "High"}],
            },
        ]

    async def request(self, method: str, params: dict, timeout: float = 60.0) -> dict:
        self.rpc_id += 1
        rid = self.rpc_id
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self.pending_requests[rid] = future
        send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending_requests.pop(rid, None)

    def resolve(self, message: dict) -> bool:
        future = self.pending_requests.get(message.get("id"))
        if future is None or future.done():
            return False
        future.set_result(message.get("result") or {})
        return True

    async def mcp(self, server: dict, body: dict) -> dict | None:
        def call() -> dict | None:
            headers = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
            for header in server.get("headers") or []:
                headers[str(header.get("name", "")).lower()] = str(header.get("value", ""))
            request = urllib.request.Request(
                server["url"], data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                raw = response.read().decode("utf-8")
                content_type = response.headers.get("content-type", "")
            if "text/event-stream" in content_type:
                for block in raw.split("\n\n"):
                    for line in block.split("\n"):
                        if line.startswith("data: "):
                            event = json.loads(line[6:])
                            if event.get("id") == body.get("id"):
                                return event
                return None
            return json.loads(raw) if raw.strip() else None

        return await asyncio.to_thread(call)

    # ------------------------------------------------------------ tool calls

    async def run_tool_call(self, session: dict, name: str, args: dict, *, structured: bool = True, title: str | None = None) -> str:
        self.tool_seq += 1
        call_id = f"tc{self.tool_seq}"
        session["last_tool_call_id"] = call_id
        tool_call = {
            "toolCallId": call_id,
            "title": title or f"mcp_astrbot_{name}",
            "kind": "other",
            "rawInput": args,
        }
        if structured == "named":
            tool_call["name"] = f"mcp_astrbot_{name}"
            tool_call["rawInput"] = {"toolSummary": "Diagnostic display summary", **args}
            args = dict(tool_call["rawInput"])
        update = {"sessionUpdate": "tool_call", **tool_call}
        if structured is True:
            update.update({"mcpServerName": "astrbot", "mcpToolName": name})
        self.update(session, update)
        permission_request = dict(tool_call)
        if structured is True:
            permission_request.update({"mcpServerName": "astrbot", "mcpToolName": name})
        outcome = await self.request(
            "session/request_permission",
            {
                "sessionId": session["id"],
                "toolCall": permission_request,
                "options": [
                    {"optionId": "allow-once", "name": "Allow once", "kind": "allow_once"},
                    {"optionId": "allow-always", "name": "Always allow", "kind": "allow_always"},
                    {"optionId": "reject-once", "name": "Reject", "kind": "reject_once"},
                ],
            },
        )
        selected = str((outcome.get("outcome") or {}).get("optionId") or "cancelled")
        if not selected.startswith("allow"):
            self.update(session, {"sessionUpdate": "tool_call_update", "toolCallId": call_id, "status": "refused"})
            return f"PERMISSION_{'REJECTED' if selected.startswith('reject') else 'CANCELLED'}"
        result = await self.mcp(
            session["mcp"],
            {
                "jsonrpc": "2.0",
                "id": self.rpc_id + 1,
                "method": "tools/call",
                "params": {
                    "name": name,
                    "arguments": args,
                    "_meta": {"progressToken": f"p{self.rpc_id}", "antigravity.google/conversation_id": session["id"]},
                },
            },
        )
        self.rpc_id += 1
        if result is None or "result" not in result:
            return f"TOOL_ERROR:{json.dumps(result)[:120]}"
        content = result["result"].get("content") or []
        text = "".join(str(part.get("text") or f"[{part.get('type')}]") for part in content)
        # Keep waiting through the relay mechanism like the real agent.
        for _hop in range(5):
            match = re.search(r'astrbot_await_result with \{"call_id":"([^"]+)"\}', text)
            if match is None:
                break
            text = await self.run_tool_call(session, "astrbot_await_result", {"call_id": match.group(1)})
        return text

    # ---------------------------------------------------------------- prompts

    async def run_prompt(self, session: dict, text: str) -> dict:
        session["cancelled"] = False
        if text.split("</astrbot_history>")[-1].strip() == "RESOURCE_FAILURE":
            for part in ("\n⚠ API error (attempt 1): RESO", "URCE_EXHAUSTED (code 429): Resource exhausted.\n"):
                self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}})
            await asyncio.sleep(2)
            return {"stopReason": "end_turn", "usage": {"totalTokens": 0}}
        if text.split("</astrbot_history>")[-1].strip() == "EXECUTION_FAILURE":
            for part in (chr(10) + "⚠ Agent execution ", "terminated due ", "to error." + chr(10)):
                self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": part}})
            return {"stopReason": "end_turn", "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}}
        self.update(session, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": f"thinking({session['level']})"}})
        if "NOTICE" in text:
            self.update(
                session,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "\n⚠ API error (attempt 1): request failed: EOF\n"}},
            )
        output: list[str] = []
        # Like a real model, act only on the live input after a replayed history block.
        live = text.split("</astrbot_history>")[-1]
        for line in live.split("\n"):
            if line.startswith("CALL "):
                _, name, rest = line.split(" ", 2)
                result = await self.run_tool_call(session, name, json.loads(rest))
                output.append(f"RESULT({name})={result}")
            elif line.startswith("NCALL "):
                _, name, rest = line.split(" ", 2)
                result = await self.run_tool_call(session, name, json.loads(rest), structured="named")
                output.append(f"RESULT({name})={result}")
            elif line.startswith("DENYCALL "):
                _, name, rest = line.split(" ", 2)
                result = await self.run_tool_call(session, name, json.loads(rest), structured=False, title=name)
                output.append(f"RESULT({name})={result}")
            elif line.startswith("PCALL "):
                calls = [part.strip().split(" ", 1) for part in line[6:].split(" ;; ")]
                results = await asyncio.gather(*(self.run_tool_call(session, name, json.loads(args)) for name, args in calls))
                output.extend(f"RESULT({name})={result}" for (name, _args), result in zip(calls, results))
            elif line.startswith("SLEEP "):
                await asyncio.sleep(int(line[6:]) / 1000)
            if session.get("cancelled"):
                return {"stopReason": "cancelled"}
        if session.get("cancelled"):
            return {"stopReason": "cancelled"}
        if "MAXTOK" in text and not session.get("continued"):
            session["continued"] = True
            self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "part-one "}})
            return {"stopReason": "max_tokens", "usage": {"inputTokens": 10, "outputTokens": 5, "totalTokens": 15}}
        reply = " | ".join(output) if output else f"echo:{session['model']}:{session['level']}:{text[-60:]}"
        self.update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": reply}})
        return {
            "stopReason": "end_turn",
            "usage": {"inputTokens": 600, "outputTokens": 50, "cachedReadTokens": 400, "cachedWriteTokens": 0, "thoughtTokens": 20, "totalTokens": 1050},
        }

    # --------------------------------------------------------------- dispatch

    async def handle(self, message: dict) -> None:
        if "method" not in message:
            self.resolve(message)
            return
        method = message.get("method")
        params = message.get("params") or {}
        rid = message.get("id")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {"protocolVersion": 1, "agentCapabilities": {"loadSession": True, "mcpCapabilities": {"http": True}}, "agentInfo": {"name": "fake-agy", "version": "1.5.5"}}})
            return
        if method == "session/cancel":
            session = self.sessions.get(str(params.get("sessionId")))
            if session is not None:
                session["cancelled"] = True
            return
        if method == "session/new":
            session = {
                "id": f"s{self.next_session}",
                "model": ALL_MODELS[0]["value"],
                "level": "medium",
                "mcp": (params.get("mcpServers") or [None])[0],
                "system": (params.get("_meta") or {}).get("antigravity", {}).get("systemPrompt", {}).get("append", ""),
                "tools": [],
            }
            self.next_session += 1
            self.sessions[session["id"]] = session
            log_event({"op": "new", "session": session["id"], "system": session["system"][:6000], "cwd": params.get("cwd")})
            if session["mcp"]:
                await self.mcp(session["mcp"], {"jsonrpc": "2.0", "id": self.rpc_id + 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "fake", "version": "0"}}})
                self.rpc_id += 1
                listing = await self.mcp(session["mcp"], {"jsonrpc": "2.0", "id": self.rpc_id + 1, "method": "tools/list", "params": {}})
                self.rpc_id += 1
                session["tools"] = [tool["name"] for tool in ((listing or {}).get("result") or {}).get("tools", [])]
                log_event({"op": "tools", "session": session["id"], "tools": session["tools"]})
            send({"jsonrpc": "2.0", "id": rid, "result": {"sessionId": session["id"], "configOptions": self.config_options(session), "models": {"availableModels": AVAILABLE, "currentModelId": f"{session['model']}-{session['level']}"}}})
            return
        if method == "session/set_config_option":
            session = self.sessions.get(str(params.get("sessionId")))
            if session is None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32004, "message": "session not found"}})
                return
            if params.get("configId") == "model":
                values = [model["value"] for model in ALL_MODELS]
                if params.get("value") not in values:
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"unknown model {params.get('value')}"}})
                    return
                session["model"] = params["value"]
            elif params.get("configId") == "thought_level":
                if session["model"].startswith("gpt-oss"):
                    send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32602, "message": f"Thinking level \"{params.get('value')}\" is not offered for model \"{session['model']}\""}})
                    return
                session["level"] = params.get("value")
            send({"jsonrpc": "2.0", "id": rid, "result": {"configOptions": self.config_options(session)}})
            return
        if method == "session/prompt":
            session = self.sessions.get(str(params.get("sessionId")))
            if session is None:
                send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32004, "message": "session not found"}})
                return
            text = "".join(str(block.get("text") or "") for block in params.get("prompt") or [] if block.get("type") == "text")
            images = sum(1 for block in params.get("prompt") or [] if block.get("type") == "image")
            log_event({"op": "prompt", "session": session["id"], "text": text, "images": images, "model": session["model"], "level": session["level"], "tools": session["tools"]})
            result = await self.run_prompt(session, text)
            send({"jsonrpc": "2.0", "id": rid, "result": result})
            return
        if method == "session/close":
            log_event({"op": "close", "session": params.get("sessionId")})
            send({"jsonrpc": "2.0", "id": rid, "result": {}})
            return
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"unknown {method}"}})


async def reader(fake: FakeAgy) -> None:
    loop = asyncio.get_running_loop()
    while True:
        line = await loop.run_in_executor(None, sys.stdin.readline)
        if not line:
            break
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except ValueError:
            continue
        asyncio.create_task(fake.handle(message))


def main() -> int:
    if "--version" in sys.argv:
        print("1.5.5")
        return 0
    if "acp" not in sys.argv:
        print("fake-agy: unsupported invocation", file=sys.stderr)
        return 2
    fake = FakeAgy()
    try:
        asyncio.run(reader(fake))
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
