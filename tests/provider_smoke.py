#!/usr/bin/env python3
"""Smoke test of the AstrBot adapter (``agy_acp/provider.py``) against ``tests/fake_agy.py``.

It drives the provider with AstrBot's own types (``ToolSet``, message segments)
the way the agent runner does. Run it with AstrBot's interpreter; it uses an
isolated AstrBot root so a running service's data directory is never touched::

    ~/.local/share/uv/tools/astrbot/bin/python tests/provider_smoke.py
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_NAME = os.path.basename(PLUGIN_ROOT)
FAKE = os.path.join(PLUGIN_ROOT, "tests", "fake_agy.py")
PNG_1X1 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="


def check(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
    print(f"  ok  {message}")


def prepare_root() -> str:
    root = tempfile.mkdtemp(prefix="astrbot-agy-smoke-")
    os.environ["ASTRBOT_ROOT"] = root
    plugins = os.path.join(root, "data", "plugins")
    os.makedirs(plugins, exist_ok=True)
    os.symlink(PLUGIN_ROOT, os.path.join(plugins, PLUGIN_NAME))
    os.chdir(root)
    sys.path.insert(0, root)
    os.environ["FAKE_AGY_LOG"] = os.path.join(root, "fake.jsonl")
    os.chmod(FAKE, 0o755)
    return root


def fake_events(root: str) -> list[dict]:
    with open(os.path.join(root, "fake.jsonl"), encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


async def run(root: str) -> None:
    # Imported exactly like AstrBot imports a plugin: data.plugins.<name>.<module>
    provider_module = importlib.import_module(f"data.plugins.{PLUGIN_NAME}.agy_acp.provider")
    from astrbot.core.agent.message import AssistantMessageSegment, ToolCallMessageSegment
    from astrbot.core.agent.tool import FunctionTool, ToolSet
    from astrbot.core.provider.register import provider_cls_map, provider_registry

    provider_module.configure_context({"patch_script": os.path.join(PLUGIN_ROOT, "tests", "fake_context_check.py")})
    provider_module.register()
    provider_module.register()  # a plugin reload registers again; it must not raise or duplicate
    check(provider_module.PROVIDER_TYPE in provider_cls_map, "adapter registered")
    check(sum(1 for meta in provider_registry if meta.type == provider_module.PROVIDER_TYPE) == 1, "re-registration is idempotent")
    template = provider_cls_map[provider_module.PROVIDER_TYPE].default_config_tmpl
    check(template["provider_type"] == "chat_completion" and template["type"] == provider_module.PROVIDER_TYPE, "WebUI template")

    config = {
        "id": "agy-smoke",
        "type": provider_module.PROVIDER_TYPE,
        "provider_type": "chat_completion",
        "enable": True,
        "model": "gemini-3.8-flash-high",
        "agy_path": FAKE,
        "agy_home": os.path.join(root, "agy-home"),
        "timeout": 30,
        "agy_warmup": False,
    }
    provider = provider_module.ProviderAntigravityACP(config, {})
    await provider.initialize()
    check(provider.provider_config["max_context_tokens"] == 512000, "provider initializes with the verified 512K limit")
    check(provider.meta().type == provider_module.PROVIDER_TYPE and provider.get_model() == "gemini-3.8-flash-high", "provider metadata")

    class SavedConfig(dict):
        async def save_config_async(self):
            with open(os.path.join(root, "context-config.json"), "w", encoding="utf-8") as handle:
                json.dump(self, handle)
            return True

    own = dict(config, max_context_tokens=256000)
    other = {"id": "other", "type": "openai", "max_context_tokens": 32000}
    saved = SavedConfig(provider=[own, other], admins_id=["keep-admin"], unrelated={"keep": True})
    manager = SimpleNamespace(
        providers_config=saved["provider"],
        get_merged_provider_config=lambda entry: dict(entry),
        resource_lock=asyncio.Lock(),
        inst_map={"agy-smoke": provider},
        acm=SimpleNamespace(confs={"default": saved}),
    )
    check(await provider_module.sync_model_contexts(manager) == 1, "only this adapter's model limit is synchronized")
    with open(os.path.join(root, "context-config.json"), encoding="utf-8") as handle:
        persisted = json.load(handle)
    check(persisted["provider"][0]["max_context_tokens"] == 512000 and persisted["provider"][1] == other, "model cap persisted without changing another provider")
    check(persisted["admins_id"] == ["keep-admin"] and persisted["unrelated"] == {"keep": True}, "unrelated configuration preserved")
    check(await provider_module.sync_model_contexts(manager) == 0, "context synchronization is idempotent")
    provider_module.configure_context({"context_window": "256K", "patch_script": os.path.join(PLUGIN_ROOT, "tests", "fake_context_check.py")})
    check(await provider_module.sync_model_contexts(manager) == 1 and provider.provider_config["max_context_tokens"] == 256000, "plugin setting updates the live provider copy too")
    provider_module.configure_context({"patch_script": os.path.join(PLUGIN_ROOT, "tests", "fake_context_check.py")})
    await provider_module.sync_model_contexts(manager)

    print("classic call style (prompt + system_prompt)")
    response = await provider.text_chat(prompt="hello", system_prompt="SYS", session_id="s1")
    check(response.role == "assistant" and response.completion_text == "echo:gemini-3.8-flash:high:hello", f"reply: {response.completion_text!r}")
    check(response.reasoning_content == "thinking(high)", "reasoning mapped")
    check(response.usage is not None and (response.usage.input_other, response.usage.input_cached, response.usage.output) == (600, 400, 50), "usage mapped")

    print("agent-runner call style (contexts + func_tool, streaming)")
    tools = ToolSet()
    tools.add_tool(
        FunctionTool(
            name="get_weather",
            description="Get the current weather of a city.",
            parameters={"type": "object", "properties": {"city": {"type": "string", "description": "City name"}}, "required": ["city"]},
            handler=None,
        )
    )
    contexts: list = [{"role": "system", "content": "SYS"}, {"role": "user", "content": 'CALL get_weather {"city":"Paris"}'}]
    extra = {"session_id": "umo:1", "conversation_id": "conv-1", "abort_signal": asyncio.Event(), "request_max_retries": 2, "extra_user_content_parts": []}
    chunks, finals = [], []
    async for item in provider.text_chat_stream(contexts=contexts, func_tool=tools, **extra):
        (chunks if item.is_chunk else finals).append(item)
    check(len(finals) == 1 and finals[0].role == "tool", "step ends with a tool-call response")
    call = finals[0]
    check(call.tools_call_name == ["get_weather"] and call.tools_call_args == [{"city": "Paris"}], f"tool call: {call.tools_call_name} {call.tools_call_args}")
    check(any(c.reasoning_content for c in chunks), "reasoning streamed as chunks")

    contexts += [
        AssistantMessageSegment(content=None, tool_calls=call.to_openai_tool_calls_model()),
        ToolCallMessageSegment(tool_call_id=call.tools_call_ids[0], content="sunny 25C"),
    ]
    chunks, finals = [], []
    async for item in provider.text_chat_stream(contexts=contexts, func_tool=tools, **extra):
        (chunks if item.is_chunk else finals).append(item)
    streamed = "".join(c.completion_text for c in chunks if c.completion_text)
    check(finals[0].role == "assistant" and finals[0].completion_text == "RESULT(get_weather)=sunny 25C", f"final answer: {finals[0].completion_text!r}")
    check(streamed == finals[0].completion_text, "streamed text equals the final text")
    sessions = [e for e in fake_events(root) if e["op"] == "new"]
    check(len(sessions) == 2, "tool loop stayed in one ACP session (2 sessions in total so far)")

    contexts += [AssistantMessageSegment(content=finals[0].completion_text), {"role": "user", "content": "thanks"}]
    response = await provider.text_chat(contexts=contexts, func_tool=tools, **extra)
    check(response.completion_text.endswith(":thanks"), "follow-up reuses the conversation")
    check(len([e for e in fake_events(root) if e["op"] == "new"]) == 2, "no extra session for the follow-up")

    print("image input")
    response = await provider.text_chat(prompt="look", image_urls=[f"base64://{PNG_1X1}"], session_id="s-img")
    prompts = [e for e in fake_events(root) if e["op"] == "prompt"]
    check(prompts[-1]["images"] == 1, "image resolved by AstrBot and sent as an ACP image block")

    print("catalog and health check")
    models = await provider.get_models()
    check("gemini-3.8-flash-high" in models, f"models: {models}")
    await provider.test()
    check(True, "provider.test() passed")

    await provider.terminate()
    await provider_module.shutdown_runtimes()
    check(not provider_module.all_runtimes(), "runtimes stopped")


def main() -> int:
    root = prepare_root()
    try:
        asyncio.run(run(root))
    except Exception:  # noqa: BLE001
        import traceback

        traceback.print_exc()
        print("\nFAILED")
        return 1
    finally:
        os.chdir("/")
        shutil.rmtree(root, ignore_errors=True)
    print("\nALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
