"""Projection of AstrBot's OpenAI-style request history into Antigravity ACP prompts.

ACP sessions own their history, so AstrBot's authoritative history is replayed
once when a session is (re)built and afterwards only new input is sent. The
replay renders earlier turns as a delimited transcript in front of the newest
user input; tool calls keep the agent-visible ``mcp_astrbot_`` names.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

#: Server name under which AstrBot tools reach the agent; agy prefixes tool names with ``mcp_<server>_``.
MCP_SERVER_NAME = "astrbot"
#: Agent-visible prefix of every AstrBot tool.
AGENT_TOOL_PREFIX = f"mcp_{MCP_SERVER_NAME}_"
#: Tool the agent calls to keep waiting on a relayed AstrBot tool result.
AWAIT_TOOL_NAME = "astrbot_await_result"

AWAIT_TOOL = {
    "name": AWAIT_TOOL_NAME,
    "description": (
        "Wait for the result of an AstrBot tool call that reported it is still running. "
        "Call this only when a tool result tells you to, passing the call_id it gave."
    ),
    "inputSchema": {
        "type": "object",
        "properties": {"call_id": {"type": "string", "description": "The call id from the still-running notice."}},
        "required": ["call_id"],
    },
}

#: Byte budget of a replayed transcript. An unpatched ``agy`` tail-truncates a
#: single user input above 64,000 * 3 = 192,000 bytes, so stay below it.
DEFAULT_REPLAY_BUDGET_BYTES = 160_000
#: Byte ceiling keeping advertised MCP tool schemas below agy's ~1,500B lazy cutoff.
MAX_EAGER_BRIDGE_TOOL_BYTES = 1_350

HISTORY_OPEN = "<astrbot_history>\n"
HISTORY_CLOSE = "\n</astrbot_history>\n\n"
_OMISSION_NOTICE_RESERVE_BYTES = 128
#: Live input for a rebuild whose trailing messages are all tool results.
EMPTY_INPUT_CONTINUATION = "Continue the task from the latest tool results above."


@dataclass
class ToolCallRef:
    """One tool call recorded on an assistant message."""

    id: str
    name: str
    arguments: str


@dataclass
class Msg:
    """One request message, normalized from AstrBot's OpenAI-style history."""

    role: str  # user | assistant | tool | system
    text: str = ""
    images: list[str] = field(default_factory=list)
    tool_calls: list[ToolCallRef] = field(default_factory=list)
    tool_call_id: str = ""
    #: Sent to the model but never persisted by AstrBot (``_no_save``).
    transient: bool = False
    #: Text of the persisted content parts only; the stable identity of the message.
    stable_text: str = ""
    stable_images: int = 0


@dataclass
class ResolvedImage:
    data: str
    mime_type: str


ImageResolver = Callable[[str], Awaitable["ResolvedImage | None"]]


# --------------------------------------------------------------------------- normalization


def _to_dict(value: Any) -> dict | None:
    if isinstance(value, dict):
        return value
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        try:
            dumped = dump()
        except Exception:  # noqa: BLE001
            return None
        return dumped if isinstance(dumped, dict) else None
    return None


def _split_content(content: Any) -> tuple[str, list[str], str, int]:
    """Return (full text, image refs, persisted-only text, persisted image count)."""
    if content is None:
        return "", [], "", 0
    if isinstance(content, str):
        return content, [], content, 0
    if not isinstance(content, list):
        text = str(content)
        return text, [], text, 0
    texts: list[str] = []
    stable_texts: list[str] = []
    images: list[str] = []
    stable_images = 0
    for raw in content:
        if isinstance(raw, str):
            texts.append(raw)
            stable_texts.append(raw)
            continue
        temp = bool(getattr(raw, "_no_save", False))
        part = _to_dict(raw)
        if part is None:
            continue
        temp = temp or bool(part.get("_no_save"))
        kind = part.get("type")
        if kind == "text":
            text = str(part.get("text") or "")
            if text:
                texts.append(text)
                if not temp:
                    stable_texts.append(text)
        elif kind == "image_url":
            image_url = part.get("image_url")
            url = image_url.get("url") if isinstance(image_url, dict) else image_url
            if isinstance(url, str) and url:
                images.append(url)
                if not temp:
                    stable_images += 1
        elif kind == "audio_url":
            texts.append("[audio]")
            if not temp:
                stable_texts.append("[audio]")
        # `think` parts are the model's private reasoning and are never replayed.
    return "\n".join(texts), images, "\n".join(stable_texts), stable_images


def _tool_calls(raw: Any) -> list[ToolCallRef]:
    calls: list[ToolCallRef] = []
    if not isinstance(raw, list):
        return calls
    for item in raw:
        data = _to_dict(item)
        if data is None:
            continue
        function = data.get("function")
        function = function if isinstance(function, dict) else {}
        name = function.get("name") or data.get("name") or ""
        arguments = function.get("arguments")
        if arguments is None:
            arguments = data.get("arguments")
        if not isinstance(arguments, str):
            try:
                arguments = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
            except (TypeError, ValueError):
                arguments = "{}"
        calls.append(ToolCallRef(id=str(data.get("id") or ""), name=str(name), arguments=arguments))
    return calls


def _message_from(raw: Any) -> Msg | None:
    transient = bool(getattr(raw, "_no_save", False))
    data = _to_dict(raw)
    if data is None:
        return None
    transient = transient or bool(data.get("_no_save"))
    role = data.get("role")
    if role not in ("user", "assistant", "tool", "system"):
        return None  # checkpoints and unknown roles are not model input
    text, images, stable_text, stable_images = _split_content(data.get("content"))
    message = Msg(
        role=role,
        text=text,
        images=images if role == "user" else [],
        transient=transient,
        stable_text=stable_text,
        stable_images=stable_images if role == "user" else 0,
    )
    if role == "assistant":
        message.tool_calls = _tool_calls(data.get("tool_calls"))
    elif role == "tool":
        message.tool_call_id = str(data.get("tool_call_id") or "")
        if images:  # tool results may carry images; keep the references for the MCP result
            message.images = images
    return message


def normalize_request(
    *,
    contexts: Any = None,
    prompt: str | None = None,
    system_prompt: str | None = None,
    image_urls: list[str] | None = None,
    extra_user_content_parts: Any = None,
    tool_calls_result: Any = None,
) -> tuple[str, list[Msg]]:
    """Fold AstrBot's ``text_chat`` arguments into one system text and one message list."""
    system_parts: list[str] = []
    if system_prompt:
        system_parts.append(system_prompt)
    messages: list[Msg] = []
    for raw in contexts or []:
        message = _message_from(raw)
        if message is None:
            continue
        if message.role == "system" and not messages:
            if message.text:
                system_parts.append(message.text)
            continue
        messages.append(message)
    if prompt is not None:
        texts = [prompt] if prompt else []
        stable = list(texts)
        images = [url for url in (image_urls or []) if isinstance(url, str) and url]
        stable_images = len(images)
        extra_text, extra_images, extra_stable, extra_stable_images = _split_content(list(extra_user_content_parts or []))
        if extra_text:
            texts.append(extra_text)
        if extra_stable:
            stable.append(extra_stable)
        images.extend(extra_images)
        stable_images += extra_stable_images
        if not texts and images:
            texts.append("[Image]")
        messages.append(
            Msg(role="user", text="\n".join(texts), images=images, stable_text="\n".join(stable), stable_images=stable_images)
        )
    if tool_calls_result is not None:
        results = tool_calls_result if isinstance(tool_calls_result, list) else [tool_calls_result]
        for result in results:
            to_messages = getattr(result, "to_openai_messages", None)
            for raw in to_messages() if callable(to_messages) else []:
                message = _message_from(raw)
                if message is not None:
                    messages.append(message)
    return "\n\n".join(part for part in system_parts if part), messages


# --------------------------------------------------------------------------- identity


def digest(value: Any) -> str:
    """Stable short digest of a JSON-serializable value."""
    payload = value if isinstance(value, str) else json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha1(payload.encode("utf-8", "replace")).hexdigest()[:16]


def fingerprint(message: Msg) -> str:
    """Identity of one persisted message as the ACP session knows it.

    Assistant text is the session's own output and AstrBot may store it
    differently (stripped, re-rendered), so assistant messages match by role and
    tool results by call id. User text is compared on its persisted parts only.
    """
    if message.role == "assistant":
        return "A"
    if message.role == "tool":
        return f"T:{message.tool_call_id}"
    return f"{message.role[0].upper()}:{digest(message.stable_text.strip())}:{message.stable_images}"


def persistent(messages: list[Msg]) -> list[Msg]:
    """Messages AstrBot persists, i.e. the ones a later request will carry again."""
    return [message for message in messages if not message.transient]


def system_delta(old: str, new: str) -> str | None:
    """Lines of ``new`` that the session has not seen yet.

    Returns ``""`` when nothing needs to be sent, the added lines when the change
    is small enough for an in-band update (e.g. a refreshed date line), and
    ``None`` when the system prompt changed so much that the session should be
    rebuilt instead.
    """
    if old == new:
        return ""
    old_lines = {line.strip() for line in old.splitlines() if line.strip()}
    new_lines = {line.strip() for line in new.splitlines() if line.strip()}
    added = [line for line in new.splitlines() if line.strip() and line.strip() not in old_lines]
    removed = [line for line in old.splitlines() if line.strip() and line.strip() not in new_lines]
    added_text = "\n".join(added)
    removed_text = "\n".join(removed)
    if len(added_text) > max(600, len(new) // 4) or len(removed_text) > max(600, len(old) // 4):
        return None
    return added_text


# --------------------------------------------------------------------------- tools


def _shorten(text: str, max_chars: int) -> str:
    clean = re.sub(r"\s+", " ", text).strip()
    if len(clean) <= max_chars:
        return clean
    sentence_end = clean.find(". ")
    if 32 <= sentence_end and sentence_end + 1 <= max_chars:
        return clean[: sentence_end + 1]
    return clean[: max(1, max_chars - 1)].rstrip() + "…"


def _compact_schema(value: Any, max_desc_chars: int) -> Any:
    if isinstance(value, list):
        return [_compact_schema(item, max_desc_chars) for item in value]
    if not isinstance(value, dict):
        return value
    out: dict = {}
    for key, entry in value.items():
        if key == "description" and isinstance(entry, str):
            if max_desc_chars > 0:
                out[key] = _shorten(entry, max_desc_chars)
            continue
        if key == "properties" and isinstance(entry, dict):
            out[key] = {name: _compact_schema(prop, max_desc_chars) for name, prop in entry.items()}
            continue
        out[key] = _compact_schema(entry, max_desc_chars)
    return out


def _wire_bytes(tool: dict) -> int:
    return len(json.dumps(tool, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def compact_bridge_tool(tool: dict) -> dict:
    """Keep one MCP tool schema under agy's eager size threshold.

    Every parameter name, type, required list and enum survives; only prose
    descriptions are shortened (the full text goes into the system prompt).
    """
    if _wire_bytes(tool) <= MAX_EAGER_BRIDGE_TOOL_BYTES:
        return tool
    first = {
        "name": tool["name"],
        "description": _shorten(tool.get("description") or "", 220),
        "inputSchema": _compact_schema(tool.get("inputSchema") or {}, 90),
    }
    if _wire_bytes(first) <= MAX_EAGER_BRIDGE_TOOL_BYTES:
        return first
    return {
        "name": tool["name"],
        "description": _shorten(tool.get("description") or "", 140),
        "inputSchema": _compact_schema(tool.get("inputSchema") or {}, 0),
    }


def raw_bridge_tools(openai_tools: Any) -> list[dict]:
    """Turn AstrBot's OpenAI-style tool descriptors into uncompacted MCP descriptors."""
    tools: list[dict] = []
    seen: set[str] = set()
    for item in openai_tools or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") if isinstance(item.get("function"), dict) else item
        name = function.get("name")
        if not isinstance(name, str) or not name or name in seen:
            continue
        seen.add(name)
        parameters = function.get("parameters")
        if not isinstance(parameters, dict) or not parameters:
            parameters = {"type": "object", "properties": {}}
        elif "type" not in parameters:
            parameters = {"type": "object", **parameters}
        tools.append({"name": name, "description": str(function.get("description") or ""), "inputSchema": parameters})
    return tools


def render_full_tool_contracts(tools: list[dict]) -> str:
    """Full prose contracts of the tools whose MCP declarations had to be compacted."""
    sections: list[str] = []
    for tool in tools:
        if _wire_bytes(tool) <= MAX_EAGER_BRIDGE_TOOL_BYTES:
            continue
        lines = [f"### {AGENT_TOOL_PREFIX}{tool['name']}"]
        description = (tool.get("description") or "").strip()
        if description:
            lines.append(description)
        properties = (tool.get("inputSchema") or {}).get("properties")
        if isinstance(properties, dict):
            params = []
            for name, prop in properties.items():
                desc = prop.get("description") if isinstance(prop, dict) else None
                if isinstance(desc, str) and desc.strip():
                    params.append(f"- `{name}`: {desc.strip()}")
            if params:
                lines.append("Parameters:\n" + "\n".join(params))
        sections.append("\n\n".join(lines))
    return "\n\n".join(sections)


def bridge_system_prompt(system: str, tools: list[dict]) -> str:
    """The instructions appended to the agent's system prompt for one session."""
    preamble = [
        "You are the chat model behind an AstrBot chatbot session. AstrBot owns the conversation, "
        "the tools, and their permissions. You are talking with people in a chat app, not working in a code workspace.",
    ]
    if tools:
        preamble.append(
            f"Every AstrBot tool is available to you under the prefix `{AGENT_TOOL_PREFIX}`: a tool named `X` in the "
            f"instructions below is the tool `{AGENT_TOOL_PREFIX}X`. Use only these tools and pass arguments exactly "
            "as their schemas define. Never claim that a tool ran unless you actually called it."
        )
    else:
        preamble.append("You have no tools in this session: answer directly.")
    preamble.append(
        "When the first user message contains an <astrbot_history> block, it reproduces the earlier part of this "
        "conversation; continue from the newest input after it without repeating completed actions or re-answering "
        "old messages."
    )
    parts = ["\n".join(preamble)]
    if system:
        parts.append(system)
    contracts = render_full_tool_contracts(tools)
    if contracts:
        parts.append(
            "<astrbot_tool_contracts>\nFull specifications for AstrBot tools whose MCP schemas were compacted:\n\n"
            f"{contracts}\n</astrbot_tool_contracts>"
        )
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- transcript


def _utf8(text: str) -> int:
    return len(text.encode("utf-8", "replace"))


def _head_within_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8", "replace")
    if len(raw) <= max_bytes:
        return text
    return raw[: max(0, max_bytes)].decode("utf-8", "ignore")


def _render_message(message: Msg, image_numbers: list[int] | None = None) -> str:
    if message.role == "user":
        lines = [message.text] if message.text else []
        for number in image_numbers or []:
            lines.append(f"[image #{number}]")
        return "<user>\n" + "\n".join(lines) + "\n</user>"
    if message.role == "assistant":
        chunks: list[str] = []
        if message.text.strip():
            chunks.append(f"<assistant>\n{message.text}\n</assistant>")
        for call in message.tool_calls:
            chunks.append(f'<tool_call id="{call.id}" name="{AGENT_TOOL_PREFIX}{call.name}">\n{call.arguments}\n</tool_call>')
        return "\n\n".join(chunks)
    if message.role == "tool":
        return f'<tool_result id="{message.tool_call_id}">\n{message.text}\n</tool_result>'
    if message.role == "system":
        return f"<system_update>\n{message.text}\n</system_update>" if message.text else ""
    return ""


def _truncated_tool_message(message: Msg, budget: int) -> str | None:
    marker = "\n[tool result truncated to fit the context replay budget]"
    limit = budget - _utf8(marker)
    for _attempt in range(8):
        if limit <= 0:
            return None
        clipped = Msg(role="tool", text=_head_within_bytes(message.text, limit) + marker, tool_call_id=message.tool_call_id)
        rendered = _render_message(clipped)
        size = _utf8(rendered)
        if size <= budget:
            return rendered
        limit -= size - budget
    return None


def _history_transcript(history: list[Msg], budget: int) -> tuple[str, list[tuple[int, str]]]:
    """Render history newest-first into ``budget`` bytes; return it with the numbered image refs it kept."""
    numbered: list[list[int]] = []
    counter = 0
    for message in history:
        numbers = []
        for _ref in message.images if message.role == "user" else []:
            counter += 1
            numbers.append(counter)
        numbered.append(numbers)
    parts: list[str] = []
    kept_images: list[tuple[int, str]] = []
    used = 0
    dropped = 0
    for index in range(len(history) - 1, -1, -1):
        message = history[index]
        rendered = _render_message(message, numbered[index])
        if not rendered:
            continue
        separator = 0 if not parts else 2
        size = _utf8(rendered)
        if used + separator + size > budget:
            if not parts and message.role == "tool" and budget > 1024:
                truncated = _truncated_tool_message(message, budget)
                if truncated is not None:
                    parts.insert(0, truncated)
                    dropped = index
                else:
                    dropped = index + 1
            else:
                dropped = index + 1
            break
        parts.insert(0, rendered)
        used += separator + size
        if message.role == "user":
            kept_images[0:0] = list(zip(numbered[index], message.images))
    if dropped > 0:
        parts.insert(0, f"[Earlier {dropped} history messages omitted to fit the context replay budget]")
    if not parts:
        return "", []
    body = "\n\n".join(parts)
    return f"{HISTORY_OPEN}{body}{HISTORY_CLOSE}", kept_images


async def input_prompt(messages: list[Msg], resolve_image: ImageResolver) -> list[dict]:
    """Convert new input messages into live ACP prompt blocks, inlining images."""
    blocks: list[dict] = []
    for message in messages:
        group: list[dict] = []
        if message.role == "user":
            if message.text:
                group.append({"type": "text", "text": message.text})
            for ref in message.images:
                image = await resolve_image(ref)
                if image is not None:
                    group.append({"type": "image", "data": image.data, "mimeType": image.mime_type})
                else:
                    group.append({"type": "text", "text": "[image unavailable]"})
        else:
            rendered = _render_message(message)
            if rendered:
                group.append({"type": "text", "text": rendered})
        if not group:
            continue
        if blocks:
            blocks.append({"type": "text", "text": "\n\n"})
        blocks.extend(group)
    if not blocks:
        blocks.append({"type": "text", "text": "Continue."})
    return blocks


async def replay_prompt(
    history: list[Msg],
    input_messages: list[Msg],
    resolve_image: ImageResolver,
    *,
    max_bytes: int = DEFAULT_REPLAY_BUDGET_BYTES,
    replay_images: int = 4,
) -> list[dict]:
    """Build the prompt that (re)creates a session.

    Earlier history becomes a transcript, the trailing input stays live content.
    History renders newest-first into the byte budget so the most recent turns
    survive; the newest ``replay_images`` history images are re-attached so a
    rebuilt session can still see what the user sent.
    """
    if input_messages:
        input_blocks = await input_prompt(input_messages, resolve_image)
    else:
        input_blocks = [{"type": "text", "text": EMPTY_INPUT_CONTINUATION}]
    blocks: list[dict] = []
    if history:
        input_bytes = sum(_utf8(block["text"]) for block in input_blocks if block.get("type") == "text")
        overhead = _utf8(HISTORY_OPEN) + _utf8(HISTORY_CLOSE) + _OMISSION_NOTICE_RESERVE_BYTES
        text, images = _history_transcript(history, max(0, max_bytes - input_bytes - overhead))
        if text:
            blocks.append({"type": "text", "text": text})
            for number, ref in images[-replay_images:] if replay_images > 0 else []:
                image = await resolve_image(ref)
                if image is None:
                    continue
                blocks.append({"type": "text", "text": f"[image #{number}] from the history above:"})
                blocks.append({"type": "image", "data": image.data, "mimeType": image.mime_type})
            if len(blocks) > 1:
                blocks.append({"type": "text", "text": "\n\n"})
    blocks.extend(input_blocks)
    return blocks


def split_trailing_input(messages: list[Msg]) -> tuple[list[Msg], list[Msg]]:
    """Split a full history into replayed history and the trailing user/system input."""
    cut = len(messages)
    while cut > 0 and messages[cut - 1].role in ("user", "system"):
        cut -= 1
    return messages[:cut], messages[cut:]
