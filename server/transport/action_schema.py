"""The action schema: loom's operation registry as a manifest and as Cedar.

A policy engine for agent tool calls can treat an MCP `tools/list` manifest
as its action schema — one Cedar action per tool, `context.input` from
`inputSchema`, `context.output` from `outputSchema`. The live listing is
curated per toolset and the compact profile drops `outputSchema`, so it
does not describe the whole surface for a gateway that wants to govern it.

This module derives two artifacts from `ALL_OPERATIONS`, the single source
of truth:

* a **manifest** — MCP `tools/list`-shaped (`name`, `description`,
  `inputSchema`, `outputSchema`) plus what a policy engine needs and MCP has
  no field for: the loom operation name, `required_role`, the effect class
  (`read` / `write` / `destructive`, from the same classifier the MCP
  annotations use), idempotency, whether success is audited, and the HTTP
  path. A tool that reads an MCP manifest consumes it as-is.
* a **Cedar schema** — namespace `Loom`: `Agent` principals, `Namespace`
  resources, a `SystemContext`, one common record type per input and output
  model, three action groups (`Read`, `Write`, `Destructive in [Write]`),
  and one action per operation `in` its group with `context: { input,
  output?, system }` — the layout a policy engine derives request and
  response events from.

JSON Schema → Cedar: `string` → `String`, `integer` → `Long`, `number` →
`decimal`, `boolean` → `Bool`, `array` → `Set<T>`, `object` with properties →
an inline record (optional attributes marked `?`), open objects and anything
unmappable → `{}` / `String` so the schema always parses. `$ref`s resolve
through `$defs`; `anyOf` with `null` is optionality.

Pure: no I/O; the CLI (`loom export-actions`) writes the text. It lives with the
transport because it is a view of the MCP surface (descriptions, annotations,
schema shaping).
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel

from server.operations.base import Operation

NAMESPACE = "Loom"
_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_MAX_DEPTH = 8


# --------------------------------------------------------------------------
# Manifest
# --------------------------------------------------------------------------


def effect_class(op: Operation) -> str:
    """`read` / `write` / `destructive` — the MCP annotation classifier's
    verdict, so the schema and the tool list never disagree."""
    from server.transport.mcp_tools import _tool_annotations

    ann = _tool_annotations(op)
    if ann["readOnlyHint"]:
        return "read"
    return "destructive" if ann["destructiveHint"] else "write"


def _description(op: Operation, listed: set[str]) -> str:
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS
    from server.transport.mcp_op import name_ops_for_listing

    custom = TOOL_DESCRIPTIONS.get(op.name)
    if custom:
        # an op in the manifest is named by its tool name, any other by its op
        # name; a manifest lists no loom_op, so nothing is marked and writes is unused
        return name_ops_for_listing(str(custom).strip(), listed, writes=False)
    return (
        f"Loom operation `{op.name}` "
        f"({'idempotent' if op.idempotent else 'state-transition'}, "
        f"min role {op.required_role.name})"
    )


def _mcp_visible(op: Operation) -> bool:
    from server.operations.toolsets import _MCP_HIDDEN_OPS

    return op.name not in _MCP_HIDDEN_OPS


def build_manifest(operations: list[Operation] | None = None) -> dict[str, Any]:
    from server.operations.registry import ALL_OPERATIONS
    from server.transport.mcp_schemas import strip_titles

    ops = operations if operations is not None else list(ALL_OPERATIONS)
    tools: list[dict[str, Any]] = []
    names = {op.mcp_tool_name for op in ops}
    for op in sorted(ops, key=lambda o: o.name):
        tools.append(
            {
                "name": op.mcp_tool_name,
                "description": _description(op, names),
                "inputSchema": strip_titles(op.input_model.model_json_schema()),
                "outputSchema": strip_titles(op.output_model.model_json_schema()),
                "loom": {
                    "operation": op.name,
                    "http_path": op.http_path,
                    "required_role": op.required_role.name,
                    "effect": effect_class(op),
                    "idempotent": bool(op.idempotent),
                    "audited": bool(op.audit),
                    "mcp_visible": _mcp_visible(op),
                    "input_model": op.input_model.__name__,
                    "output_model": op.output_model.__name__,
                },
            }
        )
    return {
        "version": 1,
        "namespace": NAMESPACE,
        "generated_from": "server.operations.registry.ALL_OPERATIONS",
        "tools": tools,
    }


# --------------------------------------------------------------------------
# JSON Schema → Cedar
# --------------------------------------------------------------------------


def _attr(name: str) -> str:
    return name if _IDENT.match(name) else json.dumps(name)


def _resolve_ref(ref: str, defs: dict[str, Any]) -> dict[str, Any] | None:
    if not ref.startswith("#/$defs/"):
        return None
    return defs.get(ref[len("#/$defs/") :])


def _non_null(variants: list[Any]) -> list[Any]:
    return [v for v in variants if not (isinstance(v, dict) and v.get("type") == "null")]


def cedar_type(node: Any, defs: dict[str, Any], depth: int = 0) -> tuple[str, bool]:
    """Render one schema node as `(cedar_type_text, nullable)`."""
    if depth > _MAX_DEPTH or not isinstance(node, dict):
        return "String", False
    if "$ref" in node:
        target = _resolve_ref(str(node["$ref"]), defs)
        return cedar_type(target, defs, depth + 1) if target is not None else ("String", False)
    for key in ("anyOf", "oneOf"):
        if key in node and isinstance(node[key], list):
            variants = node[key]
            kept = _non_null(variants)
            nullable = len(kept) != len(variants)
            if not kept:
                return "String", True
            if len(kept) == 1:
                t, n = cedar_type(kept[0], defs, depth + 1)
                return t, n or nullable
            rendered = {cedar_type(v, defs, depth + 1)[0] for v in kept}
            return (rendered.pop() if len(rendered) == 1 else "String"), nullable
    if "allOf" in node and isinstance(node["allOf"], list) and node["allOf"]:
        return cedar_type(node["allOf"][0], defs, depth + 1)
    if "enum" in node or "const" in node:
        values = node.get("enum", [node.get("const")])
        if all(isinstance(v, bool) for v in values):
            return "Bool", False
        if all(isinstance(v, int) and not isinstance(v, bool) for v in values):
            return "Long", False
        return "String", False
    typ = node.get("type")
    if isinstance(typ, list):
        kinds = [x for x in typ if x != "null"]
        nullable = len(kinds) != len(typ)
        if len(kinds) == 1:
            inner, _ = cedar_type({**node, "type": kinds[0]}, defs, depth + 1)
            return inner, nullable
        return "String", nullable
    if typ == "string":
        return "String", False
    if typ == "integer":
        return "Long", False
    if typ == "number":
        return "decimal", False
    if typ == "boolean":
        return "Bool", False
    if typ == "array":
        inner, _ = cedar_type(node.get("items", {}), defs, depth + 1)
        return f"Set<{inner}>", False
    if typ == "object" or "properties" in node:
        props = node.get("properties")
        if not isinstance(props, dict) or not props:
            return "{}", False
        required = set(node.get("required", []) or [])
        return _record(props, required, defs, depth + 1), False
    return "String", False


def _record(props: dict[str, Any], required: set[str], defs: dict[str, Any], depth: int) -> str:
    parts = []
    for name in sorted(props):
        t, nullable = cedar_type(props[name], defs, depth)
        optional = name not in required or nullable
        parts.append(f"{_attr(name)}{'?' if optional else ''}: {t}")
    return "{ " + ", ".join(parts) + " }"


def model_record(schema: dict[str, Any]) -> str:
    """A Pydantic model's JSON schema as a Cedar record type body."""
    defs = schema.get("$defs", {}) if isinstance(schema, dict) else {}
    body, _ = cedar_type({k: v for k, v in schema.items() if k != "$defs"}, defs)
    return body


# --------------------------------------------------------------------------
# Cedar schema text
# --------------------------------------------------------------------------

_HEADER = """// {ns} action schema — generated by `loom export-actions --format cedar`
// from server.operations.registry.ALL_OPERATIONS. Do not hand-edit.
//
// One action per loom operation, in its effect group (Read / Write /
// Destructive), with context laid out as { input, output?, system } — the
// convention request/response events are derived from. The JSON manifest
// (`--format json`) carries the same surface for a tool that generates its
// own rendering from an MCP manifest.
"""


def _type_name(model_name: str, taken: dict[str, str], qualified: str) -> str:
    """Stable Cedar identifier for a model class; suffix on a name clash."""
    base = re.sub(r"[^A-Za-z0-9_]", "_", model_name) or "Record"
    if not re.match(r"^[A-Za-z_]", base):
        base = "T_" + base
    name = base
    n = 2
    while name in taken and taken[name] != qualified:
        name = f"{base}{n}"
        n += 1
    taken[name] = qualified
    return name


def to_cedar_schema(operations: list[Operation] | None = None, namespace: str = NAMESPACE) -> str:
    from server.operations.registry import ALL_OPERATIONS

    source = operations if operations is not None else list(ALL_OPERATIONS)
    ops = sorted(source, key=lambda o: o.name)
    taken: dict[str, str] = {}
    type_lines: list[str] = []
    action_lines: list[str] = []
    seen_types: set[str] = set()

    def _declare(model: type[BaseModel]) -> str:
        qualified = f"{model.__module__}.{model.__qualname__}"
        name = _type_name(model.__name__, taken, qualified)
        if name not in seen_types:
            seen_types.add(name)
            type_lines.append(f"  type {name} = {model_record(model.model_json_schema())};")
        return name

    for op in ops:
        in_t = _declare(op.input_model)
        out_t = _declare(op.output_model)
        group = {"read": "Read", "write": "Write", "destructive": "Destructive"}[effect_class(op)]
        action_lines.append(
            f"  // {op.required_role.name}{', audited' if op.audit else ''}"
            f"{', idempotent' if op.idempotent else ''}\n"
            f'  action "{op.name}" in [Action::"{group}"] appliesTo {{\n'
            f"    principal: [Agent],\n"
            f"    resource: [Namespace],\n"
            f"    context: {{ input: {in_t}, output?: {out_t}, system: SystemContext }}\n"
            f"  }};"
        )

    lines = [
        _HEADER.replace("{ns}", namespace),
        f"namespace {namespace} {{",
        "  // The caller: an agent (or human) principal as loom identifies it.",
        "  entity Agent = { id: String, org: String, user_id?: String, session_id?: String,"
        " roles: Set<String> } tags String;",
        "  // The resource every operation acts in: a namespace of an org.",
        "  entity Namespace = { org: String, name: String, kind: String, visibility: String };",
        "  // Request-scoped facts every action carries.",
        "  type SystemContext = { now: datetime, request_id: String, transport: String };",
        "",
        "  // Effect groups (the MCP annotation classifier's verdict per operation).",
        '  action "Read";',
        '  action "Write";',
        '  action "Destructive" in [Action::"Write"];',
        "",
        "  // One common type per input / output model.",
        *type_lines,
        "",
        "  // One action per operation.",
        *action_lines,
        "}",
        "",
    ]
    return "\n".join(lines)


def to_manifest_json(operations: list[Operation] | None = None) -> str:
    return json.dumps(build_manifest(operations), indent=2, sort_keys=False) + "\n"


__all__ = [
    "NAMESPACE",
    "build_manifest",
    "cedar_type",
    "effect_class",
    "model_record",
    "to_cedar_schema",
    "to_manifest_json",
]
