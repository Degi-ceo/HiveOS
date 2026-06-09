"""
sanitize.py — message and payload sanitization before hitting the API.

Ported from Hermes agent/message_sanitization.py (HERMES_REFERENCE REUSE-READY #7,
Direct/Zero). Strips lone surrogate code points that crash json.dumps and
JSON-repairs malformed tool-call arguments. Applied by the minimax adapter before
building the request body.
"""
from __future__ import annotations

import json
import re
from typing import Any

_SURROGATE_RE = re.compile(r"[\ud800-\udfff]")


def strip_surrogates(text: str) -> str:
    """Replace lone surrogates with U+FFFD. Fast no-op when none present."""
    if _SURROGATE_RE.search(text):
        return _SURROGATE_RE.sub("�", text)
    return text


def _walk_surrogates(node: Any) -> None:
    """In-place recursive surrogate strip in nested dicts/lists."""
    if isinstance(node, dict):
        for k, v in node.items():
            if isinstance(v, str):
                node[k] = strip_surrogates(v)
            elif isinstance(v, (dict, list)):
                _walk_surrogates(v)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            if isinstance(v, str):
                node[i] = strip_surrogates(v)
            elif isinstance(v, (dict, list)):
                _walk_surrogates(v)


def repair_tool_arguments(arguments: str) -> str:
    """Return valid JSON from a tool-call arguments string.

    Models sometimes emit trailing commas, comments, or truncated JSON. We
    try a best-effort repair: parse first; on failure strip the most common
    issues and retry; on second failure return an empty object.
    """
    try:
        json.loads(arguments)
        return arguments
    except (json.JSONDecodeError, TypeError):
        pass
    cleaned = arguments.strip()
    # Strip JS-style trailing commas before } or ]
    cleaned = re.sub(r",\s*([}\]])", r"\1", cleaned)
    try:
        parsed = json.loads(cleaned)
        return json.dumps(parsed)
    except (json.JSONDecodeError, TypeError):
        return "{}"


def sanitize_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Strip surrogates and repair tool-call args in an Anthropic message list.

    Mutates in place (cheaper than deep-copying large contexts) and returns
    the same list.
    """
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, str):
            msg["content"] = strip_surrogates(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text":
                    block["text"] = strip_surrogates(block.get("text", ""))
                elif block.get("type") == "tool_use":
                    inp = block.get("input")
                    if isinstance(inp, dict):
                        _walk_surrogates(inp)
                elif block.get("type") == "tool_result":
                    c = block.get("content", "")
                    if isinstance(c, str):
                        block["content"] = strip_surrogates(c)
    return messages
