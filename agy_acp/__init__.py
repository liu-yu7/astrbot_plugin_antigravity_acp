"""ACP client for the official Antigravity CLI (``agy acp``).

``runtime`` and its helpers have no AstrBot dependency (only ``aiohttp`` for the
MCP bridge) so they can be exercised against a fake agent; ``provider`` is the
AstrBot adapter on top of them.
"""
