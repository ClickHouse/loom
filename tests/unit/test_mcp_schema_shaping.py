"""Unit-level checks on the per-node JSON-schema fixes the MCP transport
applies to tool descriptors, exercised through the shared `_walk_schema`
recursion + leaf rules.

The leaf rules are pure (node in / node-or-None out) so they unit-test
without spinning up the FastAPI app or the Operation registry.
"""

from __future__ import annotations

from server.transport.mcp_schemas import (
    _leaf_redact_episodic,
    _leaf_widen_integer,
    _walk_schema,
)


def _widen_integer_to_int_or_string(schema):
    return _walk_schema(schema, _leaf_widen_integer)


def _redact_episodic_from_memory_type(schema):
    return _walk_schema(schema, _leaf_redact_episodic)


def test_widen_integer_to_anyof_int_or_string() -> None:
    schema = {
        "properties": {
            "version": {"type": "integer", "title": "Version"},
            "name": {"type": "string"},
        },
    }
    out = _widen_integer_to_int_or_string(schema)
    version = out["properties"]["version"]
    # type-ARRAY form, not anyOf blocks: same semantics, ~3x fewer listing
    # bytes across the roster's 174 integer fields (the tax is per-turn).
    assert version["type"] == ["integer", "string"]
    assert version["title"] == "Version"
    assert out["properties"]["name"] == {"type": "string"}


def test_redact_episodic_from_memory_type_enum() -> None:
    schema = {
        "properties": {
            "memory_type": {
                "enum": ["episodic", "semantic", "procedural", "preference"],
                "title": "Memory Type",
            },
        },
    }
    out = _redact_episodic_from_memory_type(schema)
    enum = out["properties"]["memory_type"]["enum"]
    assert "episodic" not in enum
    assert set(enum) == {"semantic", "procedural", "preference"}
    assert out["properties"]["memory_type"]["title"] == "Memory Type"


def test_redact_episodic_nested_in_anyof() -> None:
    schema = {
        "properties": {
            "memory_type": {
                "anyOf": [
                    {"enum": ["episodic", "semantic"]},
                    {"type": "null"},
                ],
            },
        },
    }
    out = _redact_episodic_from_memory_type(schema)
    assert out["properties"]["memory_type"]["anyOf"][0]["enum"] == ["semantic"]


def test_redact_episodic_leaves_unrelated_enums_alone() -> None:
    schema = {"properties": {"scope": {"enum": ["private", "shared"]}}}
    assert _redact_episodic_from_memory_type(schema) == schema


def test_shape_schema_strips_titles_and_identity_props() -> None:
    from server.transport.mcp_schemas import _shape_schema, _strip_org_from_input_schema

    schema = {
        "properties": {
            "query": {"type": "string", "title": "Query"},
            "org": {"type": "string"},
            "agent": {"type": "string"},
            "user_id": {"type": "string"},
            "session_id": {"type": "string"},
            "consumer": {"type": "string"},
            "namespace": {"type": "string", "title": "Namespace"},
        },
        "required": ["query", "org", "namespace"],
        "title": "CodeSearchInput",
    }
    out = _strip_org_from_input_schema(_shape_schema(schema))
    # titles are per-turn listing noise (694 across the roster) — gone
    assert "title" not in str(out)
    # server-resolved attribution never reaches the LLM's surface...
    for hidden in ("org", "agent", "user_id", "session_id", "consumer"):
        assert hidden not in out["properties"]
    # ...but namespace stays as the power-user override, just not required
    assert "namespace" in out["properties"]
    assert out["required"] == ["query"]
