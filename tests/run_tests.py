#!/usr/bin/env python3
"""End-to-end tests of the ACP runtime against ``tests/fake_agy.py``.

Needs only ``aiohttp`` besides the standard library, so run it with AstrBot's
interpreter, e.g.::

    ~/.local/share/uv/tools/astrbot/bin/python tests/run_tests.py
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agy_acp import history as H  # noqa: E402
from agy_acp.context import ContextSettings  # noqa: E402
from agy_acp.notices import classify_agent_notice  # noqa: E402
from agy_acp.preflight import build_env  # noqa: E402
from agy_acp.safety import ACP_SAFE_POLICY, ASTRBOT_SAFE_MODE_PROMPT, adapt_astrbot_safety_prompt  # noqa: E402
from agy_acp.runtime import (  # noqa: E402
    AgyError,
    AntigravityRuntime,
    ChatRequest,
    RuntimeConfig,
    resolve_model_choice,
)

FAKE = os.path.join(ROOT, "tests", "fake_agy.py")
TOOLS = [
    {
        "name": "get_weather",
        "description": "Get the weather of a city.",
        "inputSchema": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]},
    },
    {
        "name": "roll",
        "description": "Roll a dice.",
        "inputSchema": {"type": "object", "properties": {"sides": {"type": "integer"}}},
    },
]


class Log:
    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def info(self, message: str, **_kw) -> None:
        self.lines.append(("info", message))

    def warning(self, message: str, **_kw) -> None:
        self.lines.append(("warn", message))

    def debug(self, message: str, **_kw) -> None:
        self.lines.append(("debug", message))

    def error(self, message: str, **_kw) -> None:
        self.lines.append(("error", message))


def user(text: str, images: list[str] | None = None) -> H.Msg:
    return H.Msg(role="user", text=text, images=images or [], stable_text=text, stable_images=len(images or []))


def assistant(text: str = "", calls=None) -> H.Msg:
    refs = [H.ToolCallRef(id=c.id, name=c.name, arguments=json.dumps(c.arguments)) for c in calls or []]
    return H.Msg(role="assistant", text=text, stable_text=text, tool_calls=refs)


def tool(call_id: str, text: str) -> H.Msg:
    return H.Msg(role="tool", text=text, stable_text=text, tool_call_id=call_id)


async def resolve_image(ref: str) -> H.ResolvedImage | None:
    return H.ResolvedImage(data="aGk=", mime_type="image/png") if ref.startswith("data:") else None


def request(key, messages, *, system="SYS", tools=None, model="gemini-3.8-flash-high", effort="") -> ChatRequest:
    return ChatRequest(
        key=key, system=system, messages=messages, tools=tools or [], model=model, effort=effort, resolve_image=resolve_image
    )


async def collect(runtime: AntigravityRuntime, req: ChatRequest) -> dict:
    out = {"text": "", "reasoning": "", "calls": None, "finish": None}
    async for kind, payload in runtime.stream(req):
        if kind == "text":
            out["text"] += payload
        elif kind == "reasoning":
            out["reasoning"] += payload
        elif kind == "tool_calls":
            out["calls"] = payload
        elif kind == "finish":
            out["finish"] = payload
    return out


class Harness:
    def __init__(self) -> None:
        self.tmp = tempfile.mkdtemp(prefix="agy-acp-test-")
        self.log_path = os.path.join(self.tmp, "fake.jsonl")
        os.environ["FAKE_AGY_LOG"] = self.log_path
        os.chmod(FAKE, 0o755)
        self.log = Log()
        self.config = RuntimeConfig(
            home=os.path.join(self.tmp, "home"),
            agy_path=FAKE,
            request_timeout=30,
            relay_after=1.5,
            idle_session_ttl=3600,
            context_settings=ContextSettings(patch_script=os.path.join(ROOT, "tests", "fake_context_check.py")),
        )
        self.runtime = AntigravityRuntime(self.config, self.log)

    def events(self, since: int = 0) -> list[dict]:
        try:
            with open(self.log_path, encoding="utf-8") as handle:
                return [json.loads(line) for line in handle if line.strip()][since:]
        except FileNotFoundError:
            return []

    def mark(self) -> int:
        return len(self.events())

    async def close(self) -> None:
        await self.runtime.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


# ------------------------------------------------------------------ unit tests


async def test_notices(_h: Harness) -> None:
    check(classify_agent_notice("\n⚠ API error (attempt 1): request failed: EOF\n").kind == "drop", "retry notice dropped")
    check(classify_agent_notice("⚠").kind == "hold", "lone warning mark is held")
    check(classify_agent_notice("⚠ API err").kind == "hold", "partial retry notice is held")
    check(classify_agent_notice("Hello there").kind == "emit", "model text is emitted")
    verdict = classify_agent_notice("There was a network issue connecting to the server, please try again.", True)
    check(verdict.kind == "error" and verdict.code == "TRANSPORT", "network notice becomes a transport error")
    verdict = classify_agent_notice("⚠ Our servers are experiencing high traffic right now, please try again in a minute.")
    check(verdict.kind == "error" and verdict.code == "RATE_LIMIT", "traffic notice becomes a rate-limit error")
    cut = "⚠ Your previous response was cut off because it exceeded the output token limit\n"
    check(classify_agent_notice(cut).kind in ("drop", "hold"), "output-limit notice is not emitted")
    quoted = "There was a network issue connecting to the server, please try again. That sentence is what agy prints."
    check(classify_agent_notice(quoted, True).kind == "emit", "a reply quoting the notice is emitted")
    terminal = "Agent execution terminated due to error."
    for marked in (terminal, chr(10) + "⚠ " + terminal + chr(10)):
        verdict = classify_agent_notice(marked, True)
        check(verdict.kind == "error" and verdict.code == "UPSTREAM_EXECUTION", "execution failure is not successful text")
    check(classify_agent_notice("⚠ Agent execution term").kind == "hold", "split execution notice held")
    check(classify_agent_notice(terminal + " This is a quoted diagnostic.", True).kind == "emit", "explained notice remains model text")


async def test_resource_notice(h: Harness) -> None:
    notice = "\n⚠ API error (attempt 1): RESOURCE_EXHAUSTED (code 429): Resource exhausted.\n"
    verdict = classify_agent_notice(notice)
    check(verdict.kind == "error" and verdict.code == "RESOURCE_EXHAUSTED", "explicit upstream 429 retains its code")
    check(classify_agent_notice("⚠ API error (attempt 1): RESO").kind == "hold", "split retry body held")
    check(classify_agent_notice("A quoted RESOURCE_EXHAUSTED (code 429) error.", True).kind == "emit", "model quotation is not a rate limit")
    mark = h.mark()
    began = asyncio.get_running_loop().time()
    try:
        await collect(h.runtime, request("resource-failure", [user("RESOURCE_FAILURE")]))
        raise AssertionError("429 was treated as model output")
    except AgyError as exc:
        check(exc.code == "RESOURCE_EXHAUSTED" and not exc.retryable, "429 does not resubmit rejected request")
    check(asyncio.get_running_loop().time() - began < 1.5, "429 reported before native terminal failure")
    check(len([e for e in h.events(mark) if e["op"] == "prompt"]) == 1, "one prompt for rejected request")
    result = await collect(h.runtime, request("resource-failure", [user("normal after resource limit")]))
    check(result["text"].endswith("normal after resource limit"), "unrelated next request is not globally disabled")


async def test_managed_update_environment(_h: Harness) -> None:
    env = build_env("/tmp/test-home", "http://127.0.0.1:7890", {"AGY_CLI_DISABLE_AUTO_UPDATE": "0", "CUSTOM_SETTING": "kept"})
    check(env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true", "managed CLI auto-update disabled even with inherited override")
    check(env["CUSTOM_SETTING"] == "kept", "unrelated environment preserved")
    check(env["HTTPS_PROXY"] == "http://127.0.0.1:7890", "proxy preserved")


async def test_safety_policy_compatibility(_h: Harness) -> None:
    suffix = chr(10) + "# Persona: unchanged" + chr(10) + "Extra custom safety rule: keep this too."
    original = ASTRBOT_SAFE_MODE_PROMPT + suffix
    adapted = adapt_astrbot_safety_prompt(original)
    check(adapted == ACP_SAFE_POLICY + suffix, "only exact default prefix adapted; all suffix bytes retained")
    check(adapt_astrbot_safety_prompt(adapted) == adapted, "safety adaptation is idempotent")
    for constraint in ("sexual", "violent", "extremist", "hateful", "illegal", "harmful", "healthy", "constructive", "positive", "real-world politics", "sensitive controversial", "subordinate", "circumvent", "decline politely", "safe alternative"):
        check(constraint in ACP_SAFE_POLICY, "safety constraint retained: " + constraint)
    custom = original.replace("Avoid sexual", "Avoid explicit sexual", 1)
    check(adapt_astrbot_safety_prompt(custom) == custom, "custom safety policies not guessed or stripped")
    quote = "Quoted text: " + original
    check(adapt_astrbot_safety_prompt(quote) == quote, "non-prefix quoted template unchanged")
    crlf = ASTRBOT_SAFE_MODE_PROMPT.replace(chr(10), chr(13) + chr(10))
    check(adapt_astrbot_safety_prompt(crlf + suffix) == ACP_SAFE_POLICY + suffix, "CRLF prefix supported without changing suffix")
    system, msgs = H.normalize_request(system_prompt=original, prompt="user input unchanged")
    check(system == adapted and msgs[0].text == "user input unchanged", "classic provider path adapted")
    system, msgs = H.normalize_request(contexts=[{"role": "system", "content": original}, {"role": "user", "content": ASTRBOT_SAFE_MODE_PROMPT}])
    check(system == adapted and msgs[0].text == ASTRBOT_SAFE_MODE_PROMPT, "agent system adapted; user content unchanged")


async def test_local_prompt_compatibility(_h: Harness) -> None:
    from agy_acp.prompts import ASTRBOT_LOCAL_MODE_PROMPT, ACP_LOCAL_MODE_POLICY, adapt_astrbot_local_prompt
    original = "ROLE AND SKILLS UNCHANGED\n" + ASTRBOT_LOCAL_MODE_PROMPT + "\nTOOLS UNCHANGED"
    expected = "ROLE AND SKILLS UNCHANGED\n" + ACP_LOCAL_MODE_POLICY + "\nTOOLS UNCHANGED"
    check(adapt_astrbot_local_prompt(original) == expected, "exact host paragraph adapted; surrounding text unchanged")
    check(adapt_astrbot_local_prompt(expected) == expected, "host adaptation is idempotent")
    for constraint in ("file and execution permissions", "Linux", "Unix-like", "POSIX-compatible", "managed session", "astrbot_shell_session", "raw text", "complete lines", "interrupt", "terminate", "write_line", "real line feed", "never", "`&`", "`nohup`", "detachment wrapper"):
        check(constraint in ACP_LOCAL_MODE_POLICY, "host constraint retained: " + constraint)
    custom = original.replace("Use POSIX-compatible", "Only use POSIX-compatible", 1)
    check(adapt_astrbot_local_prompt(custom) == custom, "custom host instructions not guessed")
    quoted = "Quoted template: " + ASTRBOT_LOCAL_MODE_PROMPT
    check(adapt_astrbot_local_prompt(quoted) == quoted, "inline quoted template unchanged")
    windows = original.replace("Linux", "Windows")
    check(adapt_astrbot_local_prompt(windows) == windows, "unverified operating system not transformed")
    system, messages = H.normalize_request(contexts=[{"role":"system","content":original},{"role":"user","content":original}])
    check(system == expected and messages[0].text == original, "system only adapted; user input unchanged")


async def test_history(_h: Harness) -> None:
    contexts = [
        {"role": "system", "content": "S"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "hi"},
                {"type": "text", "text": "tmp reminder", "_no_save": True},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,aGk="}},
            ],
        },
        {"role": "assistant", "content": "yo", "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "f", "arguments": "{\"a\":1}"}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "r"},
        {"role": "_checkpoint", "content": {"id": "x"}},
    ]
    system, messages = H.normalize_request(contexts=contexts, prompt="next", system_prompt="P")
    check(system == "P\n\nS", f"system merged: {system!r}")
    check([m.role for m in messages] == ["user", "assistant", "tool", "user"], "roles normalized, checkpoint skipped")
    first = messages[0]
    check(first.text == "hi\ntmp reminder" and first.stable_text == "hi", "transient part kept for the model, not for identity")
    check(first.images == ["data:image/png;base64,aGk="] and first.stable_images == 1, "image captured")
    check(messages[1].tool_calls[0].name == "f" and messages[1].tool_calls[0].arguments == '{"a":1}', "tool call parsed")
    stored = H.normalize_request(contexts=[{"role": "user", "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "file:///x.png"}}]}])[1][0]
    check(H.fingerprint(first) == H.fingerprint(stored), "fingerprint survives dropping the transient part")
    check(H.fingerprint(messages[1]) == "A" and H.fingerprint(messages[2]) == "T:c1", "assistant/tool identities")

    check(H.system_delta("a\nb", "a\nb") == "", "unchanged system")
    check(H.system_delta("persona\nCurrent datetime: 10:00", "persona\nCurrent datetime: 10:01") == "Current datetime: 10:01", "small delta")
    check(H.system_delta("x" * 50, "y" * 5000) is None, "large change asks for a rebuild")

    big = {"name": "t", "description": "d " * 2000, "inputSchema": {"type": "object", "properties": {"q": {"type": "string", "description": "z " * 500}}, "required": ["q"]}}
    compact = H.compact_bridge_tool(big)
    check(len(json.dumps(compact).encode()) <= H.MAX_EAGER_BRIDGE_TOOL_BYTES, "tool compacted under the eager limit")
    check(compact["inputSchema"]["required"] == ["q"] and "q" in compact["inputSchema"]["properties"], "schema shape preserved")
    check("### mcp_astrbot_t" in H.bridge_system_prompt("sys", [big]), "full contract rendered for compacted tool")
    raw = H.raw_bridge_tools([{"type": "function", "function": {"name": "w", "description": "x", "parameters": {"properties": {}}}}])
    check(raw == [{"name": "w", "description": "x", "inputSchema": {"type": "object", "properties": {}}}], f"openai tools converted: {raw}")

    blocks = await H.replay_prompt(messages[:3], messages[3:], resolve_image)
    text = "".join(b.get("text", "") for b in blocks)
    check("<astrbot_history>" in text and '<tool_call id="c1" name="mcp_astrbot_f">' in text and '<tool_result id="c1">' in text, "transcript rendered")
    check(text.rstrip().endswith("next"), "live input follows the history block")
    check(any(b["type"] == "image" for b in blocks), "recent history image re-attached")
    tiny = await H.replay_prompt([user("x" * 5000), assistant("y" * 5000), user("recent")], [user("now")], resolve_image, max_bytes=1500)
    tiny_text = "".join(b.get("text", "") for b in tiny)
    check("omitted to fit" in tiny_text and "recent" in tiny_text and "x" * 100 not in tiny_text, "oldest history dropped first")
    empty = await H.replay_prompt([user("q"), assistant("", []), tool("c9", "res")], [], resolve_image)
    check(empty[-1]["text"] == H.EMPTY_INPUT_CONTINUATION, "tool-result tail gets a continuation instruction")

    option = {"id": "model", "options": [{"value": "gemini-3.8-flash", "name": "Gemini 3.8 Flash"}, {"value": "gpt-oss-120b-medium", "name": "GPT-OSS 120B (Medium)"}]}
    check(resolve_model_choice(option, "gemini-3.8-flash-high") == ("gemini-3.8-flash", "high"), "slug with effort suffix")
    check(resolve_model_choice(option, "Gemini 3.8 Flash (Low)") == ("gemini-3.8-flash", "low"), "display name with effort")
    check(resolve_model_choice(option, "gpt-oss-120b-medium") == ("gpt-oss-120b-medium", None), "exact value wins over suffix parsing")
    check(resolve_model_choice(option, "") == (None, None), "empty keeps the session default")
    try:
        resolve_model_choice(option, "nope-9")
        raise AssertionError("unknown model accepted")
    except AgyError as exc:
        check(exc.code == "UNKNOWN_MODEL", "unknown model rejected")


# ------------------------------------------------------------------- e2e tests


async def test_bridge_numeric_enums(h: Harness) -> None:
    tools = H.raw_bridge_tools([{"function": {"name": "diagnostic", "parameters": {
        "type": "object", "properties": {"limit": {"type": "integer", "enum": [0]}},
    }}}])
    first = await collect(h.runtime, request("schema-numeric", [user("schema check")], tools=tools))
    conv = h.runtime._conversations[f"schema-numeric|{H.digest(tools)[:8]}"]
    wire = h.runtime.list_tools(conv.tenant_key)[0]["inputSchema"]["properties"]["limit"]
    check("enum" not in wire and wire["type"] == "integer", "MCP lists a typed numeric schema without invalid enum")
    check((wire["minimum"], wire["maximum"]) == (0, 0), "zero remains the only allowed numeric value")
    check(tools[0]["inputSchema"]["properties"]["limit"]["enum"] == [0], "original host schema stays intact")
    advertised_system = next(e["system"] for e in reversed(h.events()) if e["op"] == "new")
    check('"enum": [0]' in advertised_system, "original enum contract reaches the model system prompt")
    mark = h.mark()
    await collect(h.runtime, request("schema-numeric", [user("schema check"), assistant(first["text"]), user("next")], tools=tools))
    check(not any(e["op"] == "new" for e in h.events(mark)), "normalized wire schema does not spuriously rebuild sessions")


async def test_plain_chat_and_reuse(h: Harness) -> None:
    mark = h.mark()
    first = await collect(h.runtime, request("c1", [user("hello")]))
    check(first["text"] == "echo:gemini-3.8-flash:high:hello", f"reply: {first['text']!r}")
    check(first["reasoning"] == "thinking(high)", f"reasoning: {first['reasoning']!r}")
    check(first["finish"]["stop"] == "end_turn" and first["finish"]["usage"]["inputTokens"] == 600, "finish and usage")
    events = h.events(mark)
    new = [e for e in events if e["op"] == "new"]
    check(len(new) == 1 and "SYS" in new[0]["system"] and "no tools" in new[0]["system"], "one session with the system prompt appended")
    check(new[0]["cwd"].endswith("workspace"), "session cwd is the isolated workspace")
    check([e["text"] for e in events if e["op"] == "prompt"] == ["hello"], "first prompt carries only the input")

    mark = h.mark()
    second = await collect(h.runtime, request("c1", [user("hello"), assistant(first["text"]), user("second")]))
    check(second["text"].endswith(":second"), f"second reply: {second['text']!r}")
    events = h.events(mark)
    check(not [e for e in events if e["op"] == "new"], "session reused for the follow-up")
    check([e["text"] for e in events if e["op"] == "prompt"] == ["second"], "only the new input was sent")

    mark = h.mark()
    system = "SYS\nCurrent datetime: 2026-11-11 23:00"
    third = await collect(h.runtime, request("c1", [user("hello"), assistant("a"), user("second"), assistant("b"), user("third")], system=system))
    events = h.events(mark)
    check(not [e for e in events if e["op"] == "new"], "small system change keeps the session")
    prompt = [e["text"] for e in events if e["op"] == "prompt"][0]
    check("<system_update>" in prompt and "Current datetime: 2026-11-11 23:00" in prompt and prompt.endswith("third"), f"in-band system update: {prompt!r}")
    check(third["finish"] is not None, "third turn finished")

    mark = h.mark()
    await collect(h.runtime, request("c1", [user("DIFFERENT"), assistant("x"), user("fourth")], system=system))
    await asyncio.sleep(0.3)
    events = h.events(mark)
    check(len([e for e in events if e["op"] == "new"]) == 1, "rewritten history rebuilds the session")
    check(len([e for e in events if e["op"] == "close"]) == 1, "the replaced session is closed")
    prompt = [e["text"] for e in events if e["op"] == "prompt"][0]
    check("<astrbot_history>" in prompt and "DIFFERENT" in prompt and prompt.endswith("fourth"), f"replay prompt: {prompt!r}")

    mark = h.mark()
    await collect(h.runtime, request("c1", [user("DIFFERENT"), assistant("x"), user("fourth")], system=system))
    check(len([e for e in h.events(mark) if e["op"] == "new"]) == 1, "a regenerated answer (no new input) rebuilds")


async def test_execution_failure(h: Harness) -> None:
    mark = h.mark()
    try:
        await collect(h.runtime, request("terminal-failure", [user("EXECUTION_FAILURE")]))
        raise AssertionError("terminal failure was treated as a successful response")
    except AgyError as exc:
        check(exc.code == "UPSTREAM_EXECUTION", "terminal failure reaches the provider as a stable error")
        check(not exc.retryable, "unknown upstream failure does not trigger blind extra retries")
    prompts = [e for e in h.events(mark) if e["op"] == "prompt"]
    check(len(prompts) == 1, "terminal failure not re-sent")
    recovered = await collect(h.runtime, request("terminal-failure", [user("normal after failure")]))
    check(recovered["text"].endswith("normal after failure"), "failed session recovers on the next request")


async def test_images(h: Harness) -> None:
    mark = h.mark()
    await collect(h.runtime, request("img", [user("look", images=["data:image/png;base64,aGk="])]))
    prompts = [e for e in h.events(mark) if e["op"] == "prompt"]
    check(prompts[0]["images"] == 1, "image sent as an ACP image block")


async def test_tool_flow(h: Harness) -> None:
    mark = h.mark()
    ask = user('CALL get_weather {"city":"Paris"}')
    step = await collect(h.runtime, request("t1", [ask], tools=TOOLS))
    calls = step["calls"]
    check(calls is not None and len(calls) == 1, f"one tool call handed over: {step}")
    check(calls[0].name == "get_weather" and calls[0].arguments == {"city": "Paris"} and calls[0].id.startswith("agy_"), "tool call identity")
    check(step["finish"] is None, "step ended with tool calls, not a final answer")
    events = h.events(mark)
    tools_listed = [e for e in events if e["op"] == "tools"][0]["tools"]
    check(tools_listed == ["get_weather", "roll", "astrbot_await_result"], f"tools advertised over MCP: {tools_listed}")
    check("mcp_astrbot_" in [e for e in events if e["op"] == "new"][0]["system"], "tool prefix explained in the system prompt")

    final = await collect(h.runtime, request("t1", [ask, assistant("", calls), tool(calls[0].id, "sunny 25C")], tools=TOOLS))
    check(final["text"] == "RESULT(get_weather)=sunny 25C", f"tool result reached the agent: {final['text']!r}")
    check(final["finish"]["steps"] == 2, "usage normalized over two model steps")
    check(len([e for e in h.events(mark) if e["op"] == "new"]) == 1, "the whole tool loop ran in one ACP session")

    follow = await collect(h.runtime, request("t1", [ask, assistant("", calls), tool(calls[0].id, "sunny 25C"), assistant(final["text"]), user("thanks")], tools=TOOLS))
    check(follow["text"].endswith(":thanks"), "conversation continues after the tool loop")
    check(len([e for e in h.events(mark) if e["op"] == "new"]) == 1, "still the same session")


async def test_parallel_tools(h: Harness) -> None:
    ask = user('PCALL get_weather {"city":"A"} ;; roll {"sides":6}')
    step = await collect(h.runtime, request("t2", [ask], tools=TOOLS))
    calls = step["calls"]
    check(calls is not None and sorted(c.name for c in calls) == ["get_weather", "roll"], f"parallel calls batched: {calls}")
    results = [tool(c.id, f"res-{c.name}") for c in calls]
    final = await collect(h.runtime, request("t2", [ask, assistant("", calls), *results], tools=TOOLS))
    check("RESULT(get_weather)=res-get_weather" in final["text"] and "RESULT(roll)=res-roll" in final["text"], f"both results delivered: {final['text']!r}")


async def test_named_tool_metadata(h: Harness) -> None:
    identity = h.runtime._tool_identity
    check(identity({"kind": "other", "name": "mcp_astrbot_get_weather"}) == ("astrbot", "get_weather"), "new explicit MCP name recognized")
    check(identity({"kind": "execute", "name": "mcp_astrbot_get_weather"}) == (None, None), "native execution name cannot masquerade as MCP")
    check(identity({"kind": "other", "title": "mcp_astrbot_get_weather", "rawInput": {"name": "mcp_astrbot_get_weather"}}) == (None, None), "titles and raw input never authorize MCP")
    check(identity({"kind": "other", "name": "mcp_astrbot_get_weather", "mcpToolName": "roll"})[0] == "__mcp_identity_conflict__", "conflicting metadata refused")
    ask = user('NCALL get_weather {"city":"Paris"}')
    first = await collect(h.runtime, request("named-tools", [ask], tools=TOOLS))
    calls = first["calls"]
    check(calls is not None and len(calls) == 1, "new metadata authorizes the offered tool")
    check(calls[0].arguments == {"city": "Paris"}, "undeclared display summary not passed to AstrBot")
    final = await collect(h.runtime, request("named-tools", [ask, assistant("", calls), tool(calls[0].id, "sunny")], tools=TOOLS))
    check(final["text"] == "RESULT(get_weather)=sunny", "new metadata tool roundtrip")
    declared = [{"name": "summary_tool", "description": "test", "inputSchema": {"type": "object", "properties": {"toolSummary": {"type": "string"}}}}]
    got = await collect(h.runtime, request("declared-summary", [user('NCALL summary_tool {"toolSummary":"user value"}')], tools=declared))
    check(got["calls"][0].arguments["toolSummary"] == "user value", "explicitly declared summary argument preserved")
    await collect(h.runtime, request("declared-summary", [user('NCALL summary_tool {"toolSummary":"user value"}'), assistant("", got["calls"]), tool(got["calls"][0].id, "ok")], tools=declared))


async def test_permission_denied(h: Harness) -> None:
    step = await collect(h.runtime, request("t3", [user('DENYCALL get_weather {"city":"X"}')], tools=TOOLS))
    check(step["calls"] is None, "an unreported native call never reaches AstrBot")
    check(step["text"] == "RESULT(get_weather)=PERMISSION_REJECTED", f"native call refused: {step['text']!r}")
    plain = await collect(h.runtime, request("t4", [user('CALL get_weather {"city":"X"}')]))
    check(plain["calls"] is None and "PERMISSION_REJECTED" in plain["text"], "without mounted tools every permission request is refused")


async def test_notice_and_continuation(h: Harness) -> None:
    noisy = await collect(h.runtime, request("n1", [user("NOTICE hi")]))
    check("API error" not in noisy["text"] and noisy["text"].endswith("NOTICE hi"), f"retry notice filtered: {noisy['text']!r}")
    long = await collect(h.runtime, request("n2", [user("MAXTOK please")]))
    check(long["text"].startswith("part-one echo:"), f"continued after max_tokens: {long['text']!r}")
    check(long["finish"]["stop"] == "end_turn" and long["finish"]["steps"] == 2, "continuation counted as a model step")


async def test_relay(h: Harness) -> None:
    ask = user('CALL roll {"sides":6}')
    step = await collect(h.runtime, request("r1", [ask], tools=TOOLS))
    calls = step["calls"]
    check(calls is not None and len(calls) == 1, "tool call parked")
    await asyncio.sleep(3.2)  # longer than relay_after: the agent is told to keep waiting
    final = await collect(h.runtime, request("r1", [ask, assistant("", calls), tool(calls[0].id, "4")], tools=TOOLS))
    check(final["text"] == "RESULT(roll)=4", f"result delivered through the await relay: {final['text']!r}")


async def test_cancel(h: Harness) -> None:
    task = asyncio.create_task(collect(h.runtime, request("x1", [user("SLEEP 8000")])))
    await asyncio.sleep(0.8)
    task.cancel()
    try:
        await task
        raise AssertionError("cancelled request returned")
    except asyncio.CancelledError:
        pass
    conv = next(c for k, c in h.runtime._conversations.items() if k.startswith("x1|"))
    check(conv.live_turn is None and conv.dirty, "cancelled turn is closed and the session marked out of sync")
    again = await collect(h.runtime, request("x1", [user("SLEEP 8000"), user("after cancel")]))
    check(again["text"].endswith("after cancel"), f"conversation recovers after a cancel: {again['text']!r}")


async def test_ephemeral_and_models(h: Harness) -> None:
    mark = h.mark()
    out = await collect(h.runtime, request(None, [user("one-off")]))
    check(out["text"].endswith(":one-off"), "keyless request answered")
    await asyncio.sleep(0.4)
    events = h.events(mark)
    session = [e for e in events if e["op"] == "new"][0]["session"]
    check(any(e["op"] == "close" and e["session"] == session for e in events), "keyless session closed afterwards")
    check(not h.runtime._ephemerals, "no ephemeral conversation left behind")
    models = await h.runtime.list_models()
    check("gemini-3.8-flash-high" in models and "gpt-oss-120b-medium" in models, f"model catalog: {models}")
    fixed = await collect(h.runtime, request("m1", [user("hi")], model="gpt-oss-120b-medium", effort="high"))
    check(fixed["text"].startswith("echo:gpt-oss-120b-medium:"), "a model without thinking levels still answers")
    try:
        await collect(h.runtime, request("m2", [user("hi")], model="does-not-exist"))
        raise AssertionError("unknown model accepted")
    except AgyError as exc:
        check(exc.code == "UNKNOWN_MODEL", f"unknown model error: {exc.code}")


async def test_process_crash(h: Harness) -> None:
    first = await collect(h.runtime, request("k1", [user("before crash")]))
    ask = user('CALL roll {"sides":20}')
    step = await collect(h.runtime, request("k2", [ask], tools=TOOLS))
    calls = step["calls"]
    check(calls is not None, "tool call parked before the crash")
    h.runtime._proc._proc.kill()
    await asyncio.sleep(0.5)
    check(h.runtime._proc is None, "dead process detected")
    mark = h.mark()
    again = await collect(h.runtime, request("k1", [user("before crash"), assistant(first["text"]), user("after crash")]))
    check(again["text"].endswith("after crash"), f"new process serves the conversation: {again['text']!r}")
    prompt = [e["text"] for e in h.events(mark) if e["op"] == "prompt"][0]
    check("<astrbot_history>" in prompt and "before crash" in prompt, "history replayed into the new process")
    mark = h.mark()
    resumed = await collect(h.runtime, request("k2", [ask, assistant("", calls), tool(calls[0].id, "17")], tools=TOOLS))
    prompt = [e["text"] for e in h.events(mark) if e["op"] == "prompt"][0]
    check('<tool_result id="' in prompt and "17" in prompt and H.EMPTY_INPUT_CONTINUATION in prompt, f"tool loop resumed from the transcript: {prompt!r}")
    check(resumed["finish"] is not None, "resumed step finished")


async def test_context_policy_and_replay(h: Harness) -> None:
    checker = os.path.join(ROOT, "tests", "fake_context_check.py")
    h.config.context_settings = ContextSettings(patch_script=checker)
    mark = h.mark()
    messages = [user("EARLY_MARKER " + "x" * 220000), assistant("previous reply"), user("current")]
    await collect(h.runtime, request("context-replay", messages))
    prompt = [e["text"] for e in h.events(mark) if e["op"] == "prompt"][-1]
    check("EARLY_MARKER" in prompt and len(prompt.encode()) > 192000, "unlocked replay retains history beyond the old input clamp")
    status = await h.runtime.status()
    check(status["context_tokens"] == 512000 and status["replay_budget_bytes"] == 1024000, "default 512K policy is reported")
    old_pid = status["pid"]
    h.config.context_settings = ContextSettings("256K", 100000, checker)
    mark = h.mark()
    await collect(h.runtime, request("context-replay", [*messages, assistant("done"), user("next")]))
    status = await h.runtime.status()
    check(status["pid"] != old_pid and status["running_context_tokens"] == 256000, "changing context replaces the ACP process")
    check(status["replay_budget_bytes"] == 100000, "custom replay budget is applied")
    prompt = [e["text"] for e in h.events(mark) if e["op"] == "prompt"][-1]
    check("EARLY_MARKER" not in prompt and "omitted to fit" in prompt, "custom budget truncates only the old replay")
    with open(os.path.join(h.config.home, "agy", "settings.json"), encoding="utf-8") as handle:
        check(json.load(handle)["contextWindow"] == "256K", "agy settings match the configured window")
    mark = h.mark()
    await collect(h.runtime, request("context-replay", [*messages, assistant("done"), user("next"), assistant("ok"), user("again")]))
    check(not [e for e in h.events(mark) if e["op"] == "new"], "session reuse survives a context change")
    current_pid = status["pid"]
    h.config.context_settings = ContextSettings("900K", patch_script=os.path.join(h.tmp, "missing-checker.py"))
    try:
        await collect(h.runtime, request("blocked", [user("do not send")]))
        raise AssertionError("unverified 900K request was sent")
    except AgyError as exc:
        check(exc.code == "PREFLIGHT", "unverified larger context fails explicitly")
    status = await h.runtime.status()
    check(status["context_tokens"] is None and status["patch_state"] == "unknown", "status distinguishes an invalid configuration from a running old process")
    check(status["pid"] == current_pid, "failed validation does not replace a healthy process")
    h.config.context_settings = ContextSettings(patch_script=checker)


TESTS = [
    test_notices,
    test_managed_update_environment,
    test_safety_policy_compatibility,
    test_local_prompt_compatibility,
    test_history,
    test_bridge_numeric_enums,
    test_plain_chat_and_reuse,
    test_execution_failure,
    test_resource_notice,
    test_images,
    test_tool_flow,
    test_parallel_tools,
    test_named_tool_metadata,
    test_permission_denied,
    test_notice_and_continuation,
    test_relay,
    test_cancel,
    test_ephemeral_and_models,
    test_process_crash,
    test_context_policy_and_replay,
]


async def main() -> int:
    selected = [t for t in TESTS if not sys.argv[1:] or t.__name__ in sys.argv[1:]]
    harness = Harness()
    failed = 0
    try:
        for test in selected:
            try:
                await asyncio.wait_for(test(harness), 120)
                print(f"PASS {test.__name__}")
            except Exception:  # noqa: BLE001
                failed += 1
                print(f"FAIL {test.__name__}")
                traceback.print_exc()
                for level, line in harness.log.lines[-8:]:
                    print(f"    [{level}] {line}")
    finally:
        await harness.close()
    print(f"\n{len(selected) - failed}/{len(selected)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
