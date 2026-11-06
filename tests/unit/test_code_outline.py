"""code.outline: one file's declarations with line spans, source order,
whole-row budgeted — and code.symbol's auto-outline for large containers
."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

from server.settings import settings

# two-tier rows for one header, deliberately out of source order
ROWS = [
    {
        "symbol_id": "s-ctx",
        "qualified_name": "DB::Context",
        "name": "Context",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 786,
        "end_line": 786,
        "tier": "scip",
        "kind": "class",
    },
    {
        "symbol_id": "t-ctx",
        "qualified_name": "DB::Context",
        "name": "Context",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 786,
        "end_line": 2214,
        "tier": "treesitter",
        "kind": "class",
    },
    {
        "symbol_id": "m1",
        "qualified_name": "DB::Context::getClientInfo",
        "name": "getClientInfo",
        "signature": "(scip:49f6e7a06ebc5aa8)",
        "path": "src/Context.h",
        "start_line": 1054,
        "end_line": 1054,
        "tier": "scip",
        "kind": "method",
    },
    {
        "symbol_id": "m2",
        "qualified_name": "DB::Context::setSetting",
        "name": "setSetting",
        "signature": "void setSetting(std::string_view name, const Field & value)",
        "path": "src/Context.h",
        "start_line": 1400,
        "end_line": 1400,
        "tier": "scip",
        "kind": "method",
    },
    # a `using` alias the compiler records as a one-line class: not a declaration to open
    {
        "symbol_id": "alias",
        "qualified_name": "DB::ContextPtr",
        "name": "ContextPtr",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 60,
        "end_line": 60,
        "tier": "scip",
        "kind": "class",
    },
    {
        "symbol_id": "f1",
        "qualified_name": "DB::freeFunction",
        "name": "freeFunction",
        "signature": "int freeFunction()",
        "path": "src/Context.h",
        "start_line": 300,
        "end_line": 310,
        "tier": "treesitter",
        "kind": "function",
    },
    {
        "symbol_id": "st",
        "qualified_name": "DB::Holder",
        "name": "Holder",
        "signature": "",
        "path": "src/Context.h",
        "start_line": 344,
        "end_line": 358,
        "tier": "treesitter",
        "kind": "struct",
    },
]


def _settings(monkeypatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(settings, "code_org", "o")
    monkeypatch.setattr(settings, "code_namespace", "n")


def test_collapse_outline_rows_is_source_ordered_two_tier_and_drops_extentless_containers():
    from server.code.store.index import collapse_outline_rows

    out = collapse_outline_rows(ROWS)
    got = [(r["qualified_name"], r["start_line"], r["end_line"], r["tier"]) for r in out]
    assert got == [
        ("DB::freeFunction", 300, 310, "treesitter"),
        ("DB::Holder", 344, 358, "treesitter"),
        ("DB::Context", 786, 2214, "scip"),  # parser extent, compiler tier
        ("DB::Context::getClientInfo", 1054, 1054, "scip"),
        ("DB::Context::setSetting", 1400, 1400, "scip"),
    ], "source order; one row per declaration; the alias (one-line class) is gone"


def test_the_wider_of_two_rows_for_one_declaration_wins_and_either_scip_row_marks_it() -> None:
    from server.code.store.index import collapse_outline_rows

    def row(end: int, tier: str, signature: str = "") -> dict[str, Any]:
        return {
            "qualified_name": "DB::f",
            "start_line": 10,
            "end_line": end,
            "kind": "function",
            "tier": tier,
            "signature": signature,
        }

    (got,) = collapse_outline_rows([row(10, "treesitter"), row(40, "scip", "void f()")])
    assert (got["end_line"], got["tier"], got["signature"]) == (40, "scip", "void f()")
    # A narrower scip row keeps the wider span and lends its tier and signature.
    (got,) = collapse_outline_rows([row(40, "treesitter"), row(10, "scip", "void f()")])
    assert (got["end_line"], got["tier"], got["signature"]) == (40, "scip", "void f()")


class _FakeRepo:
    def __init__(self) -> None:
        self.outline_calls: list[tuple[str, tuple[str, ...] | None]] = []
        self.skeleton_calls: list[tuple[str, str]] = []

    async def outline_for_path(self, org, ns, path, *, kinds=None, limit=3000):
        from server.code.store.index import collapse_outline_rows

        self.outline_calls.append((path, kinds))
        rows = [r for r in ROWS if r["path"] == path and (kinds is None or r["kind"] in kinds)]
        return collapse_outline_rows(rows)

    async def class_skeleton_chunk(self, org, ns, path, scope):
        self.skeleton_calls.append((path, scope))
        if scope == "DB::Context":
            # a real skeleton piece is up to 6,000 chars (chunker max_chars)
            return {
                "content": "class Context {\n" + "    void member();\n" * 150 + "};",
                "start_line": 786,
                "end_line": 2214,
                "chunk_id": "c1",
            }
        return None

    async def all_state(self, org, ns):
        return {}

    # code.symbol resolution
    async def symbols_by_name(self, org, ns, name, limit=10):
        from server.code.store.index.symbol_rank import _prefer_scip

        # the real read collapses the compiler row (one-line span) onto the
        # parser extent — the shape handle_code_symbol sees for a class
        return _prefer_scip([r for r in ROWS if r["qualified_name"] == name])

    async def symbols_by_ids(self, org, ns, ids):
        return [r for r in ROWS if r["symbol_id"] in ids]

    async def symbols_fuzzy(self, org, ns, needle, *, limit=25):
        return []

    async def chunk_hashes_for_symbols(self, org, ns, ids, *, rows=None):
        return {}


async def _as_of(*a, **k):
    return {"commit": "x"}


def _outline(**kw) -> tuple[Any, _FakeRepo]:
    from server.code.api.models_navigation import CodeOutlineInput
    from server.code.app.symbol import outline as h

    fake = _FakeRepo()

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "index_as_of", new=_as_of),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_outline(CodeOutlineInput(**kw))

    return asyncio.run(go()), fake


def test_outline_lists_declarations_in_source_order_with_default_kinds(monkeypatch):
    _settings(monkeypatch)
    out, fake = _outline(path="./src/Context.h")
    from server.code.api.models_navigation import OUTLINE_DEFAULT_KINDS

    assert fake.outline_calls == [("src/Context.h", tuple(OUTLINE_DEFAULT_KINDS))], (
        "leading ./ stripped; the default kinds (statement, job / target / stage, "
        "query / changeset included) reach the read"
    )
    assert {"function", "statement", "job", "target", "stage", "query", "changeset"} <= set(
        OUTLINE_DEFAULT_KINDS
    )
    assert out.path == "src/Context.h" and out.indexed is True
    assert [e["qualified_name"] for e in out.entries] == [
        "DB::freeFunction",
        "DB::Holder",
        "DB::Context",
        "DB::Context::getClientInfo",
        "DB::Context::setSetting",
    ]
    assert out.total == out.returned == 5 and out.elided == 0 and out.next == ""
    e = out.entries[3]
    assert set(e) == {"kind", "qualified_name", "signature", "start_line", "end_line", "tier"}
    assert e["signature"] == "", "the compiler's overload-hash placeholder is not a signature"
    assert out.entries[4]["signature"].startswith("void setSetting(")
    assert out.tokens_returned > 0 and out.as_of == {"commit": "x"}


def test_outline_budgets_whole_rows_and_names_the_first_elided_entry(monkeypatch):
    _settings(monkeypatch)
    from server.code.domain import units as code_units

    # every row priced at 60 tokens: a 200-token budget holds three whole rows
    monkeypatch.setattr(code_units, "estimate_json_tokens", lambda e: 60)
    out, _ = _outline(path="src/Context.h", token_budget=200)
    assert [e["qualified_name"] for e in out.entries] == [
        "DB::freeFunction",
        "DB::Holder",
        "DB::Context",
    ], "rows ship whole or not at all — the fourth (40 tokens short) is not sliced"
    assert out.next == "code.symbol name=DB::Context::getClientInfo"
    assert out.returned == 3 and out.elided == 2 and out.total == 5
    assert out.tokens_returned == 180 <= 200


def test_outline_kinds_filter_and_unindexed_path(monkeypatch):
    _settings(monkeypatch)
    out, fake = _outline(path="src/Context.h", kinds=["class", "struct"])
    assert fake.outline_calls[0][1] == ("class", "struct")
    assert [e["qualified_name"] for e in out.entries] == ["DB::Holder", "DB::Context"]
    miss, _ = _outline(path="docs/nope.md")
    assert miss.indexed is False and miss.entries == [] and miss.total == 0 and miss.next == ""


def test_outline_is_registered_and_listed_with_the_code_tools():
    from server.operations.registry import ALL_OPERATIONS
    from server.operations.toolsets import _MCP_CODE_CORE_OPS, _MCP_HIDDEN_OPS
    from server.transport.mcp_descriptions import TOOL_DESCRIPTIONS

    op = next(o for o in ALL_OPERATIONS if o.name == "code.outline")
    assert op.idempotent and op.required_role.name == "VIEWER"
    assert "code.outline" in _MCP_CODE_CORE_OPS and "code.outline" not in _MCP_HIDDEN_OPS, (
        "the shunt's redirect target must be discoverable by agents that are not the plugin"
    )
    assert "code.outline" in TOOL_DESCRIPTIONS


# ---- code.symbol auto-outline for a 200+ line container ---------------------


def _symbol(name: str, budget: int = 2500) -> tuple[Any, _FakeRepo, list[str]]:
    from server.code.api.models_symbol import CodeSymbolInput
    from server.code.app.symbol import outline
    from server.code.app.symbol import symbol as h

    fake = _FakeRepo()
    joined: list[str] = []

    async def body(repo, org, ns, sym):
        joined.append(str(sym["symbol_id"]))
        return f"<joined body {sym['symbol_id']}>"

    async def go():
        with (
            patch.object(h, "CodeRepo", return_value=fake),
            patch.object(h, "symbol_body", new=body),
            patch.object(outline, "symbol_body", new=body),
            patch.object(h, "schedule_code_trace", new=lambda **k: None),
        ):
            return await h.handle_code_symbol(
                CodeSymbolInput(
                    name=name,
                    token_budget=budget,
                    include_history=False,
                    include_examples=False,
                    include_lessons=False,
                )
            )

    return asyncio.run(go()), fake, joined


def test_symbol_serves_a_large_class_as_skeleton_plus_member_outline(monkeypatch):
    _settings(monkeypatch)
    out, fake, joined = _symbol("DB::Context")
    assert joined == [], "no chunk join for a 1,400-line class"
    assert fake.skeleton_calls == [("src/Context.h", "DB::Context")]
    assert fake.outline_calls == [("src/Context.h", None)]
    [u] = out.definitions
    assert u.granularity == "skeleton" and u.body.startswith("class Context {") and not u.elided
    assert [(e["qualified_name"], e["start_line"]) for e in u.outline] == [
        ("DB::Context::getClientInfo", 1054),
        ("DB::Context::setSetting", 1400),
    ], "members inside the span, the class itself excluded, source order"
    assert u.next == "", "everything fit: no escalation"
    wire = u.model_dump()
    assert "outline" in wire and wire["granularity"] == "skeleton"


def test_symbol_small_units_keep_the_joined_body_and_omit_outline(monkeypatch):
    _settings(monkeypatch)
    out, fake, joined = _symbol("DB::Holder")  # 15-line struct: below the threshold
    assert joined == ["st"] and fake.skeleton_calls == []
    [u] = out.definitions
    assert u.body == "<joined body st>" and u.outline == [] and u.granularity == "unit"
    assert "outline" not in u.model_dump(), "empty enrichment stays off the wire"


def test_symbol_skeleton_under_a_tight_budget_escalates_to_code_outline(monkeypatch):
    _settings(monkeypatch)
    # 200 tokens: the ~600-token skeleton leaves no room for members, and
    # budget_units then cards the unit — either way `next` is the file outline,
    # never the class itself
    out, _fake, _ = _symbol("DB::Context", budget=200)
    assert out.definitions, "the card still ships"
    [u] = out.definitions
    assert u.next == "code.outline path=src/Context.h"


def test_card_of_a_skeleton_unit_points_at_the_file_outline():
    from server.code.domain.units import CodeUnit, _card_of

    u = CodeUnit(
        path="src/Context.h",
        symbol="DB::Context",
        body="x" * 50,
        granularity="skeleton",
        outline=[
            {
                "kind": "method",
                "qualified_name": "DB::Context::f",
                "signature": "",
                "start_line": 1,
                "end_line": 1,
                "tier": "scip",
            }
        ],
    )
    assert _card_of(u).next == "code.outline path=src/Context.h"
    assert _card_of(CodeUnit(path="p", symbol="S", body="b")).next == "code.symbol name=S"


def test_repo_relative_strips_prefixes_not_characters() -> None:
    from server.code.app.common import repo_relative

    assert repo_relative("./src/Context.h") == "src/Context.h"
    assert repo_relative("././src/x.py") == "src/x.py"
    assert repo_relative("/src/x.py") == "src/x.py"
    assert (
        repo_relative(" .github/workflows/ci.yml ") == ".github/workflows/ci.yml"
    )  # the dot stays
    assert repo_relative(".gitlab-ci.yml") == ".gitlab-ci.yml"
    assert repo_relative("docker\\server\\Dockerfile") == "docker/server/Dockerfile"


def test_outline_keeps_a_dot_directory_path(monkeypatch):
    _settings(monkeypatch)
    out, fake = _outline(path="./.github/workflows/ci.yml")
    assert fake.outline_calls[0][0] == ".github/workflows/ci.yml"
    assert out.path == ".github/workflows/ci.yml"


def test_large_lane_containers_take_the_outline_path_not_only_the_member_fold_set():
    """Rust trait / impl, a vitest suite, a TypeScript const object and a Prisma model are
    outlined like a C++ class once they span the threshold; the member fold's narrower
    set (callers of a class fold its methods' callers) is a separate constant."""
    from server.code.app.symbol.dispatch import _MEMBER_FOLD_KINDS
    from server.code.app.symbol.outline import AUTO_OUTLINE_MIN_LINES, _is_large_container

    def row(kind: str, lines: int) -> dict[str, Any]:
        return {"kind": kind, "start_line": 10, "end_line": 10 + lines - 1}

    for kind in ("trait", "impl", "suite", "const", "model", "class", "struct", "interface"):
        assert _is_large_container(row(kind, AUTO_OUTLINE_MIN_LINES)), kind
        assert not _is_large_container(row(kind, AUTO_OUTLINE_MIN_LINES - 1)), kind
    assert not _is_large_container(row("function", 5000)), "a long function keeps its body"
    assert not _is_large_container(row("namespace", 5000)), "a namespace has no skeleton"
    assert "trait" not in _MEMBER_FOLD_KINDS and "namespace" in _MEMBER_FOLD_KINDS
