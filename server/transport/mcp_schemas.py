"""MCP transport — JSON-Schema transformation for tool descriptors.

Leaf module: pure schema-walking helpers, no imports back into
``server.transport.mcp``. ``mcp_tools`` builds descriptors with these.
"""

from __future__ import annotations

import copy
import functools
from collections.abc import Callable
from typing import Any

from pydantic import BaseModel


@functools.cache
def _generated_schema(model: type[BaseModel]) -> dict[str, Any]:
    return model.model_json_schema()


def model_schema(model: type[BaseModel]) -> dict[str, Any]:
    """`model.model_json_schema()`, generated once per model class and handed
    out as a fresh copy. Pydantic regenerates a schema on every call, which
    would be ~85% of a tools/list build (~38 ms for the core listing, ~106 ms
    for `all`, on the event loop, per request); an op model does not change
    after import."""
    return copy.deepcopy(_generated_schema(model))


def _walk_schema(schema: Any, leaf: Callable[[dict[str, Any]], dict[str, Any] | None]) -> Any:
    """Recursively walk a JSON Schema, applying `leaf` at every dict node.

    `leaf(node)` returns a replacement dict (recursion stops there) or None
    to descend into children. One recursion behind all per-node fixes below.
    """
    if isinstance(schema, dict):
        replaced = leaf(schema)
        if replaced is not None:
            return replaced
        return {k: _walk_schema(v, leaf) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_walk_schema(v, leaf) for v in schema]
    return schema


def _leaf_redact_episodic(node: dict[str, Any]) -> dict[str, Any] | None:
    """Strip `"episodic"` from any `memory_type` enum the LLM sees.

    Episodic memories aren't searchable (collapsed into `agent_events`), so an
    MCP model has no reason to write one; the wire type stays because the
    capture hooks write `episodic` rows over HTTP.
    """
    enum = node.get("enum")
    if isinstance(enum, list) and "episodic" in enum:
        return {**node, "enum": [v for v in enum if v != "episodic"]}
    return None


def _leaf_strip_datetime(node: dict[str, Any]) -> dict[str, Any] | None:
    """Drop `"format": "date-time"` from string fields.

    Pydantic serializes naive CH DateTimes without a UTC offset, which strict
    client validators reject for a `date-time` field. Keep `type: string`, drop
    the format promise; the value still carries the timestamp.
    """
    if node.get("type") == "string" and node.get("format") == "date-time":
        out = dict(node)
        out.pop("format", None)
        return out
    return None


def _leaf_widen_integer(node: dict[str, Any]) -> dict[str, Any] | None:
    """Widen `{type: integer}` to `{type: [integer, string]}` so clients
    accept the string fallback `_coerce_bigints` emits for >2^53 values.

    The type-ARRAY form (core JSON Schema), not `anyOf` blocks: the schema
    tax is paid on every turn of every MCP session, and the anyOf spelling
    of the same semantics is ~3x the bytes across the roster's integer
    fields."""
    if node.get("type") == "integer" and "anyOf" not in node:
        return {**node, "type": ["integer", "string"]}
    return None


def strip_titles(schema: Any, *, _in_properties: bool = False) -> Any:
    """Drop pydantic's auto-generated per-field `title` keys everywhere.

    694 of them across the roster ("Top K", "User Id", …) — pure token
    noise for an LLM that already sees the property name. Field semantics
    live in `description`; titles carry nothing. A key inside a
    ``properties`` mapping is a field NAME, so a model with a field called
    ``title`` (``task.create``) keeps it — only the metadata
    key goes."""
    if isinstance(schema, dict):
        return {
            k: strip_titles(v, _in_properties=(k == "properties"))
            for k, v in schema.items()
            if _in_properties or k != "title"
        }
    if isinstance(schema, list):
        return [strip_titles(v) for v in schema]
    return schema


# Match disjoint node types, so application order isn't load-bearing.
_SCHEMA_LEAF_RULES: tuple[Callable[[dict[str, Any]], dict[str, Any] | None], ...] = (
    _leaf_strip_datetime,
    _leaf_widen_integer,
    _leaf_redact_episodic,
)


# Defaults that tell a model nothing it would not assume from omitting the
# field: pydantic's `None`, the empty string, the "-" identity sentinel
# (run_id / agent placeholders the server fills) and `false` on an optional
# flag (an unset flag is off; `true` defaults stay — they say the behaviour
# is on unless the caller turns it off). The core + code listing carries
# ~80 `default: null` and ~100 empty-string defaults.
_INERT_DEFAULTS: tuple[Any, ...] = (None, "", "-")


def collapse_nullable(schema: Any) -> Any:
    """Fold pydantic's Optional spelling — `anyOf: [T, {type: null}]` plus
    `default: null` — into T, drop inert defaults everywhere, and drop
    `additionalProperties: true` (the JSON-Schema default, spelled out by
    pydantic on every dict field; `false` stays).

    The wire keeps accepting null and the empty string; this is the listing
    shape only. An Optional field is one a caller may omit, and omission is
    how a model leaves a field unset — the null branch and the null default
    spend ~41 bytes per field (~80 fields on the core + code listing) to say
    so twice. Runs after the per-node rules so a widened
    integer or a redacted enum inside the surviving branch is kept."""
    if isinstance(schema, list):
        return [collapse_nullable(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    node = dict(schema)
    any_of = node.get("anyOf")
    if isinstance(any_of, list) and len(any_of) == 2:
        branches = [b for b in any_of if b != {"type": "null"}]
        if len(branches) == 1 and isinstance(branches[0], dict):
            node.pop("anyOf")
            node = {**branches[0], **node}
    if "default" in node:
        d = node["default"]
        inert = d is False or (
            not isinstance(d, bool) and any(d is x or d == x for x in _INERT_DEFAULTS)
        )
        if inert:
            node.pop("default")
    if node.get("additionalProperties") is True:
        node.pop("additionalProperties")
    return {k: collapse_nullable(v) for k, v in node.items()}


def strip_model_docstring(schema: Any) -> Any:
    """Drop the input model's class docstring that pydantic copies into the
    top-level `description` of an inputSchema. It is written for developers
    (env flags, auth internals) and sits beside the curated
    tool description the model is meant to read; on the core + code listing
    the sixteen of them cost 1,244 tokens. Called only for tools that carry
    a curated description — the metadata fallback keeps the docstring, which
    is then the only prose the tool has."""
    if isinstance(schema, dict) and "description" in schema:
        return {k: v for k, v in schema.items() if k != "description"}
    return schema


def _shape_schema(schema: Any) -> Any:
    """Apply the per-node MCP schema fixes (datetime / bigint / episodic),
    fold Optional wrappers and inert defaults, then strip pydantic titles —
    listing bytes are a per-turn tax."""
    for leaf in _SCHEMA_LEAF_RULES:
        schema = _walk_schema(schema, leaf)
    return strip_titles(collapse_nullable(schema))


# Attribution ride-alongs the server resolves without the LLM's help: org is
# pinned by the JWT, agent comes from MCP clientInfo (mcp_identity), user_id/
# session_id/consumer are trace attribution with server defaults. Repeating
# them as properties on every listed tool cost thousands of listing tokens
# per turn for fields an agent should never set. The wire keeps accepting them.
_HIDDEN_IDENTITY_PROPS = ("org", "agent", "user_id", "session_id", "consumer")


def _strip_org_from_input_schema(schema: Any) -> Any:
    """Relax identity-tuple fields on MCP input schemas.

    1. ``org``/``agent``/``user_id``/``session_id``/``consumer`` removed
       from properties + required: every MCP call has org pinned by the
       JWT and the rest auto-resolved (clientInfo slug / defaults), so
       listing them only feeds the LLM's guessing loop — and costs
       listing tokens on every turn of every session.
    2. ``namespace`` removed from ``required`` (kept in ``properties`` for
       power-user override): the server auto-fills it, and strict-schema
       LLMs otherwise refuse or guess nonsense values.

    The wire-level fields stay accepted by the server — purely a schema
    shape change to keep the LLM's surface narrow.
    """
    if not isinstance(schema, dict):
        return schema
    out = dict(schema)
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: v for k, v in props.items() if k not in _HIDDEN_IDENTITY_PROPS}
    req = out.get("required")
    if isinstance(req, list):
        out["required"] = [r for r in req if r not in (*_HIDDEN_IDENTITY_PROPS, "namespace")]
    return out


_BLOB_PROPS = frozenset({"original_bytes_b64", "original_filename", "blob_content_type"})
# Per-op input fields the dashboard's HTTP path uses and an agent never should: raw file
# bytes on the knowledge writes, and the Memories view's poll watermark on memory.list.
# The wire keeps accepting them; only the MCP listing hides them — every listed property
# is paid for on every turn of every session.
_HIDDEN_OP_PROPS: dict[str, frozenset[str]] = {
    "knowledge.add": _BLOB_PROPS,
    "knowledge.upsert": _BLOB_PROPS,
    "memory.list": frozenset({"known_version", "count_only", "preview_chars"}),
    # The Memories view's chip keys
    "conflicts.list": frozenset({"keys_only"}),
    # The Memories page's leg selection
    "memory.search": frozenset({"legs"}),
    # The sidebar badge's number
    "agents.connected": frozenset({"count_only"}),
    # memory.pin forces pinned on; memory.set shares the model
    "memory.pin": frozenset({"pinned"}),
}


def _marked_hidden(schema: dict[str, Any]) -> frozenset[str]:
    """Top-level properties carrying `MCP_HIDDEN`."""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return frozenset()
    return frozenset(k for k, v in props.items() if isinstance(v, dict) and v.get("x-mcp-hidden"))


def _strip_blob_fields_from_knowledge(op_name: str, schema: Any) -> Any:
    """Hide the op's dashboard-only input fields (`_HIDDEN_OP_PROPS`) from
    its MCP schema.

    The knowledge writes accept raw base64 file bytes, which an agent can't
    practically pass through a JSON tool arg (the bytes would have to live
    in its context window first); memory.list's `known_version` is the
    Memories view's change watermark. The wire-level fields stay alive for
    the UI's HTTP path; hiding them from MCP stops the footgun and the
    listing tax.
    """
    if not isinstance(schema, dict):
        return schema
    marked = _marked_hidden(schema)
    hidden = (_HIDDEN_OP_PROPS.get(op_name) or frozenset()) | marked
    if not hidden:
        return schema
    out = dict(schema)
    props = out.get("properties")
    if isinstance(props, dict):
        out["properties"] = {k: v for k, v in props.items() if k not in hidden}
    req = out.get("required")
    if isinstance(req, list):
        out["required"] = [r for r in req if r not in hidden]
    return out


# ============================================================
# Compact schema profile
# ============================================================
#
# tools/list is a per-session tax paid by every connected client before it has done
# anything, and with the full profile much of it is outputSchema (which no MCP client
# we ship to reads), then prose descriptions and inputSchema. Names, argument names and
# short descriptions carry the invocation. The compact profile keeps exactly that: the
# first paragraph of the description, the argument tree with types / required / enum /
# bounds and a one-sentence field description, and no outputSchema. Selected per request
# (`?schema_profile=compact` on the MCP URL) or by default (LOOM_MCP_SCHEMA_PROFILE);
# the full profile is unchanged.

COMPACT_DESCRIPTION_MAX_CHARS = 280
COMPACT_FIELD_DESCRIPTION_MAX_CHARS = 120
# Schema keys that carry invocation meaning; everything else is dropped.
_COMPACT_KEEP_KEYS = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "enum",
        "const",
        "default",
        "anyOf",
        "oneOf",
        "allOf",
        "$ref",
        "$defs",
        "additionalProperties",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "minItems",
        "maxItems",
        "minLength",
        "maxLength",
        "format",
        "description",
    }
)


def _first_sentence(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "; ", " — ", ", "):
        i = cut.rfind(sep)
        if i >= limit // 2:
            return cut[: i + (1 if sep == ". " else 0)].rstrip()
    return cut.rstrip() + "…"


def _one_sentence(text: str, limit: int) -> str:
    """The first sentence only (field descriptions: what the argument IS;
    guidance about how to fill it belongs to the tool description)."""
    text = " ".join(text.split())
    i = text.find(". ")
    if i != -1:
        text = text[: i + 1]
    return _first_sentence(text, limit)


def compact_description(text: str) -> str:
    """First paragraph of a curated description, whitespace-collapsed, cut at a
    sentence boundary near COMPACT_DESCRIPTION_MAX_CHARS. A `WHAT:` prefix is
    kept as written (it is how several descriptions lead)."""
    if not text:
        return ""
    text = text.replace("**", "")
    lines = [ln.strip() for ln in text.strip().splitlines()]
    if lines and lines[0].startswith("WHAT:"):
        # Structured descriptions (WHAT / WHEN / WHEN NOT / ARGS): the WHAT
        # and WHEN lines carry the invocation; the rest is routing prose.
        keep = [ln for ln in lines if ln.startswith(("WHAT:", "WHEN:"))]
        return _first_sentence(" ".join(keep), COMPACT_DESCRIPTION_MAX_CHARS)
    para = text.strip().split("\n\n", 1)[0]
    return _first_sentence(para, COMPACT_DESCRIPTION_MAX_CHARS)


def compact_input_schema(schema: Any) -> Any:
    """Keep the argument tree and the constraints a caller needs to fill it;
    shorten per-field descriptions to one sentence; drop examples, titles and
    other prose-only keys."""
    if isinstance(schema, list):
        return [compact_input_schema(v) for v in schema]
    if not isinstance(schema, dict):
        return schema
    out: dict[str, Any] = {}
    for k, v in schema.items():
        if k not in _COMPACT_KEEP_KEYS:
            continue
        if k == "description":
            if isinstance(v, str) and v.strip():
                out[k] = _one_sentence(v, COMPACT_FIELD_DESCRIPTION_MAX_CHARS)
            continue
        if k in ("properties", "$defs") and isinstance(v, dict):
            out[k] = {name: compact_input_schema(sub) for name, sub in v.items()}
            continue
        out[k] = compact_input_schema(v)
    return out


def compact_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """The compact projection of one tools/list entry: name, annotations,
    compact description, compact inputSchema, no outputSchema."""
    out: dict[str, Any] = {
        "name": descriptor["name"],
        "description": compact_description(str(descriptor.get("description") or "")),
        "inputSchema": compact_input_schema(descriptor.get("inputSchema") or {}),
    }
    if descriptor.get("annotations"):
        out["annotations"] = descriptor["annotations"]
    if descriptor.get("_meta"):
        out["_meta"] = descriptor["_meta"]
    return out


# ============================================================
# Schema profiles
# ============================================================
#
# standard — the default: the curated description and the
#            full inputSchema (every argument, every field description) with
#            no outputSchema. No client loom ships to reads outputSchema,
#            it is about a third of the full listing, and the listing is what
#            most sessions pay for (most MCP posts carry no tool call). Same
#            tool set, same descriptions, same arguments as `full`.
# full     — standard plus outputSchema, for a host that validates results
#            against it. Opt in per request with `?schema_profile=full` or
#            per deployment with LOOM_MCP_SCHEMA_PROFILE=full.
# compact  — first paragraph + argument tree only.

SCHEMA_PROFILES = ("standard", "full", "compact")
DEFAULT_SCHEMA_PROFILE = "standard"


def standard_descriptor(descriptor: dict[str, Any]) -> dict[str, Any]:
    """The default projection: everything but outputSchema."""
    return {k: v for k, v in descriptor.items() if k != "outputSchema"}


def apply_schema_profile(descriptors: list[dict[str, Any]], profile: str) -> list[dict[str, Any]]:
    if profile == "compact":
        return [compact_descriptor(d) for d in descriptors]
    if profile == "full":
        return descriptors
    return [standard_descriptor(d) for d in descriptors]
