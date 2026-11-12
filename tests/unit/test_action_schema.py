"""The action schema: the manifest and the Cedar text derive
from ALL_OPERATIONS, one action per operation, effect groups from the same
classifier the MCP annotations use, JSON Schema → Cedar type mapping."""

from __future__ import annotations

import json
import re

import pytest

from server.operations.registry import ALL_OPERATIONS
from server.transport import action_schema as A


def test_manifest_has_one_tool_per_operation_with_loom_extras():
    m = A.build_manifest()
    assert m["version"] == 1 and m["namespace"] == "Loom"
    assert len(m["tools"]) == len(ALL_OPERATIONS)
    names = [t["loom"]["operation"] for t in m["tools"]]
    assert names == sorted(names) and len(set(names)) == len(names)
    by_op = {t["loom"]["operation"]: t for t in m["tools"]}
    t = by_op["memory.set"]
    assert t["name"] == "loom_memory_set"
    assert t["inputSchema"]["type"] == "object" and "memory_key" in t["inputSchema"]["properties"]
    assert "outputSchema" in t and t["outputSchema"]["type"] == "object"
    assert t["loom"]["required_role"] == "WRITER" and t["loom"]["effect"] == "write"
    assert t["loom"]["http_path"] == "/v1/memory.set"
    assert by_op["memory.get"]["loom"]["effect"] == "read"
    assert by_op["memory.delete"]["loom"]["effect"] == "destructive"
    # no pydantic titles ride along (listing bytes)
    assert "title" not in t["inputSchema"]
    # a description names the other ops by their tool names, as a listing would
    assert "loom_memory_set" in by_op["memory.apply"]["description"]
    assert "memory.set" not in by_op["memory.apply"]["description"]
    # the manifest round-trips as JSON
    json.loads(A.to_manifest_json())


def test_effect_class_agrees_with_mcp_annotations():
    from server.transport.mcp_tools import _tool_annotations

    for op in ALL_OPERATIONS:
        ann = _tool_annotations(op)
        eff = A.effect_class(op)
        if ann["readOnlyHint"]:
            assert eff == "read", op.name
        elif ann["destructiveHint"]:
            assert eff == "destructive", op.name
        else:
            assert eff == "write", op.name


@pytest.mark.parametrize(
    "node,expected",
    [
        ({"type": "string"}, ("String", False)),
        ({"type": "integer"}, ("Long", False)),
        ({"type": "number"}, ("decimal", False)),
        ({"type": "boolean"}, ("Bool", False)),
        ({"type": "array", "items": {"type": "string"}}, ("Set<String>", False)),
        ({"type": "array"}, ("Set<String>", False)),
        ({"anyOf": [{"type": "integer"}, {"type": "null"}]}, ("Long", True)),
        ({"anyOf": [{"type": "integer"}, {"type": "string"}]}, ("String", False)),
        ({"type": ["string", "null"]}, ("String", True)),
        ({"enum": ["a", "b"]}, ("String", False)),
        ({"enum": [1, 2]}, ("Long", False)),
        ({"type": "object"}, ("{}", False)),
        ({"type": "object", "additionalProperties": {"type": "string"}}, ("{}", False)),
        (
            {
                "type": "object",
                "properties": {
                    "b": {"type": "integer"},
                    "a": {"type": "string"},
                    "x-y": {"type": "boolean"},
                },
                "required": ["a"],
            },
            ('{ a: String, b?: Long, "x-y"?: Bool }', False),
        ),
        ({"$ref": "#/$defs/Missing"}, ("String", False)),
        ("garbage", ("String", False)),
    ],
)
def test_cedar_type_mapping(node, expected):
    assert A.cedar_type(node, {}) == expected


def test_refs_resolve_through_defs_and_recursion_is_bounded():
    defs = {
        "Inner": {"type": "object", "properties": {"n": {"type": "integer"}}, "required": ["n"]},
        "Loop": {"type": "object", "properties": {"again": {"$ref": "#/$defs/Loop"}}},
    }
    assert A.cedar_type({"$ref": "#/$defs/Inner"}, defs) == ("{ n: Long }", False)
    text, _ = A.cedar_type({"$ref": "#/$defs/Loop"}, defs)
    assert text.startswith("{ again?: {") and text.count("{") <= 12


def test_cedar_schema_declares_every_operation_once_in_its_group():
    text = A.to_cedar_schema()
    assert text.startswith("// Loom action schema")
    assert "namespace Loom {" in text and text.rstrip().endswith("}")
    assert text.count("{") == text.count("}"), "unbalanced braces"
    assert 'action "Read";' in text and 'action "Write";' in text
    assert 'action "Destructive" in [Action::"Write"];' in text
    for op in ALL_OPERATIONS:
        group = {"read": "Read", "write": "Write", "destructive": "Destructive"}[A.effect_class(op)]
        decl = f'action "{op.name}" in [Action::"{group}"] appliesTo {{'
        assert text.count(decl) == 1, op.name
    # every action's context follows the input / output? / system layout
    assert text.count("context: { input: ") == len(ALL_OPERATIONS)
    assert text.count(", output?: ") == len(ALL_OPERATIONS)
    assert text.count(", system: SystemContext }") == len(ALL_OPERATIONS)


def test_common_types_are_unique_identifiers_and_referenced():
    text = A.to_cedar_schema()
    types = re.findall(r"^  type ([A-Za-z_][A-Za-z0-9_]*) = ", text, flags=re.M)
    assert types and len(types) == len(set(types)), "duplicate common type"
    for name in types:
        assert re.search(rf"\b(input|output\?|system): {name}\b", text), (
            f"{name} declared but unused"
        )
    # a shared model is declared once, however many operations use it
    body = text[text.index("  // One common type") :]
    assert body.count("type SystemContext") == 0  # declared in the header block, not per model


def test_model_record_marks_optional_and_nullable_fields():
    from pydantic import BaseModel

    class M(BaseModel):
        a: str
        b: int | None = None
        c: list[str] = []
        d: dict[str, int] = {}

    rec = A.model_record(M.model_json_schema())
    assert rec == "{ a: String, b?: Long, c?: Set<String>, d?: {} }"
