"""
Tests for hive.llm.sanitize — message and payload sanitization.

Covers the 10 lines that were missed at 82%:
- _walk_surrogates dict branch recursion into list values (31-32)
- _walk_surrogates full list branch (33-38)
- sanitize_messages non-dict block `continue` (76)
- sanitize_messages text-block surrogate strip (78)
"""
from __future__ import annotations

import json

from hive.llm.sanitize import (
    _walk_surrogates,
    repair_tool_arguments,
    sanitize_messages,
    strip_surrogates,
)


# ---------------------------------------------------------------------------
# strip_surrogates — baseline (already covered, sanity)
# ---------------------------------------------------------------------------


def test_strip_surrogates_replaces_lone_surrogate():
    # Lone high surrogate (D800) without a low surrogate
    assert strip_surrogates("a\ud800b") == "a�b"


def test_strip_surrogates_noop_when_clean():
    assert strip_surrogates("hello world") == "hello world"


# ---------------------------------------------------------------------------
# sanitize_messages — string content branch (line 72)
# ---------------------------------------------------------------------------


def test_sanitize_messages_string_content_strips_surrogates():
    # When msg["content"] is a plain string (not a list of blocks), the str branch
    # at line 72 must strip surrogates in place.
    messages = [{"role": "user", "content": "hi\ud800there"}]
    out = sanitize_messages(messages)
    assert out[0]["content"] == "hi�there"


# ---------------------------------------------------------------------------
# _walk_surrogates — list branch (lines 33-38)
# ---------------------------------------------------------------------------


def test_walk_surrogates_top_level_list_of_strings():
    node = ["ok", "bad\ud800here", "fine"]
    _walk_surrogates(node)
    assert node == ["ok", "bad�here", "fine"]


def test_walk_surrogates_top_level_list_of_dicts():
    node = [{"a": "ok\ud800"}, {"b": "clean"}]
    _walk_surrogates(node)
    assert node == [{"a": "ok�"}, {"b": "clean"}]


def test_walk_surrogates_nested_list_inside_list():
    # Outer list element is itself a list — exercises the inner isinstance(v, (dict, list))
    # branch (line 37) and recursive call (line 38).
    node = [["a\ud800", "b"], ["c"]]
    _walk_surrogates(node)
    assert node == [["a�", "b"], ["c"]]


# ---------------------------------------------------------------------------
# _walk_surrogates — dict branch recursion into a list value (lines 31-32)
# ---------------------------------------------------------------------------


def test_walk_surrogates_dict_value_is_list():
    # Dict value is a list of strings — exercises dict's `elif isinstance(v, (dict, list))` (line 31)
    # and the recursive call (line 32).
    node = {"items": ["x\ud800", "y"], "name": "clean"}
    _walk_surrogates(node)
    assert node == {"items": ["x�", "y"], "name": "clean"}


def test_walk_surrogates_dict_value_is_nested_dict():
    # Dict value is a dict — also hits the dict-side isinstance(v, (dict, list)) branch.
    node = {"inner": {"k": "v\ud800"}}
    _walk_surrogates(node)
    assert node == {"inner": {"k": "v�"}}


# ---------------------------------------------------------------------------
# sanitize_messages — line 76 (non-dict block in content list)
# ---------------------------------------------------------------------------


def test_sanitize_messages_skips_non_dict_block_in_content_list():
    # Anthropic content blocks are dicts, but defensively skip anything else.
    messages = [
        {
            "role": "assistant",
            "content": [
                "raw-string-block",          # line 75-76: not a dict → continue
                {"type": "text", "text": "ok"},
            ],
        }
    ]
    out = sanitize_messages(messages)
    # Non-dict block passes through untouched; text block is fine.
    assert out[0]["content"][0] == "raw-string-block"
    assert out[0]["content"][1] == {"type": "text", "text": "ok"}


# ---------------------------------------------------------------------------
# sanitize_messages — line 78 (text block surrogate strip)
# ---------------------------------------------------------------------------


def test_sanitize_messages_text_block_strips_surrogates():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello\ud800world"},
            ],
        }
    ]
    out = sanitize_messages(messages)
    assert out[0]["content"][0]["text"] == "hello�world"


def test_sanitize_messages_text_block_missing_text_defaults_to_empty():
    # block.get("text", "") — line 78 when text key absent
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text"},  # no "text" key
            ],
        }
    ]
    out = sanitize_messages(messages)
    assert out[0]["content"][0]["text"] == ""


# ---------------------------------------------------------------------------
# sanitize_messages — tool_use path (line 79-82) and tool_result (83-86) — already
# covered, but included as regression guards so future changes to the dict/text
# paths don't accidentally drop these.
# ---------------------------------------------------------------------------


def test_sanitize_messages_tool_use_input_recurses():
    messages = [
        {
            "role": "assistant",
            "content": [
                {
                    "type": "tool_use",
                    "id": "t1",
                    "name": "do_thing",
                    "input": {"q": "hi\ud800"},
                },
            ],
        }
    ]
    out = sanitize_messages(messages)
    assert out[0]["content"][0]["input"] == {"q": "hi�"}


def test_sanitize_messages_tool_result_string_content_stripped():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "tool_result",
                    "tool_use_id": "t1",
                    "content": "result\ud800",
                },
            ],
        }
    ]
    out = sanitize_messages(messages)
    assert out[0]["content"][0]["content"] == "result�"


# ---------------------------------------------------------------------------
# repair_tool_arguments — happy path + fallback. Both branches were already
# covered, included as regression guards.
# ---------------------------------------------------------------------------


def test_repair_tool_arguments_already_valid():
    assert repair_tool_arguments('{"a": 1}') == '{"a": 1}'


def test_repair_tool_arguments_strips_trailing_comma():
    # First parse fails; the regex strip succeeds; returns canonical JSON.
    out = repair_tool_arguments('{"a": 1,}')
    assert json.loads(out) == {"a": 1}


def test_repair_tool_arguments_returns_empty_object_on_unrecoverable():
    # Both parses fail → "{}"
    assert repair_tool_arguments("not json at all {") == "{}"
