"""AstrBot plugin: Gemini models through the official Antigravity CLI (``agy acp``).

The plugin registers a chat provider adapter (``antigravity_acp``) and an admin
command (``/agy``) for sign-in and diagnostics. See README.md.
"""

from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star
from astrbot.core.star.filter.command import GreedyStr

from .agy_acp import provider as agy_provider
from .agy_acp.runtime import AgyError, AntigravityRuntime

# Plugins are imported before AstrBot instantiates its providers, so the adapter
# has to exist as soon as this module is loaded.
agy_provider.register()

HELP = """Antigravity (agy acp) 管理指令（仅管理员）：
/agy status   查看 agy 版本、登录状态、进程与可用模型
/agy login    开始 Google 登录，返回授权链接
/agy code <授权码>   提交浏览器里拿到的授权码，完成登录
/agy models   重新读取当前账号可用的模型列表
/agy reset    丢弃所有 ACP 会话（下次对话按 AstrBot 历史重建）
/agy restart  重启 agy acp 进程"""


class AntigravityACPPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        self.context = context
        self.config = config if config is not None else {}
        agy_provider.configure_context(self.config)
        self._adopt_task: asyncio.Task | None = None

    async def initialize(self) -> None:
        self._adopt_task = asyncio.create_task(self._adopt_providers(), name="agy-acp:adopt")

    async def terminate(self) -> None:
        task, self._adopt_task = self._adopt_task, None
        if task is not None and not task.done():
            task.cancel()
        await agy_provider.shutdown_runtimes()

    # ----------------------------------------------------------------- helpers

    def _provider_configs(self) -> list[dict]:
        """Merged configurations of every provider entry (and bare source) of this adapter type."""
        manager = self.context.provider_manager
        configs: list[dict] = []
        for entry in list(getattr(manager, "providers_config", []) or []):
            try:
                merged = manager.get_merged_provider_config(entry)
            except Exception:  # noqa: BLE001
                merged = entry
            if isinstance(merged, dict) and merged.get("type") == agy_provider.PROVIDER_TYPE:
                configs.append(merged)
        for source in list(getattr(manager, "provider_sources_config", []) or []):
            if isinstance(source, dict) and source.get("type") == agy_provider.PROVIDER_TYPE:
                configs.append(source)
        return configs

    def _runtime(self) -> AntigravityRuntime:
        configs = self._provider_configs()
        return agy_provider.get_runtime(configs[0] if configs else {})

    async def _adopt_providers(self) -> None:
        """Bring this adapter's provider entries in line with the freshly loaded plugin code.

        When the plugin is installed, enabled or reloaded while AstrBot is already
        running, entries of this type were either skipped ("adapter not found") or
        still run the previous copy of the code; both are reloaded here. At a cold
        start AstrBot instantiates them itself right after the plugins, so this
        waits for the provider manager to finish first and then finds nothing to do.
        """
        try:
            manager = self.context.provider_manager
            for _ in range(180):
                if getattr(manager, "_mcp_init_task", None) is not None:
                    break
                await asyncio.sleep(1)
            else:
                return
            await asyncio.sleep(1)
            changed = await agy_provider.sync_model_contexts(manager)
            if changed:
                logger.info(f"[antigravity-acp] synchronized context limits for {changed} model entries")
            for entry in list(getattr(manager, "providers_config", []) or []):
                merged = manager.get_merged_provider_config(entry)
                if merged.get("type") != agy_provider.PROVIDER_TYPE or not merged.get("enable", False):
                    continue
                instance = manager.inst_map.get(entry.get("id"))
                if instance is None or type(instance) is not agy_provider.ProviderAntigravityACP:
                    logger.info(f"[antigravity-acp] (re)loading provider {entry.get('id')} with the current plugin code")
                    await manager.reload(entry)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[antigravity-acp] could not adopt existing provider entries: {exc!r}")

    # ---------------------------------------------------------------- commands

    @filter.permission_type(filter.PermissionType.ADMIN)
    @filter.command("agy")
    async def agy(self, event: AstrMessageEvent, args: GreedyStr):
        """Antigravity 管理：/agy status | login | code <授权码> | models | reset | restart"""
        parts = str(args or "").strip().split(None, 1)
        action = parts[0].lower() if parts else "status"
        rest = parts[1].strip() if len(parts) > 1 else ""
        try:
            runtime = self._runtime()
            if action in ("help", "帮助", "?"):
                yield event.plain_result(HELP)
            elif action == "status":
                yield event.plain_result(self._format_status(await runtime.status()))
            elif action == "login":
                url = await runtime.begin_login()
                yield event.plain_result(
                    "请在浏览器打开下面的链接，登录有 Antigravity 权限的 Google 账号并授权：\n\n"
                    f"{url}\n\n"
                    "授权完成后页面会显示一个授权码，发送 `/agy code <授权码>` 完成登录（建议在私聊或 WebUI 聊天里操作）。"
                )
            elif action == "code":
                if not rest:
                    yield event.plain_result("用法：/agy code <授权码>")
                else:
                    ok, detail = await runtime.complete_login(rest)
                    if ok:
                        models = await runtime.list_models(refresh=True)
                        yield event.plain_result("登录成功。可用模型：\n" + "\n".join(models))
                    else:
                        yield event.plain_result(f"登录未完成：{detail}")
            elif action == "models":
                models = await runtime.list_models(refresh=True)
                yield event.plain_result("当前账号可用模型：\n" + "\n".join(models))
            elif action == "reset":
                count = await runtime.reset_conversations()
                yield event.plain_result(f"已丢弃 {count} 个 ACP 会话，下次对话会按 AstrBot 历史重建。")
            elif action == "restart":
                await runtime.restart()
                await agy_provider.sync_model_contexts(self.context.provider_manager)
                yield event.plain_result("agy acp 进程已停止，下次请求时会按当前上下文设置重新启动。")
            else:
                yield event.plain_result(f"未知子指令：{action}\n\n{HELP}")
        except AgyError as exc:
            yield event.plain_result(f"[{exc.code}] {exc}")
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[antigravity-acp] /agy {action} failed: {exc!r}", exc_info=True)
            yield event.plain_result(f"执行失败：{exc}")

    @staticmethod
    def _format_status(status: dict) -> str:
        lines = [
            "Antigravity (agy acp) 状态",
            f"- agy: {status['binary']} (版本 {status['version']})",
            f"- 登录: {'已登录' if status['logged_in'] else '未登录 → 发送 /agy login'}",
            f"- 进程: {'运行中, pid ' + str(status['pid']) if status['running'] else '未启动（首次请求时启动）'}",
            f"- 活跃 ACP 会话: {status['sessions']}",
            f"- 代理: {status['proxy']}",
            f"- 隔离目录: {status['home']}",
            f"- 上下文设置: {status['context_window']}",
            f"- 已验证窗口上限: {str(status['context_tokens']) + ' tokens' if status['context_tokens'] else '不可用（见错误）'}",
            f"- 当前进程窗口: {str(status['running_context_tokens']) + ' tokens' if status['running_context_tokens'] else '未启动'}",
            f"- 历史回放预算: {str(status['replay_budget_bytes']) + ' bytes' if status['replay_budget_bytes'] is not None else '不可用'}",
            f"- 上下文补丁: {status['patch_state']} — {status['patch_detail']}",
            f"- 补丁检查脚本: {status['patch_script']}",
        ]
        if status["models"]:
            lines.append("- 模型: " + ", ".join(status["models"]))
        if status["efforts"]:
            lines.append("- 思考档位: " + ", ".join(status["efforts"]))
        if status["last_error"]:
            lines.append(f"- 最近错误: {status['last_error']}")
        if not status["logged_in"]:
            lines.append(f"- 终端登录命令: {status['login_command']}")
        return "\n".join(lines)
