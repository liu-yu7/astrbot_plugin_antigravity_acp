"""Context policy and read-only verification of the optional local agy patch."""

from __future__ import annotations

import asyncio
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from .preflight import PreflightError

WINDOWS = {"256K": 256_000, "512K": 512_000, "900K": 900_000}
DEFAULT_WINDOW = "512K"
LOCKED_REPLAY_BYTES = 160_000
MAX_REPLAY_BYTES = 2_200_000
# The automatic preset reserves the 64K output budget of supported 1M Gemini models.
UNLOCKED_DEFAULT_TOKENS = 983_040
_GATES = ("1", "2", "3A", "3B", "4A", "4B")


@dataclass(frozen=True)
class ContextSettings:
    window: str = DEFAULT_WINDOW
    replay_bytes: int = 0
    patch_script: str = ""

    @classmethod
    def from_config(cls, config: dict) -> ContextSettings:
        window = config.get("context_window", DEFAULT_WINDOW)
        if window not in (*WINDOWS, "Default"):
            raise PreflightError("上下文窗口必须是 Default、256K、512K 或 900K")
        replay = config.get("replay_budget_bytes", 0)
        if isinstance(replay, bool) or not isinstance(replay, int) or not 0 <= replay <= MAX_REPLAY_BYTES:
            raise PreflightError(f"历史回放预算必须是 0 到 {MAX_REPLAY_BYTES} 的整数（字节）")
        script = config.get("patch_script", "")
        if not isinstance(script, str):
            raise PreflightError("补丁检查脚本必须是本机 Python 文件路径")
        script = os.path.expanduser(script.strip())
        if script and not os.path.isabs(script):
            raise PreflightError("补丁检查脚本路径必须是绝对路径")
        return cls(window=window, replay_bytes=replay, patch_script=script)


@dataclass(frozen=True)
class PatchStatus:
    state: str
    detail: str
    script: str
    fingerprint: tuple = ()


@dataclass(frozen=True)
class ContextPlan:
    window: str
    tokens: int
    replay_bytes: int
    patch: PatchStatus

    @property
    def settings_value(self) -> str:
        return "" if self.window == "Default" else self.window

    @property
    def signature(self) -> tuple:
        return (self.window, self.tokens, self.replay_bytes, self.patch.fingerprint)


def resolve_plan(settings: ContextSettings, patch: PatchStatus) -> ContextPlan:
    unlocked = patch.state == "unlocked"
    tokens = WINDOWS.get(settings.window, UNLOCKED_DEFAULT_TOKENS if unlocked else 256_000)
    if tokens > 256_000 and not unlocked:
        raise PreflightError(
            f"已选择 {settings.window}，但无法确认 agy 上下文补丁已完整应用（{patch.detail}）。"
            "请检查插件设置中的补丁脚本，或改用 256K；插件不会自动改写 agy 二进制。"
        )
    ceiling = min(tokens * 2, MAX_REPLAY_BYTES) if unlocked else LOCKED_REPLAY_BYTES
    replay = settings.replay_bytes or ceiling
    if replay > ceiling:
        raise PreflightError(
            f"当前窗口/补丁状态允许的历史回放预算最多为 {ceiling} 字节，当前设置为 {replay}；"
            "设为 0 可自动选择。"
        )
    return ContextPlan(settings.window, tokens, replay, patch)


def model_context_limit(model: str, tokens: int) -> int:
    return min(tokens, 128_000) if str(model).lower().startswith("gpt-oss") else tokens


def _file_identity(path: str) -> tuple:
    try:
        stat = os.stat(path)
        return (path, stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    except OSError:
        return (path, None)


class ContextInspector:
    def __init__(self) -> None:
        self._cached: PatchStatus | None = None
        self._lock = asyncio.Lock()

    async def inspect(self, binary: str, script: str = "") -> PatchStatus:
        script = script or str(Path.home() / ".gemini" / "antigravity" / "tools" / "agy-patch.py")
        async with self._lock:
            fingerprint = (_file_identity(binary), _file_identity(script))
            if self._cached is not None and self._cached.fingerprint == fingerprint:
                return self._cached
            if not os.path.isfile(script):
                result = PatchStatus("unknown", f"找不到检查脚本：{script}", script, fingerprint)
            else:
                result = await self._run(binary, script, fingerprint)
            self._cached = result
            return result

    async def _run(self, binary: str, script: str, fingerprint: tuple) -> PatchStatus:
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", script, "--binary", binary, "--dry-run", "--no-env-fix",
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=os.path.expanduser("~"),
            )
            out, err = await asyncio.wait_for(proc.communicate(), 90)
            text = (out + b"\n" + err).decode("utf-8", "replace")
            if (_file_identity(binary), _file_identity(script)) != fingerprint:
                return PatchStatus("unknown", "检查期间二进制或脚本发生变化，请重试", script)
            matches = {gate: re.findall(rf"^\[([=*])\] \[Gate {gate}\] (.+)$", text, re.MULTILINE) for gate in _GATES}
            complete = all(len(matches[gate]) == 1 for gate in _GATES)
            unlocked = complete and all(matches[gate][0][0] == "=" and "already patched" in matches[gate][0][1] for gate in _GATES)
            if proc.returncode == 0 and unlocked and "all gates already unlocked; nothing to write" in text:
                return PatchStatus("unlocked", "六处上下文/用户输入补丁均已应用", script, fingerprint)
            if proc.returncode == 0 and complete and "dry run complete:" in text:
                return PatchStatus("locked", "上下文补丁尚未完整应用", script, fingerprint)
            detail = " ".join(text.strip().splitlines()[-2:])[-400:]
            return PatchStatus("unknown", f"检查不兼容或失败（exit={proc.returncode}）：{detail}", script, fingerprint)
        except asyncio.TimeoutError:
            return PatchStatus("unknown", "补丁检查超时（90 秒）", script, fingerprint)
        except OSError as exc:
            return PatchStatus("unknown", f"无法运行补丁检查：{exc}", script, fingerprint)
        finally:
            if proc is not None and proc.returncode is None:
                proc.kill()
                await proc.wait()
