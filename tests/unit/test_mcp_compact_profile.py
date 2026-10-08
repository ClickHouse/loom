"""tools/list compact profile: invocation-critical fields only."""

from __future__ import annotations

import asyncio
import json

from server.transport.mcp_schemas import (
    apply_schema_profile,
    compact_description,
    compact_descriptor,
    compact_input_schema,
)

FULL = {
    "name": "loom_memory_set",
    "description": (
        "Save a fact to Loom — shared across tools. **Use Loom for cross-tool facts.**\n\n"
        "**Call this proactively whenever the user states a fact.** Long guidance follows "
        "for several paragraphs about triggers, dedupe and key naming."
    ),
    "annotations": {"readOnlyHint": False, "destructiveHint": False},
    "inputSchema": {
        "type": "object",
        "required": ["memory_key", "value"],
        "properties": {
            "memory_key": {
                "type": "string",
                "title": "Memory Key",
                "description": "Stable key. Use snake_case; include the project token. Examples: a_b.",
            },
            "value": {
                "anyOf": [{"type": "string"}, {"type": "object", "additionalProperties": True}]
            },
            "top_k": {
                "type": ["integer", "string"],
                "default": 5,
                "minimum": 1,
                "maximum": 25,
                "examples": [5, 10],
            },
            "mode": {"type": "string", "enum": ["a", "b"], "default": "a"},
        },
        "$defs": {"X": {"type": "object", "properties": {"y": {"type": "string", "title": "Y"}}}},
    },
    "outputSchema": {"type": "object", "properties": {"version": {"type": "integer"}}},
}


def test_description_is_first_paragraph_without_markdown_bold():
    d = compact_description(FULL["description"])
    assert d == "Save a fact to Loom — shared across tools. Use Loom for cross-tool facts."
    assert compact_description("") == ""


def test_long_first_paragraph_is_cut_at_a_sentence_boundary():
    text = ("First sentence about the tool. " * 20).strip()
    d = compact_description(text)
    assert len(d) <= 281 and d.endswith(".")


def test_input_schema_keeps_the_argument_tree_and_constraints_only():
    c = compact_input_schema(FULL["inputSchema"])
    assert c["required"] == ["memory_key", "value"]
    assert set(c["properties"]) == {"memory_key", "value", "top_k", "mode"}
    assert c["properties"]["top_k"] == {
        "type": ["integer", "string"],
        "default": 5,
        "minimum": 1,
        "maximum": 25,
    }
    assert c["properties"]["mode"]["enum"] == ["a", "b"]
    assert "title" not in c["properties"]["memory_key"]
    assert c["properties"]["memory_key"]["description"] == "Stable key."  # one sentence
    assert "title" not in c["$defs"]["X"]["properties"]["y"]
    assert c["properties"]["value"]["anyOf"][1] == {"type": "object", "additionalProperties": True}


def test_descriptor_drops_output_schema_and_is_much_smaller():
    c = compact_descriptor(FULL)
    assert (
        "outputSchema" not in c
        and c["name"] == FULL["name"]
        and c["annotations"] == FULL["annotations"]
    )
    assert len(json.dumps(c)) < 0.75 * len(
        json.dumps(FULL)
    )  # toy fixture; the live listing test asserts <0.55
    assert apply_schema_profile([FULL], "full") == [FULL]
    assert apply_schema_profile([FULL], "compact") == [c]


def test_tools_list_honours_the_profile_argument_and_setting(monkeypatch):
    from server.settings import settings
    from server.transport import mcp_tools as mt

    monkeypatch.setattr(settings, "mcp_toolset", "all", raising=False)
    monkeypatch.setattr(settings, "mcp_code_tools", True, raising=False)
    monkeypatch.setattr(settings, "mcp_schema_profile", "standard", raising=False)
    standard = asyncio.run(mt._tools_descriptor())
    full = asyncio.run(mt._tools_descriptor(schema_profile="full"))
    compact = asyncio.run(mt._tools_descriptor(schema_profile="compact"))
    assert (
        [t["name"] for t in full] == [t["name"] for t in compact] == [t["name"] for t in standard]
    )
    assert all("outputSchema" in t for t in full) and not any("outputSchema" in t for t in compact)
    assert not any("outputSchema" in t for t in standard), "the default has no outputSchema"
    fb, cb = len(json.dumps(full)), len(json.dumps(compact))
    assert cb < 0.55 * fb, f"compact {cb} vs full {fb}"
    monkeypatch.setattr(settings, "mcp_schema_profile", "compact", raising=False)
    assert asyncio.run(mt._tools_descriptor()) == compact, "setting selects the default"
    assert asyncio.run(mt._tools_descriptor(schema_profile="bogus")) == compact, (
        "unknown falls back to the default"
    )


def test_structured_what_when_descriptions_keep_what_and_when_lines():
    text = (
        "WHAT: Fetch one memory by exact `memory_key`.\n"
        "WHEN: The user names a key explicitly.\n"
        "WHEN NOT: Don't call for natural-language recall — that's loom_search.\n"
        "ARGS: Just the memory_key."
    )
    assert compact_description(text) == (
        "WHAT: Fetch one memory by exact `memory_key`. WHEN: The user names a key explicitly."
    )
