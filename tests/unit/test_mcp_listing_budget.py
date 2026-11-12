"""The tools/list budget: the default profile carries no
outputSchema, the full inputSchema survives, no listed description points a
model at a tool it cannot call, one revision policy, and the listing stays
under a measured token ceiling (chars/4)."""

from __future__ import annotations

import asyncio
import json
import re

import pytest

from server.operations.registry import ALL_OPERATIONS
from server.operations.toolsets import (
    _MCP_ATOMIC_OPS,
    _MCP_CODE_CORE_OPS,
    _MCP_CODING_OPS,
    _MCP_CORE_OPS,
    _MCP_HIDDEN_OPS,
)
from server.settings import settings
from server.transport import mcp_tools as mt
from server.transport.mcp_descriptions import (
    ATOMIC_TOOL_DESCRIPTIONS,
    REVISION_POLICY,
    TOOL_DESCRIPTIONS,
    revision_policy,
)
from server.transport.mcp_schemas import (
    DEFAULT_SCHEMA_PROFILE,
    SCHEMA_PROFILES,
    apply_schema_profile,
    collapse_nullable,
)

OPS = {op.name: op for op in ALL_OPERATIONS}
TOOL_TO_OP = {op.mcp_tool_name: op.name for op in ALL_OPERATIONS}


def _listing(toolset: str, code: bool, profile: str | None = None) -> list[dict]:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    monkey.setattr(settings, "mcp_code_tools", code)
    monkey.setattr(settings, "mcp_schema_profile", DEFAULT_SCHEMA_PROFILE)
    try:
        return asyncio.run(mt._tools_descriptor(schema_profile=profile))
    finally:
        monkey.undo()


def _tok(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"))) // 4


# ---------------------------------------------------------------- profiles


def test_default_profile_is_standard_and_drops_output_schema_only():
    assert DEFAULT_SCHEMA_PROFILE == "standard" and settings.mcp_schema_profile == "standard"
    assert SCHEMA_PROFILES == ("standard", "full", "compact")
    standard = _listing("core", True)
    full = _listing("core", True, "full")
    assert [t["name"] for t in standard] == [t["name"] for t in full]
    assert not any("outputSchema" in t for t in standard)
    assert all("outputSchema" in t for t in full), "?schema_profile=full keeps the old shape"
    for s, f in zip(standard, full, strict=True):
        # everything but outputSchema is byte-identical: full inputSchema, prose, annotations
        assert s == {k: v for k, v in f.items() if k != "outputSchema"}
    assert apply_schema_profile(full, "bogus") == standard, "an unknown profile is the default"


def test_collapse_nullable_folds_optional_and_inert_defaults_only():
    schema = {
        "type": "object",
        "properties": {
            "limit": {
                "anyOf": [{"type": ["integer", "string"], "minimum": 1}, {"type": "null"}],
                "default": None,
                "description": "Cap.",
            },
            "run_id": {"type": "string", "default": "-"},
            "namespace": {"type": "string", "default": ""},
            "dry_run": {"type": "boolean", "default": True},
            "min_fan_in": {"type": "integer", "default": 0},
            "mode": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
            "target": {"anyOf": [{"$ref": "#/$defs/T"}, {"type": "null"}], "default": None},
            "meta": {"type": "object", "additionalProperties": True},
            "args": {"type": "object", "additionalProperties": False},
            "fresh": {"type": "boolean", "default": False},
        },
    }
    out = collapse_nullable(schema)["properties"]
    assert out["limit"] == {"type": ["integer", "string"], "minimum": 1, "description": "Cap."}
    assert out["run_id"] == {"type": "string"} and out["namespace"] == {"type": "string"}
    assert out["dry_run"] == {"type": "boolean", "default": True}, "informative defaults stay"
    assert out["min_fan_in"] == {"type": "integer", "default": 0}
    assert out["mode"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}, "a real union stays"
    assert out["target"] == {"$ref": "#/$defs/T"}
    assert out["meta"] == {"type": "object"}, (
        "additionalProperties: true is the JSON-Schema default"
    )
    assert out["args"] == {"type": "object", "additionalProperties": False}
    assert out["fresh"] == {"type": "boolean"}, "an unset flag is off"


def test_input_schema_keeps_every_argument_and_field_description():
    """Full inputSchema: the argument tree and its prose are what the compact
    profile cuts and the standard profile keeps."""
    by = {t["name"]: t for t in _listing("core", True)}
    apply = by["loom_memory_apply"]["inputSchema"]
    assert {"target", "action", "reason", "limit", "dry_run"} <= set(apply["properties"])
    assert "$defs" in apply and "ApplyTarget" in apply["$defs"]
    assert by["loom_search"]["inputSchema"]["properties"]["page"].get("description")
    full = {t["name"]: t for t in _listing("core", True, "full")}
    n_field_desc = lambda t: json.dumps(t["inputSchema"]).count('"description"')  # noqa: E731
    assert all(n_field_desc(by[n]) == n_field_desc(full[n]) for n in by), "no field prose is cut"

    # pydantic's title metadata is gone; a FIELD named title (task.create)
    # is an argument and stays
    def _title_metadata(node, in_props=False):
        if isinstance(node, dict):
            return any(
                (k == "title" and not in_props and isinstance(v, str))
                or _title_metadata(v, k == "properties")
                for k, v in node.items()
            )
        return isinstance(node, list) and any(_title_metadata(v) for v in node)

    assert not any(_title_metadata(t["inputSchema"]) for t in by.values())
    tasks = {t["name"]: t for t in _listing("tasks", False)}
    assert "title" in tasks["loom_task_create"]["inputSchema"]["properties"]
    # the input model's class docstring is not an argument: gone where a curated description exists
    assert "description" not in by["loom_memory_update"]["inputSchema"]


# ---------------------------------------------------------------- reference lint

_OP_REF = re.compile(r"\b[a-z_]+(?:\.[a-z_]+){1,2}\b")
_TOOL_REF = re.compile(r"\bloom_[a-z_]+\b")
_CALLABLE_BY_NAME = re.compile(r"\(([^()]*?)callable by name\)")


def _op_refs(text: str) -> set[str]:
    refs = {r for r in _OP_REF.findall(text) if r in OPS}
    refs |= {TOOL_TO_OP[r] for r in _TOOL_REF.findall(text) if r in TOOL_TO_OP}
    return refs


def _marked_callable_by_name(text: str) -> set[str]:
    """Ops named in a sentence that carries "callable by name" or "via
    loom_op" — the sanctioned ways for a listed description to point at an
    unlisted op (loom_op is how a client reaches one)."""
    out: set[str] = set()
    for m in re.finditer(r"callable by name|via loom_op", text):
        head = text[: m.start()]
        cut = max(head.rfind(". "), head.rfind("\n"), head.rfind("; "))
        out |= _op_refs(head[cut + 1 :])
    return out


@pytest.mark.parametrize(
    ("toolset", "code"),
    [
        ("core", False),
        ("core", True),
        ("coding", False),
        ("atomic", False),
        ("tasks", False),
        ("tracker", False),
        ("all", False),
        ("all", True),
    ],
)
def test_no_listed_description_names_a_hidden_op(toolset: str, code: bool):
    """A hidden op is refused by tools/call — naming it as a fallback sends
    the model down a dead path."""
    for t in _listing(toolset, code):
        hidden = sorted(_op_refs(t["description"]) & _MCP_HIDDEN_OPS)
        assert not hidden, f"{toolset}: {t['name']} names hidden {hidden}"


@pytest.mark.parametrize(
    ("toolset", "code"),
    [
        ("core", False),
        ("core", True),
        ("coding", False),
        ("atomic", False),
        ("tasks", False),
        ("tracker", False),
    ],
)
def test_no_listed_description_names_an_unlisted_op_without_saying_so(toolset: str, code: bool):
    """Under a curated toolset a description may point at an op the listing
    does not carry only if it says "(callable by name)" — most clients refuse
    a tool name they were not listed."""
    tools = _listing(toolset, code)
    listed = {TOOL_TO_OP.get(t["name"], t["name"]) for t in tools}
    for t in tools:
        text = t["description"]
        unlisted = _op_refs(text) - listed - _marked_callable_by_name(text)
        assert not unlisted, f"{toolset}: {t['name']} names unlisted {sorted(unlisted)}"


# ---------------------------------------------------------------- one revision policy


def test_run_begin_names_writes_for_run_id_in_every_tier():
    """A body run_id on a read filters to the run's rows, so no tier tells an agent to
    pass it on every memory call."""
    for text in (TOOL_DESCRIPTIONS["run.begin"], ATOMIC_TOOL_DESCRIPTIONS["run.begin"]):
        assert "every memory call" not in text and "memory.ask" not in text
        assert "memory.set" in text and "session's own run" in text
    assert "carries your run_id" not in ATOMIC_TOOL_DESCRIPTIONS["memory.ask"]


def test_one_revision_policy_text_everywhere():
    core_set = TOOL_DESCRIPTIONS["memory.set"]
    assert revision_policy("memory.update") in core_set
    assert revision_policy("memory.update") in TOOL_DESCRIPTIONS["memory.update"]
    assert revision_policy("loom_memory_set") in ATOMIC_TOOL_DESCRIPTIONS["memory.set"]
    assert "revision policy" in ATOMIC_TOOL_DESCRIPTIONS["memory.delete"]
    # the contradiction is gone: a changed fact is never "a NEW memory_key"
    assert "NEW memory_key" not in core_set
    assert "under that same key" in REVISION_POLICY and "second copy" in REVISION_POLICY
    # the load-bearing sentences the trim kept
    for must in (
        "Call this proactively",
        "Don't search before saving",
        "one call per fact",
        "Auto-tag",
        "Do NOT save",
        "ttl_seconds",
        "on_expire",
        "ONE namespace",
    ):
        assert must in core_set, must


# ---------------------------------------------------------------- counts derive from the registry


def test_roster_sizes_derive_from_the_registry():
    synthetic = 3  # loom_search, loom_activate, loom_op
    assert len(_listing("core", False)) == len(_MCP_CORE_OPS) + synthetic
    assert len(_listing("core", True)) == len(_MCP_CORE_OPS | _MCP_CODE_CORE_OPS) + synthetic
    assert len(_listing("atomic", True)) == len(_MCP_ATOMIC_OPS) + synthetic
    # The coding tier carries the code tools regardless of the flag
    assert len(_listing("coding", False)) == len(_MCP_CODING_OPS | _MCP_CODE_CORE_OPS) + synthetic
    assert len(_listing("coding", True)) == len(_MCP_CODING_OPS | _MCP_CODE_CORE_OPS) + synthetic
    # `all` lists the stable surface plus every curated-tier op.
    visible_all = [
        op
        for op in ALL_OPERATIONS
        if op.name not in _MCP_HIDDEN_OPS
        and not op.name.startswith("code.")
        and (op.maturity == "stable" or op.name in _MCP_CORE_OPS | _MCP_ATOMIC_OPS)
    ]
    assert len(_listing("all", False)) == len(visible_all) + synthetic
    assert len(visible_all) + synthetic <= 120, "the `all` tier ceiling"


# ---------------------------------------------------------------- budget


def test_listing_budget_chars_over_four():
    """The ceilings sit a little above the current listings; a new tool or a
    long description pays for itself here."""
    assert _tok({"tools": _listing("core", True)}) <= 9_300
    assert _tok({"tools": _listing("core", False)}) <= 5_800
    assert _tok({"tools": _listing("coding", False)}) <= 8_850
    assert _tok({"tools": _listing("atomic", False)}) <= 4_500
    for t in _listing("core", True):
        assert len(t["description"]) // 4 <= 480, f"{t['name']} description is over budget"


def test_tracker_toolset_lists_the_tracker_record_bisect_pr_check_and_impact_within_its_own_budget():
    """A second MCP entry for QA work; nothing lands on core/coding."""
    from server.operations.toolsets import _MCP_CODING_OPS, _MCP_CORE_OPS, _MCP_TRACKER_OPS

    qa = {t["name"]: t for t in _listing("tracker", False)}
    for name in (
        "loom_tracker_answer",
        "loom_tracker_duplicate_check",
        "loom_tracker_similar",
        "loom_tracker_test_signal",
        "loom_code_bisect_candidates",
        "loom_code_pr_check",
        "loom_code_impact",
    ):
        assert name in qa, name
    assert "loom_memory_set" not in qa and "loom_code_symbol" not in qa, (
        "QA work, not a coding superset"
    )
    # The four QA-plane ops a coding session asks for are listed on coding too
    assert (_MCP_TRACKER_OPS & _MCP_CODING_OPS) == {
        "tracker.duplicate_check",
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
    }
    # One entry may list the union, within the two budgets added
    union = _listing("core", True) and asyncio.run(mt._tools_descriptor(toolset="coding+tracker"))
    assert len(union) == len(_MCP_CODING_OPS | _MCP_CODE_CORE_OPS | _MCP_TRACKER_OPS) + 3
    assert _tok({"tools": union}) <= 12_000
    assert "tracker.answer" not in _MCP_CORE_OPS
    assert _tok({"tools": _listing("tracker", False)}) <= 9_000


# ---------------------------------------------------------------- unlisted fields


def test_marked_fields_are_unlisted_but_still_accepted():
    """A field carrying MCP_HIDDEN is absent from every listed
    schema, the op's model still validates it (HTTP / SDK / MCP by name), and
    nothing a curated description or skill tells an agent to pass is marked."""
    from server.operations.base import MCP_HIDDEN

    # `all` carries every core tool plus the stable ops callable by name there.
    listed = {t["name"]: t for t in _listing("all", True)}
    marked: dict[str, set[str]] = {}
    for op in ALL_OPERATIONS:
        fields = {
            k
            for k, f in op.input_model.model_fields.items()
            if (f.json_schema_extra or {}) == MCP_HIDDEN
        }
        if fields:
            marked[op.name] = fields
            tool = listed.get(op.mcp_tool_name)
            if tool:
                props = set(tool["inputSchema"].get("properties", {}))
                assert not (props & fields), (op.name, props & fields)
                assert "x-mcp-hidden" not in json.dumps(tool["inputSchema"])
    assert {"kind_prior", "vector_shortlist", "rerank"} <= marked["code.search"]
    assert {"classify", "source_trace", "char_limit"} <= marked["memory.set"]
    # hidden is unlisted, not removed: the model still takes the argument
    m = OPS["code.search"].input_model.model_validate(
        {"org": "o", "namespace": "n", "query": "q", "kind_prior": "flat", "vector_shortlist": 50}
    )
    assert m.kind_prior == "flat" and m.vector_shortlist == 50
    # the fields a description or a skill names stay listed
    keep = {
        "code.search": {"query", "expand", "namespaces", "top_k", "cursor"},
        "memory.set": {
            "dry_run",
            "ttl_seconds",
            "on_expire",
            "entities",
            "memory_type",
            "confidence",
            "source_resource_id",
            "if_match_sha256",
        },
        "memory.set_from_messages": {"defer", "recalled_keys", "record_schema"},
        "context.pack": {"include_runs", "only_sections", "token_budget"},
        "code.callers": {"edge_kinds", "include_ambiguous", "max_nodes"},
    }
    for op_name, names in keep.items():
        props = set(listed[OPS[op_name].mcp_tool_name]["inputSchema"]["properties"])
        assert names <= props, (op_name, names - props)
    # memory.pin forces pinned on; memory.set (same model) still lists it
    assert "pinned" not in listed["loom_memory_pin"]["inputSchema"]["properties"]
    assert "pinned" in listed["loom_memory_set"]["inputSchema"]["properties"]
