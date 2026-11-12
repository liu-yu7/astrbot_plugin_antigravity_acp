"""AstrBot chat provider backed by the official Antigravity CLI over ACP.

The provider turns AstrBot's OpenAI-style request into one step of an ACP
prompt turn and maps the agent's stream back onto ``LLMResponse`` chunks. The
sign-in stays inside the official ``agy`` process: no token is extracted and no
request is relayed through any third-party gateway.
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncGenerator
from typing import Any, Literal

import astrbot.core.message.components as Comp
from astrbot import logger
from astrbot.api.provider import Provider
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.provider.entities import LLMResponse, ProviderType, TokenUsage
from astrbot.core.provider.register import provider_cls_map, provider_registry, register_provider_adapter
from astrbot.core.utils.astrbot_path import get_astrbot_plugin_data_path

from . import history as H
from .context import ContextSettings, model_context_limit
from .preflight import PreflightError
from .runtime import AgyError, AntigravityRuntime, ChatRequest, RuntimeConfig

try:  # AstrBot's shared media resolver: local paths, http(s), base64:// and data URIs
    from astrbot.core.utils.media_utils import resolve_media_ref_to_base64_data
except Exception:  # noqa: BLE001 - older cores; images then degrade to a placeholder
    resolve_media_ref_to_base64_data = None  # type: ignore[assignment]

PLUGIN_NAME = "astrbot_plugin_antigravity_acp"
PROVIDER_TYPE = "antigravity_acp"
PROVIDER_DESC = "Antigravity CLI (agy acp) 适配器：复用官方 Antigravity 登录态接入 Gemini"

#: Template shown in the WebUI when adding a provider source of this type.
DEFAULT_CONFIG_TMPL: dict[str, Any] = {
    "id": "antigravity",
    "provider": "antigravity",
    "type": PROVIDER_TYPE,
    "provider_type": "chat_completion",
    "enable": True,
    "key": [],  # unused: the login lives inside the official agy process
    "timeout": 300,
    "proxy": "",
    "agy_path": "",
    "agy_home": "",
    "agy_thought_level": "",
    "agy_expose_tools": True,
    "agy_max_sessions": 6,
    "agy_idle_ttl": 900,
}

_RUNTIMES: dict[tuple, AntigravityRuntime] = {}
_CONTEXT_SETTINGS = ContextSettings()


def configure_context(config: dict) -> None:
    global _CONTEXT_SETTINGS
    _CONTEXT_SETTINGS = ContextSettings.from_config(config)


def _number(value: Any, default: float, minimum: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if number >= minimum else default


def default_home() -> str:
    """Isolated Antigravity home inside AstrBot's plugin data directory."""
    return os.path.join(get_astrbot_plugin_data_path(), PLUGIN_NAME)


def runtime_config_from(provider_config: dict) -> RuntimeConfig:
    """Build the runtime settings from one (merged) provider configuration."""
    home = str(provider_config.get("agy_home") or "").strip()
    extra_env = provider_config.get("agy_env")
    return RuntimeConfig(
        home=os.path.abspath(os.path.expanduser(home)) if home else default_home(),
        agy_path=str(provider_config.get("agy_path") or "").strip(),
        proxy=str(provider_config.get("proxy") or "").strip(),
        extra_env=dict(extra_env) if isinstance(extra_env, dict) else {},
        default_effort=str(provider_config.get("agy_thought_level") or "").strip().lower(),
        expose_tools=bool(provider_config.get("agy_expose_tools", True)),
        max_live_sessions=int(_number(provider_config.get("agy_max_sessions"), 6, 1)),
        idle_session_ttl=_number(provider_config.get("agy_idle_ttl"), 900, 30),
        request_timeout=_number(provider_config.get("timeout"), 300, 10),
        context_settings=_CONTEXT_SETTINGS,
    )


def get_runtime(provider_config: dict) -> AntigravityRuntime:
    """Runtime shared by every provider entry that points at the same agy home and login."""
    config = runtime_config_from(provider_config)
    signature = config.signature()
    runtime = _RUNTIMES.get(signature)
    if runtime is None:
        runtime = AntigravityRuntime(config, logger)
        _RUNTIMES[signature] = runtime
    else:
        runtime.config = config  # pick up tunables changed in the WebUI
    return runtime


def all_runtimes() -> list[AntigravityRuntime]:
    return list(_RUNTIMES.values())


async def configured_model_limit(config: dict) -> int:
    try:
        plan = await get_runtime(config).context_plan()
        tokens = plan.tokens
    except PreflightError as exc:
        logger.warning(f"[antigravity-acp] context unavailable: {exc}")
        tokens = 256_000
    return model_context_limit(str(config.get("model") or ""), tokens)


async def sync_model_contexts(manager: Any) -> int:
    targets = []
    for entry in list(manager.providers_config):
        merged = manager.get_merged_provider_config(entry)
        if merged.get("type") == PROVIDER_TYPE:
            targets.append((entry, await configured_model_limit(merged)))
    changed = 0
    async with manager.resource_lock:
        for entry, limit in targets:
            if not any(item is entry for item in manager.providers_config):
                continue
            if entry.get("max_context_tokens") != limit:
                entry["max_context_tokens"] = limit
                changed += 1
            instance = manager.inst_map.get(entry.get("id"))
            if isinstance(instance, ProviderAntigravityACP):
                instance.provider_config["max_context_tokens"] = limit
        if changed:
            await manager.acm.confs["default"].save_config_async()
    return changed


async def shutdown_runtimes() -> None:
    """Stop every agy process this plugin started."""
    runtimes = list(_RUNTIMES.values())
    _RUNTIMES.clear()
    for runtime in runtimes:
        try:
            await runtime.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[antigravity-acp] failed to stop a runtime: {exc!r}")


class ProviderAntigravityACP(Provider):
    """Chat provider that drives the local official ``agy acp`` server."""

    def __init__(self, provider_config: dict, provider_settings: dict) -> None:
        super().__init__(provider_config, provider_settings)
        self.set_model(str(provider_config.get("model") or ""))
        self._warmup_task: asyncio.Task | None = None

    # ------------------------------------------------------------ plumbing

    @property
    def runtime(self) -> AntigravityRuntime:
        return get_runtime(self.provider_config)

    def get_current_key(self) -> str:
        return ""

    def get_keys(self) -> list[str]:
        return [""]

    def set_key(self, key: str) -> None:
        return None

    async def initialize(self) -> None:
        """Start agy and read the model catalog in the background so the first chat is not cold."""
        self.provider_config["max_context_tokens"] = await configured_model_limit(self.provider_config)
        if not self.provider_config.get("agy_warmup", True):
            return

        async def warmup() -> None:
            try:
                await self.runtime.list_models()
            except Exception as exc:  # noqa: BLE001 - warmup is best effort
                logger.debug(f"[antigravity-acp] warmup skipped: {exc!r}")

        self._warmup_task = asyncio.create_task(warmup(), name="agy-acp:warmup")

    async def terminate(self) -> None:
        # The runtime is shared between provider entries and is stopped with the plugin.
        task, self._warmup_task = self._warmup_task, None
        if task is not None and not task.done():
            task.cancel()

    async def get_models(self) -> list[str]:
        return await self.runtime.list_models()

    async def test(self, timeout: float = 120.0) -> None:
        await asyncio.wait_for(self.text_chat(prompt="REPLY `PONG` ONLY"), timeout=timeout)

    async def _resolve_image(self, ref: str) -> H.ResolvedImage | None:
        if resolve_media_ref_to_base64_data is None:
            return None
        try:
            data = await resolve_media_ref_to_base64_data(ref, media_type="image", strict=False)
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[antigravity-acp] 图片解析失败，将以占位符代替: {exc!r}")
            return None
        if not data or not getattr(data, "base64_data", ""):
            return None
        return H.ResolvedImage(data=data.base64_data, mime_type=getattr(data, "mime_type", None) or "image/jpeg")

    @staticmethod
    def _usage(payload: dict) -> TokenUsage | None:
        usage = payload.get("usage")
        if not isinstance(usage, dict):
            return None
        # One ACP turn can span several model calls (tool rounds, continuations). agy reports
        # their sum, so prompt buckets are normalized to reflect one request's context size.
        steps = max(1, int(payload.get("steps") or 1))

        def count(name: str) -> int:
            value = usage.get(name)
            return int(value) if isinstance(value, (int, float)) and value > 0 else 0

        return TokenUsage(
            input_other=round(count("inputTokens") / steps),
            input_cached=round(count("cachedReadTokens") / steps),
            output=count("outputTokens"),
        )

    def _build_request(
        self,
        *,
        prompt: str | None,
        session_id: str | None,
        image_urls: list[str] | None,
        func_tool: Any,
        contexts: Any,
        system_prompt: str | None,
        tool_calls_result: Any,
        model: str | None,
        extra_user_content_parts: Any,
        conversation_id: Any,
    ) -> ChatRequest:
        system, messages = H.normalize_request(
            contexts=contexts,
            prompt=prompt,
            system_prompt=system_prompt,
            image_urls=image_urls,
            extra_user_content_parts=extra_user_content_parts,
            tool_calls_result=tool_calls_result,
        )
        if not messages:
            raise AgyError("EMPTY_REQUEST", "没有可发送给 Antigravity 的消息")
        tools: list[dict] = []
        if func_tool:
            try:
                tools = H.raw_bridge_tools(func_tool.get_func_desc_openai_style())
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[antigravity-acp] 无法读取工具列表，本次请求不带工具: {exc!r}")
        key = conversation_id or session_id
        return ChatRequest(
            key=str(key) if key else None,
            system=system,
            messages=messages,
            tools=tools,
            model=str(model or self.get_model() or ""),
            effort=str(self.provider_config.get("agy_thought_level") or ""),
            resolve_image=self._resolve_image,
        )

    # ---------------------------------------------------------------- chat

    async def text_chat_stream(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: Any = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> AsyncGenerator[LLMResponse, None]:
        request = self._build_request(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            conversation_id=kwargs.get("conversation_id"),
        )
        attempts = request_max_retries if isinstance(request_max_retries, int) and request_max_retries > 0 else 2
        attempts = min(attempts, 3)
        runtime = self.runtime
        for attempt in range(1, attempts + 1):
            text: list[str] = []
            reasoning: list[str] = []
            emitted = False
            try:
                async for kind, payload in runtime.stream(request):
                    if kind == "text":
                        emitted = True
                        text.append(payload)
                        yield LLMResponse(
                            "assistant", result_chain=MessageChain(chain=[Comp.Plain(payload)]), is_chunk=True
                        )
                    elif kind == "reasoning":
                        emitted = True
                        reasoning.append(payload)
                        yield LLMResponse("assistant", completion_text="", reasoning_content=payload, is_chunk=True)
                    elif kind == "tool_calls":
                        yield LLMResponse(
                            "tool",
                            completion_text="".join(text),
                            tools_call_name=[call.name for call in payload],
                            tools_call_args=[call.arguments for call in payload],
                            tools_call_ids=[call.id for call in payload],
                            reasoning_content="".join(reasoning) or None,
                        )
                        return
                    elif kind == "finish":
                        yield LLMResponse(
                            "assistant",
                            completion_text="".join(text),
                            reasoning_content="".join(reasoning) or None,
                            usage=self._usage(payload),
                        )
                        return
                return
            except AgyError as exc:
                if exc.retryable and not emitted and attempt < attempts:
                    logger.warning(f"[antigravity-acp] {exc.code}: {exc}；第 {attempt} 次重试")
                    await asyncio.sleep(min(2.0 * attempt, 5.0))
                    continue
                raise

    async def text_chat(
        self,
        prompt: str | None = None,
        session_id: str | None = None,
        image_urls: list[str] | None = None,
        audio_urls: list[str] | None = None,
        func_tool: Any = None,
        contexts: Any = None,
        system_prompt: str | None = None,
        tool_calls_result: Any = None,
        model: str | None = None,
        extra_user_content_parts: Any = None,
        tool_choice: Literal["auto", "required"] = "auto",
        request_max_retries: int | None = None,
        **kwargs: Any,
    ) -> LLMResponse:
        final: LLMResponse | None = None
        async for response in self.text_chat_stream(
            prompt=prompt,
            session_id=session_id,
            image_urls=image_urls,
            audio_urls=audio_urls,
            func_tool=func_tool,
            contexts=contexts,
            system_prompt=system_prompt,
            tool_calls_result=tool_calls_result,
            model=model,
            extra_user_content_parts=extra_user_content_parts,
            tool_choice=tool_choice,
            request_max_retries=request_max_retries,
            **kwargs,
        ):
            if not response.is_chunk:
                final = response
        if final is None:
            raise AgyError("EMPTY_RESPONSE", "Antigravity 没有返回任何内容")
        return final


def register() -> None:
    """Register (or re-register after a plugin reload) the provider adapter.

    Plugins load before AstrBot instantiates providers, so registering at plugin
    import time makes the adapter available both at startup and in the WebUI's
    "add provider" list.
    """
    previous = provider_cls_map.pop(PROVIDER_TYPE, None)
    if previous is not None:
        try:
            provider_registry.remove(previous)
        except ValueError:
            pass
    register_provider_adapter(
        PROVIDER_TYPE,
        PROVIDER_DESC,
        provider_type=ProviderType.CHAT_COMPLETION,
        default_config_tmpl=dict(DEFAULT_CONFIG_TMPL),
        provider_display_name="Antigravity (agy acp)",
    )(ProviderAntigravityACP)
