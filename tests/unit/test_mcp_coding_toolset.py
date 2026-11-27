"""The `coding` MCP toolset: one listing per archetype instead
of one `core` for two. Membership derives from the frozensets; the toolset is
selectable per deployment and per request; the code tools ride it without the
deployment flag; the catalogue says which toolsets list an op; the coding-tier
descriptions are the core texts trimmed of sentences a coding session does not act on;
each description stays under its budget (the listing's ceiling is in
test_mcp_listing_budget.py).

Hermetic: `_caller_default_context` (which can hit ClickHouse) is patched."""

from __future__ import annotations

import asyncio
import dataclasses
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import server.transport.mcp as mcp
import server.transport.mcp_call as mcp_call
from server.code.domain.scope import reset_code_scope, set_code_scope
from server.insights.app.mentor import named_for_caller
from server.operations.capabilities import capability_snapshot
from server.operations.reach import catalogue_facets
from server.operations.registry import ALL_OPERATIONS, OPERATIONS_BY_NAME
from server.operations.toolsets import (
    _MCP_ATOMIC_OPS,
    _MCP_CODE_CORE_OPS,
    _MCP_CODING_OPS,
    _MCP_CORE_OPS,
    _MCP_HIDDEN_OPS,
    CURATED_TOOLSETS,
    MCP_TOOLSETS,
    curated_roster,
    op_toolsets,
    resolve_toolset,
)
from server.settings import Settings, settings
from server.transport import mcp_op, mcp_primer
from server.transport import mcp_tools as mt
from server.transport.mcp_descriptions import (
    ACTIVATION_TRIGGERS,
    CODING_TOOL_DESCRIPTIONS,
    FIRST_STEP_HOOK,
    TOOL_DESCRIPTIONS,
)
from tests.unit.loom_op_marks import (
    assert_marked_once_outside_parentheses,
    synthetic_descriptors,
)

OPS = {op.name: op for op in ALL_OPERATIONS}
TOOL_TO_OP = {op.mcp_tool_name: op.name for op in ALL_OPERATIONS}
SYNTHETIC = {"loom_search", "loom_activate", "loom_op"}


@pytest.fixture(autouse=True)
def _no_ch_default_context(monkeypatch):
    async def _empty(auth, defaults=None):
        return ""

    monkeypatch.setattr(mcp_primer, "_caller_default_context", _empty)


def _listing(toolset: str, code: bool, *, requested: str | None = None) -> list[dict]:
    monkey = pytest.MonkeyPatch()
    monkey.setattr(settings, "mcp_toolset", toolset)
    monkey.setattr(settings, "mcp_code_tools", code)
    monkey.setattr(settings, "mcp_schema_profile", "standard")
    try:
        return asyncio.run(mt._tools_descriptor(toolset=requested))
    finally:
        monkey.undo()


def _names(tools: list[dict]) -> set[str]:
    return {t["name"] for t in tools}


def _shown(listed: dict[str, dict], toolset: str, name: str, source: str) -> bool:
    """Whether tool ``name`` carries ``source`` as this listing renders it."""
    text = mcp_op.name_ops_for_listing(source, set(listed), writes=mcp_op.routes_writes(toolset))
    return listed[name]["description"] == text


# ---------------------------------------------------------------- membership


def test_coding_roster_is_the_six_memory_verbs_plus_the_code_tools():
    # Memory.pin, blacklist.add and working_memory.append are not
    # listed (coding clients do not call them); they stay
    # callable by name. memory.update stays — the revision verb.
    assert _MCP_CODING_OPS == {
        "memory.set",
        "memory.update",
        "memory.get",
        "memory.delete",
        "working_memory.set",
        "context.pack",
        "tracker.duplicate_check",
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
        "code.read",
        "code.edit_context",
    }
    # The QA-plane ops a coding session asks for are
    # listed on coding without joining core
    tracker_on_coding = {
        "tracker.duplicate_check",
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
    }
    # the scratchpad is coding's own; the memory verbs are core's
    assert (
        _MCP_CODING_OPS
        - tracker_on_coding
        - {"working_memory.set", "code.read", "code.edit_context"}
        <= _MCP_CORE_OPS
    )
    assert not (_MCP_CODING_OPS & _MCP_HIDDEN_OPS)
    assert len(_MCP_CODE_CORE_OPS) == 8
    assert curated_roster("coding", code_tools=False) == _MCP_CODING_OPS | _MCP_CODE_CORE_OPS
    assert curated_roster("coding", code_tools=True) == _MCP_CODING_OPS | _MCP_CODE_CORE_OPS
    assert curated_roster("core", code_tools=False) == _MCP_CORE_OPS
    assert curated_roster("core", code_tools=True) == _MCP_CORE_OPS | _MCP_CODE_CORE_OPS
    assert curated_roster("atomic", code_tools=True) == _MCP_ATOMIC_OPS
    assert curated_roster("all", code_tools=True) is None
    # `readonly` is a projection, not a curated roster: it joins
    # MCP_TOOLSETS but not CURATED_TOOLSETS, and curated_roster returns None.
    assert curated_roster("readonly", code_tools=True) is None
    # `tasks` is the fourth curated roster (the task inbox plane)
    # `qa` is the fifth curated roster (tracker record, bisect, pr_check, impact)
    assert MCP_TOOLSETS == (
        "core",
        "coding",
        "atomic",
        "tasks",
        "tracker",
        "readonly",
        "assistant",
        "all",
    )
    assert CURATED_TOOLSETS == {"core", "coding", "atomic", "tasks", "tracker"}


def test_coding_listing_is_its_roster_plus_the_code_tools_regardless_of_the_flag():
    for code in (False, True):
        tools = _listing("coding", code)
        names = _names(tools)
        assert len(tools) == len(_MCP_CODING_OPS | _MCP_CODE_CORE_OPS) + len(SYNTHETIC) == 23
        assert "loom_code_read" in names
        assert SYNTHETIC <= names
        assert {TOOL_TO_OP[n] for n in names - SYNTHETIC} == _MCP_CODING_OPS | _MCP_CODE_CORE_OPS
    # what the census found no coding client calling is not listed …
    core_only = _names(_listing("core", True)) - _names(_listing("coding", True))
    assert {
        "loom_memory_apply",
        "loom_memory_lock",
        "loom_memory_pin",
        "loom_memory_history",
        "loom_blacklist_add",
    } <= core_only
    # … and every one of those stays callable by name (the transport reads the
    # registry, not the roster)
    assert all(
        n in mcp_call.TOOLS_BY_NAME and TOOL_TO_OP[n] not in _MCP_HIDDEN_OPS
        for n in core_only
        if n in TOOL_TO_OP
    )


def test_core_and_atomic_listings_are_unchanged_by_the_new_tier():
    assert len(_listing("core", False)) == len(_MCP_CORE_OPS) + 3
    assert len(_listing("core", True)) == len(_MCP_CORE_OPS | _MCP_CODE_CORE_OPS) + 3
    assert len(_listing("atomic", True)) == len(_MCP_ATOMIC_OPS) + 3
    assert not any(n.startswith("loom_code_") for n in _names(_listing("core", False)))


# ---------------------------------------------------------------- selection


def test_toolset_is_selectable_per_deployment_and_per_request():
    assert resolve_toolset(None, "coding") == "coding"
    assert resolve_toolset("coding", "core") == "coding"
    assert resolve_toolset("CODING", "core") == "coding"
    assert resolve_toolset("bogus", "core") == "core", "an unknown request value is the default"
    assert resolve_toolset(None, "bogus") == "core", "an unknown configured value is core"
    assert resolve_toolset("", None) == "core"
    # the per-request override on a `core` deployment is the coding listing
    assert len(_listing("core", True, requested="coding")) == 23
    assert (
        len(_listing("core", True, requested="bogus"))
        == len(_MCP_CORE_OPS | _MCP_CODE_CORE_OPS) + 3
    )
    assert len(_listing("coding", False, requested="atomic")) == len(_MCP_ATOMIC_OPS) + 3


# ---------------------------------------------------------------- descriptions


def test_coding_descriptions_derive_from_the_core_texts():
    # Memory.pin and working_memory.append are not on the coding
    # listing, so they have no coding texts
    # Blast radius has a coding-tier text (the paging detail stays on qa/all)
    assert set(CODING_TOOL_DESCRIPTIONS) == {
        "memory.set",
        "loom_activate",
        "code.impact",
        "code.symbol",
        "code.grep",
    }
    # derived: every replace took effect and the rest of the text is the core text
    assert "native memory" not in CODING_TOOL_DESCRIPTIONS["memory.set"]
    assert "native memory" in TOOL_DESCRIPTIONS["memory.set"]
    assert "Other writes" in CODING_TOOL_DESCRIPTIONS["memory.set"]
    # the removal pick line on memory.delete: listed paths by tool, the rest marked
    expected = {
        "core": ("loom_memory_apply with", "loom_blacklist_add;"),
        "coding": ("memory.apply (via loom_op) with", "blacklist.add (via loom_op);"),
    }
    for toolset, (apply_ref, refuse_ref) in expected.items():
        delete = {t["name"]: t for t in _listing(toolset, False)}["loom_memory_delete"]
        line = next(ln for ln in delete["description"].splitlines() if ln.startswith("WHICH"))
        assert line.startswith("WHICH REMOVAL: one key is this tool;"), line
        assert apply_ref in line and refuse_ref in line, (toolset, line)
        assert "memory.clear (via loom_op)" in line and "erasure.request (via loom_op)" in line
    for must in (
        "Call this proactively",
        "Don't search before saving",
        "one call per fact",
        "Auto-tag",
        "Do NOT save",
        "ttl_seconds",
        "on_expire",
        "ONE namespace",
        "under that same key",
    ):
        assert must in CODING_TOOL_DESCRIPTIONS["memory.set"], must
    listed = {t["name"]: t for t in _listing("coding", False)}
    assert _shown(listed, "coding", "loom_memory_set", CODING_TOOL_DESCRIPTIONS["memory.set"])
    assert "loom_memory_pin" not in listed
    assert _shown(listed, "coding", "loom_memory_update", TOOL_DESCRIPTIONS["memory.update"])
    # the core tier does not see the coding texts
    core = {t["name"]: t for t in _listing("core", True)}
    assert _shown(core, "core", "loom_memory_set", TOOL_DESCRIPTIONS["memory.set"])
    assert _shown(core, "core", "loom_memory_pin", TOOL_DESCRIPTIONS["memory.pin"])


def test_activation_triggers_are_one_list_on_both_tiers():
    core_text = next(d for d in synthetic_descriptors("core") if d["name"] == "loom_activate")
    coding_text = next(d for d in synthetic_descriptors("coding") if d["name"] == "loom_activate")
    assert coding_text["description"] == CODING_TOOL_DESCRIPTIONS["loom_activate"]
    for trigger in ACTIVATION_TRIGGERS:
        assert f"'{trigger}'" in core_text["description"], trigger
        assert f"'{trigger}'" in coding_text["description"], trigger
    assert "Do **NOT** call `tools/list`" in coding_text["description"]
    assert len(coding_text["description"]) < len(core_text["description"])
    # the search tool's coding text derives from the core text: the two store
    # parentheticals go, every call / paging / do-not-call sentence stays
    from server.transport.mcp_descriptions import coding_search_description

    core_s = next(d for d in synthetic_descriptors("core") if d["name"] == "loom_search")
    coding_s = next(d for d in synthetic_descriptors("coding") if d["name"] == "loom_search")
    assert coding_s["description"] == coding_search_description(core_s["description"])
    assert (
        "profile, knowledge, past conversations and readable issues and pull requests"
        in coding_s["description"]
    )
    assert "(uploaded documents)" in core_s["description"]
    for must in (
        "One call per question",
        "Paging",
        "next_page",
        "Do NOT call for general knowledge",
    ):
        assert must in coding_s["description"], must
    assert coding_s["inputSchema"] == core_s["inputSchema"]
    # the inert-default rule reaches the synthetic schemas on every tier
    props = core_s["inputSchema"]["properties"]
    assert "default" not in props["recipe"] and "default" not in props["page"]
    assert "default" not in props.get("explain", {})
    assert props["top_k"]["default"] == 5


# ---------------------------------------------------------------- budget


def test_coding_descriptions_stay_under_budget():
    """Each coding description stays under the per-tool budget; the listing's
    total ceiling is in test_mcp_listing_budget.py."""
    for t in _listing("coding", False):
        assert len(t["description"]) // 4 <= 480, f"{t['name']} description is over budget"
    # a derived text is never longer than the core text it is built from
    for op, text in CODING_TOOL_DESCRIPTIONS.items():
        if op in TOOL_DESCRIPTIONS:
            assert len(text) <= len(TOOL_DESCRIPTIONS[op]), op


# ---------------------------------------------------------------- catalogue


def test_catalogue_carries_the_toolset_membership():
    assert op_toolsets("memory.set") == ["all", "atomic", "coding", "core"]
    assert op_toolsets("memory.update") == ["all", "coding", "core"]
    assert op_toolsets("knowledge.add") == ["all"]
    assert op_toolsets("code.search") == ["all", "assistant", "coding", "core"]
    assert op_toolsets("run.begin") == ["all", "atomic"]
    assert op_toolsets("facts.traverse") == ["all", "assistant"]
    assert op_toolsets("memory.bulk_set") == [], "a hidden op is on no toolset"
    assert op_toolsets("memory.search") == ["assistant"], "but the assistant roster"
    for op in ALL_OPERATIONS:
        f = catalogue_facets(op)
        assert f["toolsets"] == op_toolsets(op.name)
    # ops.capabilities: `toolsets` per op, additive beside mcp_tier
    snap = capability_snapshot()["ops"]
    assert snap["memory.pin"]["toolsets"] == ["all", "core"]
    assert snap["memory.pin"]["mcp_tier"] == "core"
    assert snap["memory.search"]["toolsets"] == ["assistant"]
    from server.code.api.models_status import CodeIndexStatusOutput
    from server.code.app.index_status import code_op_catalogue
    from server.insights.api.models_ops import OpCapability

    cat = code_op_catalogue()
    assert set(cat.toolsets) == set(cat.ops)
    assert cat.toolsets["code.search"] == ["all", "assistant", "coding", "core"]
    assert cat.toolsets["code.reindex"] == []
    assert CodeIndexStatusOutput(repo="r", branch="b", namespace="n").op_toolsets == {}
    assert (
        OpCapability(
            name="x",
            path="/v1/x",
            mcp_tool="loom_x",
            mcp_tier="core",
            required_role="VIEWER",
            budget_p99_ms=1,
            idempotent=True,
            audit=False,
        ).toolsets
        == []
    )


# ---------------------------------------------------------------- unions


def test_a_plus_union_of_curated_rosters_is_one_listing():
    from server.operations.toolsets import (
        _MCP_TRACKER_OPS,
        canonical_toolset,
        curated_roster,
        is_curated,
        is_toolset,
        toolset_parts,
    )

    assert toolset_parts("Coding+TRACKER+coding") == ("coding", "tracker")
    # a query string hands `+` over as a space, and people type commas
    assert toolset_parts("coding tracker") == ("coding", "tracker")
    assert canonical_toolset("coding,tracker") == "coding+tracker"
    assert is_toolset("coding+tracker") and is_toolset("tasks+coding+tracker")
    assert not is_toolset("qa") and not is_toolset("coding+qa")
    # only curated rosters join: a whole surface or an unknown part makes the value unknown
    assert not is_toolset("coding+all") and not is_toolset("coding+readonly")
    assert not is_toolset("coding+bogus") and not is_toolset("+") and not is_toolset("")
    assert is_curated("coding+tracker") and not is_curated("all") and not is_curated("coding+all")
    assert resolve_toolset("coding+tracker", "core") == "coding+tracker"
    assert resolve_toolset("Coding+TRACKER+coding", "core") == "coding+tracker"
    assert resolve_toolset("coding+qa", "core") == "core"
    assert resolve_toolset("coding+bogus", "core") == "core", "an unknown part is the default"
    assert resolve_toolset(None, "coding+tracker") == "coding+tracker", (
        "a deployment may default to a union"
    )
    # the roster is the union of the parts' rosters
    union = curated_roster("coding+tracker", code_tools=False)
    assert union == (_MCP_CODING_OPS | _MCP_CODE_CORE_OPS | _MCP_TRACKER_OPS)
    assert curated_roster("coding+all", code_tools=False) is None
    tools = _listing("core", True, requested="coding+tracker")
    names = _names(tools)
    assert len(tools) == len(union) + len(SYNTHETIC)
    assert {
        "loom_tracker_answer",
        "loom_tracker_flaky",
        "loom_memory_set",
        "loom_code_symbol",
    } <= names
    # the first part drives tier texts: coding's memory.set and activation texts
    listed = {t["name"]: t for t in tools}
    union_ts = "coding+tracker"
    assert _shown(listed, union_ts, "loom_memory_set", CODING_TOOL_DESCRIPTIONS["memory.set"])
    assert _shown(listed, union_ts, "loom_code_impact", CODING_TOOL_DESCRIPTIONS["code.impact"])
    # a `qa`-first union still finds the coding texts (qa has no tier of its own)
    tracker_first = {t["name"]: t for t in _listing("core", True, requested="tracker+coding")}
    assert _shown(
        tracker_first, "tracker+coding", "loom_memory_set", CODING_TOOL_DESCRIPTIONS["memory.set"]
    )


@pytest.mark.parametrize("profile", ["standard", "compact", "full"])
def test_coding_profiles_load_source_tools_upfront(monkeypatch, profile) -> None:
    default = Settings.model_fields["mcp_always_load_tools"].get_default()
    monkeypatch.setattr(settings, "mcp_always_load_tools", default)
    tools = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset="coding"))
    assert len(tools) == 23
    flagged = {tool["name"] for tool in tools if tool.get("_meta", {}).get("anthropic/alwaysLoad")}
    expected = (
        {"loom_code_edit_context", "loom_code_grep"}
        if profile == "compact"
        else {"loom_code_symbol", "loom_code_grep", "loom_code_search"}
    )
    assert flagged == expected
    assert {"loom_op", "loom_search", "loom_code_search"} <= {tool["name"] for tool in tools}
    edit = next(tool for tool in tools if tool["name"] == "loom_code_edit_context")
    schema = edit["inputSchema"]
    assert "name" not in schema.get("required", [])
    assert {"name", "names", "clues"} <= schema["properties"].keys()
    assert schema["properties"]["include_related_source"]["default"] is True
    assert "Batch exact `names` or declaration `clues`" in edit["description"]
    assert edit["annotations"]["readOnlyHint"]
    grep = next(tool for tool in tools if tool["name"] == "loom_code_grep")
    assert "Keep default bodies for edits" in grep["description"]
    assert {"include_body", "context_lines"} <= grep["inputSchema"]["properties"].keys()
    if profile != "compact":
        assert "loom_code_edit_context for a known definition" in grep["description"]


@contextmanager
def _bound() -> Iterator[None]:
    """A connection whose header names a repository."""
    scope = set_code_scope("acme/code-repo")
    try:
        yield
    finally:
        reset_code_scope(scope)


def _bound_listing(toolset: str, profile: str) -> list[dict]:
    with _bound():
        return asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset=toolset))


def test_a_bound_compact_coding_connection_lists_the_code_tools_alone(monkeypatch) -> None:
    default = Settings.model_fields["mcp_always_load_tools"].get_default()
    monkeypatch.setattr(settings, "mcp_always_load_tools", default)
    tools = {tool["name"]: tool for tool in _bound_listing("coding", "compact")}
    coding = [op for op in mt.listed_ops("coding") if op.name.startswith("code.")]
    assert list(tools) == [op.mcp_tool_name for op in coding]
    assert {"loom_code_read", "loom_code_search", "loom_code_callers"} <= tools.keys()
    flagged = {name for name, tool in tools.items() if tool.get("_meta")}
    assert flagged == {"loom_code_edit_context", "loom_code_grep"}
    grep = tools["loom_code_grep"]["inputSchema"]
    unbound = asyncio.run(mt._tools_descriptor(schema_profile="compact", toolset="coding"))
    compact = next(t for t in unbound if t["name"] == "loom_code_grep")["inputSchema"]
    # the binding fills `namespace`, only a presence check across repositories takes
    # `namespaces`, and `path_globs` expresses the two path filters
    left_out = {"namespace", "namespaces", "path_prefix", "langs"}
    assert set(grep["properties"]) == set(compact["properties"]) - left_out
    assert left_out <= set(compact["properties"])
    # a reply's `next_cursor` and the description's `mode=files` name listed arguments
    assert {"cursor", "mode", "path_globs", "exclude_globs", "case_sensitive"} <= set(
        grep["properties"]
    )
    assert "mode=files" in tools["loom_code_grep"]["description"]
    assert grep["required"] == ["pattern"]
    assert grep["properties"]["context_lines"]["type"] == "integer"
    # the call still takes every argument the listing leaves out
    assert left_out <= OPERATIONS_BY_NAME["code.grep"].input_model.model_fields.keys()
    assert not mt.code_only_listing("coding", "compact")
    # an explicit list marks the tools this listing carries and adds none
    monkeypatch.setattr(settings, "mcp_always_load_tools", "loom_op,memory.set,code.callers")
    explicit = _bound_listing("coding", "compact")
    assert [tool["name"] for tool in explicit] == list(tools)
    assert [tool["name"] for tool in explicit if tool.get("_meta")] == ["loom_code_callers"]


def test_a_reply_on_the_listing_of_code_tools_alone_names_no_tool_it_leaves_out() -> None:
    text = "Save with memory.set, inspect with memory.related, read callers with code.callers."
    listed = "Save with loom_memory_set, inspect with memory.related (via loom_op)"
    with _bound():
        assert mt.listed_tool_names("coding", "compact") == {
            tool["name"] for tool in _bound_listing("coding", "compact")
        }
        with mt.hint_naming("coding", "compact") as name:
            assert name(text) == (
                "Save with memory.set, inspect with memory.related, "
                "read callers with loom_code_callers."
            )
        for toolset, profile in (("coding", "standard"), ("coding+tracker", "compact")):
            assert mt.mentor_namer(toolset, profile)(text).startswith(listed)
        # what a hidden op's refusal offers instead follows the same listing
        hidden = mcp_op.hidden_refusal("memory.bulk_set", "coding", "compact")
        assert hidden and hidden["instead"].startswith("call memory.set once per fact")
        hidden = mcp_op.hidden_refusal("memory.bulk_set", "coding", "standard")
        assert hidden and hidden["instead"].startswith("call loom_memory_set once per fact")
        # and so does the refusal `loom_op` answers for that op
        call = {
            "method": "tools/call",
            "params": {"name": "loom_op", "arguments": {"op": "memory.bulk_set"}},
        }
        for profile, named in (("compact", "memory.set"), ("standard", "loom_memory_set")):
            _, reply = mcp_op.route(call, toolset="coding", auth=None, schema_profile=profile)
            assert reply and reply["result"]["structuredContent"]["instead"].startswith(
                f"call {named} once per fact"
            )
    assert mt.mentor_namer("coding", "compact")(text).startswith(listed)


def test_a_compact_deployment_default_keeps_every_tool_on_a_bound_connection(monkeypatch) -> None:
    """The URL names no profile, so the client did not ask for the code tools alone."""
    monkeypatch.setattr(settings, "mcp_schema_profile", "compact")
    unbound = asyncio.run(mt._tools_descriptor(toolset="coding"))
    names = {tool["name"] for tool in unbound}
    assert {"loom_op", "loom_search", "loom_memory_set", "loom_context_pack"} <= names
    text = "Save with memory.set."
    with _bound():
        assert asyncio.run(mt._tools_descriptor(toolset="coding")) == unbound
        assert not mt.code_only_listing("coding", None)
        assert not mt.code_only_listing("coding", "bogus")  # an unknown value is the default
        assert mt.code_only_listing("coding", "compact")
        assert mt.listed_tool_names("coding") == names
        assert mt.name_ops_for_toolset(text, "coding") == "Save with loom_memory_set."
        assert mt.name_ops_for_toolset(text, "coding", "compact") == text
        # the toolset may be the deployment's default: the URL then names the profile alone
        monkeypatch.setattr(settings, "mcp_toolset", "core")
        assert not mt.code_only_listing(None, "COMPACT")
        monkeypatch.setattr(settings, "mcp_toolset", "coding")
        assert mt.code_only_listing(None, "COMPACT")
        assert mt.listed_tool_names(None, "compact") == mt.listed_tool_names("coding", "compact")
        assert asyncio.run(mt._tools_descriptor()) == unbound
    grep = next(t for t in unbound if t["name"] == "loom_code_grep")["inputSchema"]
    assert {"namespace", "path_prefix"} <= grep["properties"].keys()


def test_a_tool_the_listing_of_code_tools_alone_leaves_out_stays_callable(monkeypatch) -> None:
    """`loom_op`, a memory write and grep's unlisted arguments run on a `coding`
    connection bound to a repository whose URL asks for the compact profile."""
    seen: dict[str, Any] = {}
    hint = "Save with memory.set, read callers with code.callers."

    def stub(tool: str) -> None:
        op = mcp_call.TOOLS_BY_NAME[tool]

        async def handler(inp: Any) -> Any:
            seen[op.name] = inp
            seen["hint"] = named_for_caller(hint)
            return op.output_model.model_construct()

        monkeypatch.setitem(mcp_call.TOOLS_BY_NAME, tool, dataclasses.replace(op, handler=handler))

    async def defaults(auth: Any, client_name: str | None = None) -> dict[str, str]:
        return {"org": "acme", "namespace": "home", "agent": "claude-code"}

    async def admitted(req_id: Any, op: Any, auth: Any) -> None:
        return None

    async def no_repo_tag(org: str, namespace: str) -> str:
        return ""

    monkeypatch.setattr(settings, "identity_provider", "single-user")
    monkeypatch.setattr(mcp_call, "_caller_defaults", defaults)
    monkeypatch.setattr(mcp_call, "_session_content_refusal", admitted)
    monkeypatch.setattr(mcp_call, "session_repo_tag", no_repo_tag)
    stub("loom_code_grep")
    stub("loom_memory_set")
    app = FastAPI()
    mcp.attach(app)
    client = TestClient(app)

    def post(method: str, params: dict[str, Any]) -> dict[str, Any]:
        reply = client.post(
            "/mcp?toolset=coding&schema_profile=compact",
            headers={"X-Loom-Code-Scope": "acme/code-repo"},
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        )
        result: dict[str, Any] = reply.json()["result"]
        return result

    def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        result = post("tools/call", {"name": name, "arguments": arguments})
        assert not result.get("isError"), result
        return result

    listed = {tool["name"] for tool in post("tools/list", {})["tools"]}
    assert "loom_code_grep" in listed
    assert not listed & {"loom_op", "loom_memory_set", "loom_activate"}
    call(
        "loom_code_grep", {"pattern": "needle", "mode": "files", "cursor": "next", "langs": ["py"]}
    )
    grep = seen.pop("code.grep")
    assert (grep.namespace, grep.mode, grep.cursor, grep.langs) == (
        "code-repo",
        "files",
        "next",
        ["py"],
    )
    # a mentor hint the handler renders names only what this listing carries
    assert seen.pop("hint") == "Save with memory.set, read callers with loom_code_callers."
    fact = {"memory_key": "repo/decision/listing", "value": "kept"}
    call("loom_memory_set", fact)
    assert seen.pop("memory.set").memory_key == fact["memory_key"]
    call("loom_op", {"op": "memory.set", "arguments": fact})
    assert seen.pop("memory.set").namespace == "home"
    help_rows = call("loom_op", {"op": "help", "query": "memory.set"})["structuredContent"]
    assert help_rows["ops"][0]["op"] == "memory.set"
    # the activation primer is this connection's routing: it names no tool left out
    primer = call("loom_activate", {})["structuredContent"]["primer"]
    assert "`loom_code_grep`" in primer
    for absent in ("loom_op", "loom_search", "loom_memory", "loom_context_pack"):
        assert absent not in primer, absent
    # the `loom` prompt and `loom://primer`, asked for by name, carry the same text
    prompt = post("prompts/get", {"name": "loom"})["messages"][0]["content"]["text"]
    resource = post("resources/read", {"uri": "loom://primer"})["contents"][0]["text"]
    assert prompt == resource == primer
    # a hidden op, called by its tool name or through `loom_op`, is refused in this
    # listing's names
    for name, arguments in (("loom_memory_bulk_set", {}), ("loom_op", {"op": "memory.bulk_set"})):
        refused = post("tools/call", {"name": name, "arguments": arguments})
        assert refused["isError"]
        assert refused["structuredContent"]["instead"].startswith("call memory.set once per fact")


@pytest.mark.parametrize(
    ("toolset", "profile"),
    [
        ("coding", "standard"),
        ("coding", "full"),
        ("coding+tracker", "compact"),
        ("core", "compact"),
    ],
)
def test_a_bound_connection_on_another_listing_keeps_its_tools(toolset, profile) -> None:
    unbound = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset=toolset))
    assert _bound_listing(toolset, profile) == unbound
    assert {"loom_op", "loom_search"} <= {tool["name"] for tool in unbound}


def test_edit_context_says_when_to_use_it_without_ordering() -> None:
    for profile in ("standard", "compact"):
        tools = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset="coding"))
        text = next(t["description"] for t in tools if t["name"] == "loom_code_edit_context")
        assert text == (
            "Before editing, read source, tests and notes. "
            "Batch exact `names` or declaration `clues`."
        )
        assert "first" not in text.lower()


@pytest.mark.parametrize("toolset", ["core", "coding+tracker"])
def test_other_default_toolsets_keep_the_generic_dispatcher_upfront(monkeypatch, toolset) -> None:
    monkeypatch.setattr(settings, "mcp_always_load_tools", "auto")
    tools = asyncio.run(mt._tools_descriptor(toolset=toolset))
    dispatcher = next(tool for tool in tools if tool["name"] == "loom_op")
    assert dispatcher["_meta"]["anthropic/alwaysLoad"] is True


@pytest.mark.parametrize("profile", ["standard", "compact", "full"])
def test_eager_opt_in_changes_only_loading_metadata(monkeypatch, profile) -> None:
    monkeypatch.setattr(settings, "mcp_always_load_tools", "")
    deferred = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset="coding"))
    monkeypatch.setattr(settings, "mcp_always_load_tools", "code.search,code.symbol")
    eager = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset="coding"))
    flagged = {tool["name"] for tool in eager if tool.get("_meta", {}).get("anthropic/alwaysLoad")}
    assert flagged == {"loom_code_search", "loom_code_symbol"}
    unmarked = []
    for tool in eager:
        descriptor = {**tool}
        meta = {**descriptor.get("_meta", {})}
        meta.pop("anthropic/alwaysLoad", None)
        if meta:
            descriptor["_meta"] = meta
        else:
            descriptor.pop("_meta", None)
        unmarked.append(descriptor)
    assert unmarked == deferred


def test_mcp_always_load_tools_names_the_flagged_ops(monkeypatch) -> None:
    import asyncio

    from server.settings import settings
    from server.transport.mcp_tools import _tools_descriptor

    monkeypatch.setattr(settings, "mcp_always_load_tools", "code.callers")
    tools = asyncio.run(_tools_descriptor(toolset="coding"))
    flagged = {t["name"] for t in tools if (t.get("_meta") or {}).get("anthropic/alwaysLoad")}
    assert flagged == {"loom_code_callers"}
    monkeypatch.setattr(settings, "mcp_always_load_tools", "")
    tools = asyncio.run(_tools_descriptor(toolset="coding"))
    assert not any(t.get("_meta") for t in tools)


@pytest.mark.parametrize("profile", ["standard", "compact", "full"])
def test_synthetic_operations_and_tools_honor_explicit_loading_metadata(
    monkeypatch, profile
) -> None:
    monkeypatch.setattr(settings, "mcp_always_load_tools", "search.query,loom_op,loom_activate")
    tools = asyncio.run(mt._tools_descriptor(schema_profile=profile, toolset="coding"))
    flagged = {tool["name"] for tool in tools if tool.get("_meta", {}).get("anthropic/alwaysLoad")}
    assert flagged == {"loom_search", "loom_op", "loom_activate"}


@pytest.mark.parametrize(
    "toolset", ["core", "coding", "tasks", "tracker", "atomic", "readonly", "assistant"]
)
def test_loading_metadata_never_adds_a_tool_or_widens_its_roster(monkeypatch, toolset) -> None:
    monkeypatch.setattr(settings, "mcp_always_load_tools", "")
    deferred = asyncio.run(mt._tools_descriptor(toolset=toolset))
    monkeypatch.setattr(
        settings, "mcp_always_load_tools", "search.query,code.search,loom_op,memory.delete"
    )
    upfront = asyncio.run(mt._tools_descriptor(toolset=toolset))
    assert {t["name"] for t in upfront} == {t["name"] for t in deferred}
    assert [{k: v for k, v in t.items() if k != "_meta"} for t in upfront] == deferred
    if toolset == "assistant":
        assert "loom_op" not in {t["name"] for t in upfront}


@pytest.mark.parametrize("toolset,writes", [("coding", True), ("readonly", False)])
def test_operation_dispatcher_annotations_cover_its_possible_effects(toolset, writes) -> None:
    tools = asyncio.run(mt._tools_descriptor(toolset=toolset))
    op = next(t for t in tools if t["name"] == "loom_op")
    assert op["annotations"]["readOnlyHint"] is not writes
    assert op["annotations"]["destructiveHint"] is writes


def test_the_primer_names_collection_list_by_the_tool_that_reaches_it() -> None:
    refs = mcp_primer._tool_refs("core")
    assert refs["collection.list"] == "`collection.list`"
    rules = mcp_primer._write_rules(refs)
    assert "shared collection (`col-…`)" in rules
    # the rendered primer names it as core's listing does, outside any parenthesis
    assert "`collection.list` (via loom_op) names those collections." in mcp_primer._loom_rules(
        "core", None
    )


@pytest.mark.parametrize("toolset", ["core", "coding", "tasks", "tracker", "atomic", "readonly"])
def test_the_primer_marks_each_unlisted_op_once_and_outside_parentheses(toolset: str) -> None:
    for text in (mcp_primer._loom_rules(toolset, None), mcp_primer._coding_instructions(toolset)):
        assert_marked_once_outside_parentheses(text, toolset)


def test_the_primers_say_loom_search_reads_collections() -> None:
    full = mcp_primer._loom_rules("core", None)
    assert "and the collections you can read" in full
    coding = mcp_primer._coding_instructions("coding", {"namespace": "project"})
    assert "also reads the collections you can read" in coding


def test_the_primers_drop_the_collections_clause_when_search_does_not_span_them(
    monkeypatch,
) -> None:
    monkeypatch.setattr(settings, "knowledge_search_fanout_kinds", "memory")
    full = mcp_primer._loom_rules("core", None)
    assert "and the collections you can read" not in full
    coding = mcp_primer._coding_instructions("coding")
    assert "collections you can read" not in coding
    # "" spans every kind.
    monkeypatch.setattr(settings, "knowledge_search_fanout_kinds", "")
    full = mcp_primer._loom_rules("core", None)
    assert "and the collections you can read" in full


def test_memory_scoped_primers_set_the_task_pack_order() -> None:
    """The primer owns session order, and both discovery surfaces describe the task pack."""
    from scripts.codegen.page_summary import first_sentence  # the docs generator, kept lazy

    coding = mcp_primer._coding_instructions("coding", {"namespace": "project"})
    assert "First step of each task: call `loom_context_pack` with the task as `query`" in coding
    assert f"Skip it when {FIRST_STEP_HOOK}." in coding
    rules = mcp_primer._loom_rules("core", None)
    assert (
        "**1. Task context.** With a default memory namespace, call `loom_context_pack` first with the task as `query`"
        in rules
    )
    assert f"Skip it when {FIRST_STEP_HOOK}." in rules
    assert "For one fact, rule 2's search alone is enough." in rules
    summary = first_sentence(TOOL_DESCRIPTIONS["context.pack"])
    assert summary == "Retrieve task context by passing the task as `query`."
    assert mcp_op._purpose(OPERATIONS_BY_NAME["context.pack"]) == summary


def test_coding_connections_teach_code_routing_without_plugin_hooks() -> None:
    coding = mcp_primer._coding_instructions("coding")
    assert "Use Loom first to search and read unchanged indexed source, including tests" in coding
    assert "use `loom_code_edit_context` with a definition or literal clues" in coding
    assert "Inspect a definition with `loom_code_symbol`" in coding
    for name, purpose in (
        ("loom_code_search", "for behavior or an issue"),
        ("loom_code_grep", "for exact text or an error"),
        ("loom_code_read", "to read known line ranges together"),
        ("loom_code_callers", "for callers"),
        ("loom_code_impact", "for change effects"),
        ("loom_code_context_pack", "for a task briefing"),
    ):
        assert f"`{name}` {purpose}" in coding
    assert "In Claude Code, find deferred tools with ToolSearch" in coding
    assert "linked lessons" in coding
    assert "`code_state`" in coding
    assert "Batch related matches or ranges" in coding
    assert "Use local tools to edit, run tests, and inspect Git" in coding
    assert "Read locally for working-copy changes, unindexed paths, explicit requests" in coding
    assert "evidence Loom cannot supply" in coding
    assert "Reuse source and linked lessons already in context" in coding


@pytest.mark.parametrize("profile", ["standard", "compact"])
async def test_coding_schemas_keep_the_inputs_needed_to_select_and_call_code_tools(profile) -> None:
    from server.transport.mcp_tools import _tools_descriptor

    descriptors = {
        row["name"]: row
        for row in await _tools_descriptor(toolset="coding", schema_profile=profile)
    }
    for name, fields in (
        ("loom_code_search", ("query", "bodies")),
        ("loom_code_symbol", ("name", "include_body", "uses_brief")),
        ("loom_code_grep", ("pattern",)),
    ):
        properties = descriptors[name]["inputSchema"]["properties"]
        assert all(properties[field].get("description") for field in fields)

    for name in ("loom_code_search", "loom_code_grep"):
        cursor = descriptors[name]["inputSchema"]["properties"]["cursor"]["description"]
        assert "`next_cursor` with the original search arguments" in cursor
        assert "omit it to start a new search" in cursor
    regex = descriptors["loom_code_grep"]["inputSchema"]["properties"]["regex"]["description"]
    assert "without lookaround or backreferences" in regex


@pytest.mark.parametrize("defaults", [None, {"namespace": ""}])
def test_coding_connections_without_a_memory_home_do_not_request_a_memory_pack(defaults) -> None:
    coding = mcp_primer._coding_instructions("coding", defaults)
    assert "loom_code_context_pack" in coding
    assert "First step of each task" not in coding
    assert "Recall before answering from memory" in coding


def test_coding_tool_listing_describes_task_pack_inputs_and_results() -> None:
    tools = asyncio.run(mt._tools_descriptor(toolset="coding"))
    description = next(t["description"] for t in tools if t["name"] == "loom_context_pack")
    assert description.startswith("Retrieve task context by passing the task as `query`.")
    assert "memories, decisions, procedures, working memory and the profile" in description
    assert "`include_runs=true` lists recent sessions and runs" in description


@pytest.mark.parametrize("toolset", ["core", "coding", "tasks", "tracker", "atomic", "readonly"])
def test_shared_routing_covers_capabilities_and_safe_operation_discovery(toolset) -> None:
    for text in (mcp_primer._loom_rules(toolset, None), mcp_primer._coding_instructions(toolset)):
        for purpose in ("saved knowledge", "documents", "code", "issues", "tasks", "records"):
            assert purpose in text
        assert '`op="help"`' in text
        assert "never invent an unlisted tool name" in text
        assert "make only calls needed for the task" in text
        assert "Writes need user intent and permission" in text


def test_assistant_routing_stays_with_listed_read_tools() -> None:
    text = mcp_primer._assistant_primer(None)
    assert "relevant listed Loom read tools" in text
    assert "cannot write or route unlisted operations" in text
    assert "loom_op" not in text
