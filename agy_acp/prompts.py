"""Compatibility for one reproduced AstrBot Linux host-tools template.

This changes wording, never tool availability, permissions, skill instructions
or user input. Unknown/custom host templates are not inferred or stripped.
"""
from __future__ import annotations

ASTRBOT_LOCAL_MODE_PROMPT = (
    "Local computer tools are available subject to your role's file and execution permissions. "
    "Current operating system: Linux. "
    "The runtime shell is Unix-like. Use POSIX-compatible shell commands. "
    "Local shell commands automatically return a managed session when they outlive the initial wait. "
    "Use `astrbot_shell_session` to list, poll, write raw text or complete lines to, interrupt, or terminate those sessions. "
    "Use its `write_line` action for line-oriented programs so the session receives a real line feed. "
    "Do not add `&`, `nohup`, or another detachment wrapper for ordinary long-running commands."
)

ACP_LOCAL_MODE_POLICY = (
    "AstrBot host tool policy: The role's file and execution permissions govern all host tool use. "
    "The host operating system is Linux, with a Unix-like shell requiring POSIX-compatible commands. "
    "AstrBot manages commands as sessions when they run beyond the initial wait. "
    "Use `astrbot_shell_session` to list or poll these sessions, write raw text or complete lines, interrupt them, or terminate them. "
    "For line-oriented programs, use `write_line` so the input includes a real line feed. "
    "Keep ordinary long-running commands inside the managed session: never add `&`, use `nohup`, or use any other detachment wrapper."
)


def adapt_astrbot_local_prompt(system: str) -> str:
    """Replace an exact stock paragraph, preserving all surrounding bytes."""
    start = system.find(ASTRBOT_LOCAL_MODE_PROMPT)
    if start < 0:
        return system
    end = start + len(ASTRBOT_LOCAL_MODE_PROMPT)
    if (start and system[start - 1] != "\n") or (end < len(system) and system[end] not in "\r\n"):
        return system
    return system[:start] + ACP_LOCAL_MODE_POLICY + system[end:]
