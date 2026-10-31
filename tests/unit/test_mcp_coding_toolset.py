"""The `coding` MCP toolset: one listing per archetype instead
of one `core` for two. Membership derives from the frozensets; the toolset is
selectable per deployment and per request; the code tools ride it without the
deployment flag; the catalogue says which toolsets list an op; the coding-tier
descriptions are the core texts with the unlisted op marked callable by name;
the listing stays under its measured ceiling.

Hermetic: `_caller_default_context` (which can hit ClickHouse) is patched."""

from __future__ import annotations

import asyncio
import json

import pytest

import server.transport.mcp_call as mcp_call
from server.operations.capabilities import capability_snapshot
from server.operations.reach import catalogue_facets
from server.operations.registry import ALL_OPERATIONS
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
)
from server.settings import settings
from server.transport import mcp_primer
from server.transport import mcp_tools as mt
from server.transport.mcp_descriptions import (
    ACTIVATION_TRIGGERS,
    CODING_TOOL_DESCRIPTIONS,
    TOOL_DESCRIPTIONS,
)

OPS = {op.name: op for op in ALL_OPERATIONS}
TOOL_TO_OP = {op.mcp_tool_name: op.name for op in ALL_OPERATIONS}
SYNTHETIC = {"loom_search", "loom_activate", "loom_op"}
# The gate is the saving: the tokens the coding listing takes off the core +
# code listing, both measured on the same tree. Every coding tool is a core or
# code tool, so a description or field grown on a shared tool moves both sides
# by the same amount and leaves the saving where it was (a ratio would drift
# with shared growth). The floor sits a little under the current saving.
CODING_SAVING_FLOOR = 4_600
# The prose the tier authors: a derived text may exceed the core text it is
# built from only by the "(callable by name)" mark (≤ 8 tokens).
CALLABLE_BY_NAME_MARK_TOKENS = 8


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


def _tok(obj) -> int:
    return len(json.dumps(obj, separators=(",", ":"))) // 4


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
    }
    # The QA-plane ops a coding session asks for are
    # listed on coding without joining core
    tracker_on_coding = {
        "tracker.duplicate_check",
        "code.bisect_candidates",
        "code.pr_check",
        "code.impact",
    }
    assert _MCP_CODING_OPS - tracker_on_coding <= _MCP_CORE_OPS, "coding ⊆ core + the QA-plane four"
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


def test_coding_listing_is_twenty_tools_with_the_code_tools_regardless_of_the_flag():
    for code in (False, True):
        tools = _listing("coding", code)
        names = _names(tools)
        assert len(tools) == len(_MCP_CODING_OPS | _MCP_CODE_CORE_OPS) + len(SYNTHETIC) == 21
        assert SYNTHETIC <= names
        assert {TOOL_TO_OP[n] for n in names - SYNTHETIC} == _MCP_CODING_OPS | _MCP_CODE_CORE_OPS
    # what the census found no coding client calling is not listed …
    core_only = _names(_listing("core", True)) - _names(_listing("coding", True))
    assert {
        "loom_knowledge_add",
        "loom_conversation_create",
        "loom_profile_set",
        "loom_erasure_request",
        "loom_namespace_list_mine",
        "loom_memory_apply",
        "loom_memory_lock",
        "loom_memory_set_from_messages",
        "loom_working_memory_replace",
    } <= core_only
    # … and every one of those stays callable by name (the transport reads the
    # registry, not the roster)
    assert all(
        n in mcp_call.TOOLS_BY_NAME and TOOL_TO_OP[n] not in _MCP_HIDDEN_OPS
        for n in core_only
        if n in TOOL_TO_OP
    )
    # The unlisted verbs are core-only — listed under
    # core, absent from coding, still callable by name
    for dropped in ("loom_memory_pin", "loom_blacklist_add", "loom_working_memory_append"):
        assert dropped not in names and dropped in core_only and dropped in mcp_call.TOOLS_BY_NAME


def test_core_and_atomic_listings_are_unchanged_by_the_new_tier():
    assert len(_listing("core", False)) == len(_MCP_CORE_OPS) + 3
    assert len(_listing("core", True)) == len(_MCP_CORE_OPS | _MCP_CODE_CORE_OPS) + 3
    assert len(_listing("atomic", True)) == len(_MCP_ATOMIC_OPS) + 3
    assert not any(n.startswith("loom_code_") for n in _names(_listing("core", False)))


# ---------------------------------------------------------------- selection


def test_toolset_is_selectable_per_deployment_and_per_request():
    assert mt.resolve_toolset(None, "coding") == "coding"
    assert mt.resolve_toolset("coding", "core") == "coding"
    assert mt.resolve_toolset("CODING", "core") == "coding"
    assert mt.resolve_toolset("bogus", "core") == "core", "an unknown request value is the default"
    assert mt.resolve_toolset(None, "bogus") == "core", "an unknown configured value is core"
    assert mt.resolve_toolset("", None) == "core"
    # the per-request override on a `core` deployment is the coding listing
    assert len(_listing("core", True, requested="coding")) == 21
    assert (
        len(_listing("core", True, requested="bogus"))
        == len(_MCP_CORE_OPS | _MCP_CODE_CORE_OPS) + 3
    )
    assert len(_listing("coding", False, requested="atomic")) == len(_MCP_ATOMIC_OPS) + 3


# ---------------------------------------------------------------- descriptions


def test_coding_descriptions_derive_from_the_core_texts_and_mark_unlisted_ops():
    # Memory.pin and working_memory.append are not on the coding
    # listing, so they have no coding texts
    # Blast radius has a coding-tier text (the paging detail stays on qa/all)
    assert set(CODING_TOOL_DESCRIPTIONS) == {"memory.set", "loom_activate", "code.impact"}
    # derived: every replace took effect and the rest of the text is the core text
    assert "native memory" not in CODING_TOOL_DESCRIPTIONS["memory.set"]
    assert "native memory" in TOOL_DESCRIPTIONS["memory.set"]
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
    assert listed["loom_memory_set"]["description"] == CODING_TOOL_DESCRIPTIONS["memory.set"]
    assert "loom_memory_pin" not in listed
    assert listed["loom_memory_update"]["description"] == TOOL_DESCRIPTIONS["memory.update"]
    # the core tier does not see the coding texts
    core = {t["name"]: t for t in _listing("core", True)}
    assert core["loom_memory_set"]["description"] == TOOL_DESCRIPTIONS["memory.set"]
    assert core["loom_memory_pin"]["description"] == TOOL_DESCRIPTIONS["memory.pin"]


def test_activation_triggers_are_one_list_on_both_tiers():
    core_text = next(
        d for d in mt._synthetic_tool_descriptors("core") if d["name"] == "loom_activate"
    )
    coding_text = next(
        d for d in mt._synthetic_tool_descriptors("coding") if d["name"] == "loom_activate"
    )
    assert coding_text["description"] == CODING_TOOL_DESCRIPTIONS["loom_activate"]
    for trigger in ACTIVATION_TRIGGERS:
        assert f"'{trigger}'" in core_text["description"], trigger
        assert f"'{trigger}'" in coding_text["description"], trigger
    assert "Do **NOT** call `tools/list`" in coding_text["description"]
    assert len(coding_text["description"]) < len(core_text["description"])
    # the search tool's coding text derives from the core text: the two store
    # parentheticals go, every call / paging / do-not-call sentence stays
    from server.transport.mcp_descriptions import coding_search_description

    core_s = next(d for d in mt._synthetic_tool_descriptors("core") if d["name"] == "loom_search")
    coding_s = next(
        d for d in mt._synthetic_tool_descriptors("coding") if d["name"] == "loom_search"
    )
    assert coding_s["description"] == coding_search_description(core_s["description"])
    assert "profile, knowledge and past conversations" in coding_s["description"]
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


def test_coding_listing_stays_under_its_ceiling():
    """The gate: core + code − coding ≥ the floor, both listings measured on
    this tree (chars/4). What the tier owns is its membership (pinned above)
    and four derived texts, so only those can spend the saving; an absolute
    ceiling or a ratio would move with text the tier does not author."""
    coding = _tok({"tools": _listing("coding", False)})
    core_code = _tok({"tools": _listing("core", True)})
    assert core_code - coding >= CODING_SAVING_FLOOR, (coding, core_code, core_code - coding)
    for t in _listing("coding", False):
        assert len(t["description"]) // 4 <= 480, f"{t['name']} description is over budget"
    # the coding-authored prose: never longer than the core text it derives
    # from, beyond the mark that names the unlisted op as callable by name
    for op, text in CODING_TOOL_DESCRIPTIONS.items():
        if op in TOOL_DESCRIPTIONS:
            grown = (len(text) - len(TOOL_DESCRIPTIONS[op])) // 4
            assert grown <= CALLABLE_BY_NAME_MARK_TOKENS, (op, grown)


# ---------------------------------------------------------------- catalogue


def test_catalogue_carries_the_toolset_membership():
    assert op_toolsets("memory.set") == ["all", "atomic", "coding", "core"]
    assert op_toolsets("memory.update") == ["all", "coding", "core"]
    assert op_toolsets("knowledge.add") == ["all", "core"]
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
    assert mt.resolve_toolset("coding+tracker", "core") == "coding+tracker"
    assert mt.resolve_toolset("Coding+TRACKER+coding", "core") == "coding+tracker"
    assert mt.resolve_toolset("coding+qa", "core") == "core"
    assert mt.resolve_toolset("coding+bogus", "core") == "core", "an unknown part is the default"
    assert mt.resolve_toolset(None, "coding+tracker") == "coding+tracker", (
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
    assert listed["loom_memory_set"]["description"] == CODING_TOOL_DESCRIPTIONS["memory.set"]
    assert listed["loom_code_impact"]["description"] == CODING_TOOL_DESCRIPTIONS["code.impact"]
    # a `qa`-first union still finds the coding texts (qa has no tier of its own)
    tracker_first = {t["name"]: t for t in _listing("core", True, requested="tracker+coding")}
    assert tracker_first["loom_memory_set"]["description"] == CODING_TOOL_DESCRIPTIONS["memory.set"]


def test_the_core_code_tools_ask_to_be_loaded_up_front() -> None:
    import asyncio

    from server.transport.mcp_tools import _tools_descriptor

    for profile in ("standard", "compact"):
        tools = asyncio.run(_tools_descriptor(schema_profile=profile, toolset="coding"))
        flagged = {t["name"] for t in tools if (t.get("_meta") or {}).get("anthropic/alwaysLoad")}
        assert flagged == {"loom_code_search", "loom_code_symbol"}, profile


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
