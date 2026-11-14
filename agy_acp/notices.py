"""Recognition of the status notices ``agy`` streams as ordinary message text.

The agent reports its own transport retries, output-cap continuations and
terminal upstream failures inside ``agent_message_chunk`` updates. Those are
not model output: retry notices are dropped, terminal failures become errors,
and text that may still be the start of a notice is held until it resolves.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Agent-side transport retry notice, e.g. ``⚠ API error (attempt 1): …``.
_AGENT_RETRY_NOTICE = re.compile(r"^\s*⚠\s*(?:Error:\s*)?API error \(attempt \d+\)", re.IGNORECASE)
# Prefix of the retry notice while its opening words arrive across chunks.
_AGENT_RETRY_PREFIX = re.compile(
    r"^\s*⚠\s*(?:e(?:r(?:r(?:o(?:r:?)?)?)?)?\s*)?"
    r"(?:a(?:p(?:i(?:\s+e(?:r(?:r(?:o(?:r(?:\s*\((?:a(?:t(?:t(?:e(?:m(?:p(?:t(?:\s*\d*)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?)?$",
    re.IGNORECASE,
)
# Lone warning prefix (``⚠`` or ``⚠ Error:``) before the notice body arrives.
WARNING_PREFIX = re.compile(r"^\s*⚠(?:\s*e(?:r(?:r(?:o(?:r:?)?)?)?)?)?\s*$", re.IGNORECASE)

_OUTPUT_LIMIT_HEAD = "your previous response was cut off because it exceeded the output token limit"
_OUTPUT_LIMIT_TAIL = "please continue from where you left off, keeping your response shorter"
NETWORK_ISSUE_MESSAGE = "There was a network issue connecting to the server, please try again."
_NETWORK_ISSUE_HEAD = "there was a network issue connecting to the server, please try again"
HIGH_TRAFFIC_MESSAGE = "Our servers are experiencing high traffic right now, please try again in a minute."
_HIGH_TRAFFIC_HEAD = "our servers are experiencing high traffic right now, please try again in a minute"
EXECUTION_FAILURE_MESSAGE = "Agent execution terminated due to error."
_EXECUTION_FAILURE_HEAD = "agent execution terminated due to error"

_RETRIES_ONLY = re.compile(r"^retries remaining:\s*\d+$", re.IGNORECASE)
_RETRIES_TAIL = re.compile(r"\s*retries remaining:\s*\d*\s*$", re.IGNORECASE)
_RETRIES_PARTIAL = re.compile(r"^retries remaining:\s*\d*$", re.IGNORECASE)


@dataclass(frozen=True)
class NoticeVerdict:
    """How buffered message text should be handled: emit, hold, drop, or error."""

    kind: str
    code: str = ""
    message: str = ""


_EMIT = NoticeVerdict("emit")
_HOLD = NoticeVerdict("hold")
_DROP = NoticeVerdict("drop")


def _notice_text(raw: str) -> str:
    """Collapse agy's warning mark and line breaks so a notice is recognizable across chunks."""
    text = raw.replace("⚠", " ")
    text = re.sub(r"\bError:", " ", text, flags=re.IGNORECASE)
    return re.sub(r"\s+", " ", text).strip().lower()


def _match_terminal_notice(raw: str, text: str, head: str, code: str, message: str, final: bool) -> NoticeVerdict | None:
    full = f"{head}."
    marked = "⚠" in raw
    if text == full or (marked and text.startswith(f"{full} ")):
        # Without the mark, hold until `final` so a model reply that merely quotes
        # the sentence and keeps explaining it is emitted intact.
        return NoticeVerdict("error", code, message) if (marked or final) else _HOLD
    if text == head:
        return NoticeVerdict("error", code, message) if final else _HOLD
    if not final and full.startswith(text):
        return _HOLD
    return None


def classify_output_limit_notice(raw: str) -> str:
    """Classify text against agy's output-cap continuation notice: drop, hold, or emit."""
    text = _notice_text(raw)
    if not text:
        return "emit"
    stripped = _RETRIES_TAIL.sub("", text).strip()
    retry = text[len(stripped):].strip()
    if retry and not _RETRIES_PARTIAL.match(retry):
        return "emit"
    if not stripped:
        return "drop" if _RETRIES_ONLY.match(text) else "emit"
    full = f"{_OUTPUT_LIMIT_HEAD} {_OUTPUT_LIMIT_TAIL}"
    if stripped in (full, _OUTPUT_LIMIT_HEAD, _OUTPUT_LIMIT_TAIL):
        return "drop"
    if full.startswith(stripped) and stripped.startswith("your previous"):
        return "hold"
    if _OUTPUT_LIMIT_TAIL.startswith(stripped) and stripped.startswith("please continue from where you left off"):
        return "hold"
    if "retries remaining:".startswith(text) and text.startswith("retries"):
        return "hold"
    return "emit"


def classify_agent_notice(raw: str, final: bool = False) -> NoticeVerdict:
    """Classify buffered ``agent_message_chunk`` text against agy's non-model notices.

    ``final`` is True when the message stream ended or switched away from text.
    """
    output_limit = classify_output_limit_notice(raw)
    if output_limit == "drop":
        return _DROP
    if output_limit == "hold":
        return _EMIT if final else _HOLD
    if _AGENT_RETRY_NOTICE.match(raw):
        return _DROP
    if not final and (WARNING_PREFIX.match(raw) or _AGENT_RETRY_PREFIX.match(raw.strip())):
        return _HOLD
    text = _notice_text(raw)
    if not text:
        return _EMIT
    network = _match_terminal_notice(raw, text, _NETWORK_ISSUE_HEAD, "TRANSPORT", NETWORK_ISSUE_MESSAGE, final)
    if network is not None:
        return network
    traffic = _match_terminal_notice(raw, text, _HIGH_TRAFFIC_HEAD, "RATE_LIMIT", HIGH_TRAFFIC_MESSAGE, final)
    if traffic is not None:
        return traffic
    execution = _match_terminal_notice(
        raw, text, _EXECUTION_FAILURE_HEAD, "UPSTREAM_EXECUTION", EXECUTION_FAILURE_MESSAGE, final
    )
    if execution is not None:
        return execution
    return _EMIT
