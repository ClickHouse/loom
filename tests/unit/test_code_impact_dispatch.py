"""Override callers resolve through the base virtual; diff-shaped impact
seeds by hunk-header function name (drift-proof) and keeps files past the
20th."""

from __future__ import annotations

import pytest

from server.code.app.symbol import diff, dispatch, graph
from server.settings import settings

# The scalar code repository these tests model (none is configured by default).
pytestmark = pytest.mark.usefixtures("scalar_code_repo")

DIFF = """\
diff --git a/src/Core/BaseSettings.h b/src/Core/BaseSettings.h
--- a/src/Core/BaseSettings.h
+++ b/src/Core/BaseSettings.h
@@ -707,7 +707,7 @@ void BaseSettings<TTraits>::write(WriteBuffer & out, SettingsWriteFormat format)
 x
@@ -313,8 +313,8 @@ public:
 y
diff --git a/src/Common/maskURIPassword.h b/src/Common/maskURIPassword.h
--- a/src/Common/maskURIPassword.h
+++ b/src/Common/maskURIPassword.h
@@ -152,32 +152,55 @@ inline bool maskPresignedURLParameters(std::string & url)
 z
diff --git a/src/Core/SettingsSecrets.cpp b/src/Core/SettingsSecrets.cpp
--- /dev/null
+++ b/src/Core/SettingsSecrets.cpp
@@ -0,0 +1,36 @@
+new
"""


def test_hunk_context_name_extraction() -> None:
    f = diff._hunk_context_name
    assert (
        f("void BaseSettings<TTraits>::write(WriteBuffer & out, SettingsWriteFormat format)")
        == "BaseSettings::write"
    )
    assert (
        f("inline bool maskPresignedURLParameters(std::string & url)")
        == "maskPresignedURLParameters"
    )
    assert (
        f("void CoordinationSettings::dump(WriteBufferFromOwnString & buf) const")
        == "CoordinationSettings::dump"
    )
    assert f("public:") == ""
    assert f("try") == ""
    assert f("Skip fields with unsupported types while schema inference for format BSON.") == ""
    assert f("    if (x)") == ""


def test_parse_unified_diff_full_ranges_and_names() -> None:
    ranges, names = diff._parse_unified_diff_full(DIFF)
    assert ranges["src/Core/BaseSettings.h"] == [(707, 713), (313, 320)]
    assert names["src/Core/BaseSettings.h"] == {"BaseSettings::write"}
    assert names["src/Common/maskURIPassword.h"] == {"maskPresignedURLParameters"}
    assert ranges["src/Core/SettingsSecrets.cpp"] == [(1, 36)]  # new file: whole span
    assert ranges["src/Common/maskURIPassword.h"] == [(152, 206)]


def test_name_matches_is_suffix_qualified_only() -> None:
    assert diff._name_matches("DB::BaseSettings::write", "BaseSettings::write")
    assert diff._name_matches("DB::maskPresignedURLParameters", "maskPresignedURLParameters")
    assert not diff._name_matches("DB::Other::write", "BaseSettings::write")
    assert not diff._name_matches("DB::BaseSettings::write", "")


def test_arity() -> None:
    assert dispatch._arity("void f()") == 0
    assert dispatch._arity("void f(int a)") == 1
    assert (
        dispatch._arity(
            "void read(QueryPlan &, const Names &, const StorageSnapshotPtr &, SelectQueryInfo &, ContextPtr, QueryProcessingStage::Enum, size_t, size_t) override"
        )
        == 8
    )
    assert dispatch._arity("no parens") == 0


class _Repo:
    """Fake corpus shaped like the real one: signatures carry NO virtual/override
    keywords, the override's class has no inherits edge indexed, IStorage is a
    base (something inherits from it), IReadable::read has the wrong arity."""

    SIG8 = "(QueryPlan &, const Names &, const StorageSnapshotPtr &, SelectQueryInfo &, ContextPtr, QueryProcessingStage::Enum, size_t, size_t)"

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        if name == "DB::StorageReplicatedMergeTree":
            return [{"symbol_id": "cls1", "qualified_name": name, "kind": "class"}]
        if name == "DB::IStorage":
            return [{"symbol_id": "istorage", "qualified_name": name, "kind": "class"}]
        if name == "DB::IReadable":
            return [{"symbol_id": "ireadable", "qualified_name": name, "kind": "class"}]
        return []

    async def methods_named(self, org, ns, method, *, limit=300):
        assert method == "read"
        return [
            {
                "symbol_id": "ovr",
                "qualified_name": "DB::StorageReplicatedMergeTree::read",
                "kind": "function",
                "signature": self.SIG8,
            },
            {
                "symbol_id": "base1",
                "qualified_name": "DB::IStorage::read",
                "kind": "function",
                "signature": self.SIG8,
            },
            {
                "symbol_id": "other",
                "qualified_name": "DB::IReadable::read",
                "kind": "function",
                "signature": "(char *, size_t)",
            },
        ]

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        return []  # NO inherits edges anywhere (IStorage qualifies by its interface name)


@pytest.mark.asyncio
async def test_dispatch_seeds_fall_back_to_virtual_by_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "code_intel_enabled", True)
    seeds = [
        {
            "symbol_id": "ovr",
            "qualified_name": "DB::StorageReplicatedMergeTree::read",
            "kind": "function",
            "signature": _Repo.SIG8,
        }
    ]
    out = await dispatch._dispatch_seeds(_Repo(), "clickhouse", "code-clickhouse", seeds)  # type: ignore[arg-type]
    assert [(r["qualified_name"], r["via"]) for r in out] == [
        ("DB::IStorage::read", "virtual_by_name")
    ]


class _ManyRepo:
    async def symbols_by_name(self, org, ns, name, *, limit=50):
        return [
            {
                "symbol_id": f"s{i}",
                "qualified_name": f"DB::Class{i}::setColumns",
                "kind": "function",
                "signature": "(x)",
                "path": f"src/{i}.cpp",
                "tier": "scip",
            }
            for i in range(12)
        ]

    async def symbols_by_ids(self, org, ns, ids):
        return []

    async def edges_for_symbols(self, *a, **k):
        raise AssertionError("must not walk the graph for an ambiguous bare name")


@pytest.mark.asyncio
async def test_callers_refuses_ambiguous_bare_name_without_walking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.code.api.models_symbol import CodeCallersInput

    monkeypatch.setattr(settings, "code_intel_enabled", True)
    monkeypatch.setattr(graph, "CodeRepo", _ManyRepo)
    out = await graph.handle_code_callers(CodeCallersInput(name="setColumns"))
    assert out.ambiguous is True and out.edges == []
    assert len(out.candidates) == 12 and "qualify" in out.note


@pytest.mark.asyncio
async def test_dispatch_skips_heuristics_when_namespace_has_override_edges(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _has(repo, org, ns):
        return True

    monkeypatch.setattr(dispatch, "_namespace_has_override_edges", _has)
    seeds = [
        {
            "symbol_id": "ovr",
            "qualified_name": "DB::StorageReplicatedMergeTree::read",
            "kind": "function",
            "signature": _Repo.SIG8,
        }
    ]
    # _Repo has no overrides edge for the seed and would normally fall back by name
    assert await dispatch._dispatch_seeds(_Repo(), "o", "n", seeds) == []  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_dispatch_seeds_skip_free_functions() -> None:
    seeds = [
        {"symbol_id": "f", "qualified_name": "freeFunction", "kind": "function", "signature": "()"}
    ]
    assert await dispatch._dispatch_seeds(_Repo(), "o", "n", seeds) == []  # type: ignore[arg-type]


class _HierRepo(_Repo):
    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        if direction == "callees" and ids == ["cls1"] and edge_kinds == ["inherits"]:
            return [{"to_qualified_name": "DB::MergeTreeData", "to_symbol_id": "cls2"}]
        return await super().edges_for_symbols(
            org, ns, ids, direction=direction, edge_kinds=edge_kinds, limit=limit
        )

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        if name == "DB::MergeTreeData::read":
            return [
                {
                    "symbol_id": "b2",
                    "qualified_name": name,
                    "kind": "function",
                    "signature": self.SIG8,
                }
            ]
        return await super().symbols_by_name(org, ns, name, limit=limit)


@pytest.mark.asyncio
async def test_dispatch_seeds_prefer_hierarchy_when_indexed() -> None:
    seeds = [
        {
            "symbol_id": "ovr",
            "qualified_name": "DB::StorageReplicatedMergeTree::read",
            "kind": "function",
            "signature": _Repo.SIG8,
        }
    ]
    out = await dispatch._dispatch_seeds(_HierRepo(), "o", "n", seeds)  # type: ignore[arg-type]
    assert [(r["qualified_name"], r["via"]) for r in out] == [
        ("DB::MergeTreeData::read", "inherits")
    ]


class _GoHierRepo(_Repo):
    """A Go namespace whose compiler tier carries
    `overrides` edges (scip-go's standard-library relationships) but none for
    the seed; the fast tier's struct → interface edge is what reaches the
    interface method."""

    async def edges_for_symbols(self, org, ns, ids, *, direction, edge_kinds=None, limit=200):
        if direction == "callees" and ids == ["st1"] and edge_kinds == ["inherits"]:
            return [{"to_qualified_name": "lib.driver.driver::Conn", "to_symbol_id": "if1"}]
        return await super().edges_for_symbols(
            org, ns, ids, direction=direction, edge_kinds=edge_kinds, limit=limit
        )

    async def symbols_by_name(self, org, ns, name, *, limit=50):
        if name == "root.clickhouse::clickhouse":
            return [
                {
                    "symbol_id": "st1",
                    "qualified_name": name,
                    "kind": "struct",
                    "signature": "type clickhouse struct",
                }
            ]
        if name == "lib.driver.driver::Conn::Query":
            return [
                {
                    "symbol_id": "im1",
                    "qualified_name": name,
                    "kind": "method",
                    "signature": "(ctx context.Context) (Rows, error)",
                }
            ]
        return []

    async def methods_named(self, org, ns, method, *, limit=300):
        raise AssertionError("the by-name rung must stay behind the overrides gate")


@pytest.mark.asyncio
async def test_go_seed_keeps_the_hierarchy_walk_under_the_overrides_gate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _has(repo, org, ns):
        return True

    monkeypatch.setattr(dispatch, "_namespace_has_override_edges", _has)
    seeds = [
        {
            "symbol_id": "ovr",
            "qualified_name": "root.clickhouse::clickhouse::Query",
            "kind": "function",
            "signature": "(ctx context.Context) (rows driver.Rows, err error)",
            "path": "clickhouse.go",
        }
    ]
    out = await dispatch._dispatch_seeds(_GoHierRepo(), "o", "n", seeds)  # type: ignore[arg-type]
    assert [(r["qualified_name"], r["via"]) for r in out] == [
        ("lib.driver.driver::Conn::Query", "inherits")
    ]
    # a C++ seed under the same gate still skips every heuristic (scip-clang is complete)
    cpp = [
        {
            "symbol_id": "ovr",
            "qualified_name": "DB::StorageReplicatedMergeTree::read",
            "kind": "function",
            "signature": _Repo.SIG8,
            "path": "src/Storages/StorageReplicatedMergeTree.cpp",
        }
    ]
    assert await dispatch._dispatch_seeds(_Repo(), "o", "n", cpp) == []  # type: ignore[arg-type]


def test_partial_relationship_languages() -> None:
    assert dispatch._partial_relationships("clickhouse.go")
    assert dispatch._partial_relationships("web/src/Grid.tsx")
    assert dispatch._partial_relationships("server/memory/app/x.py")
    assert not dispatch._partial_relationships("src/Core/Field.cpp")
    assert not dispatch._partial_relationships("")


def test_touched_rows_fold_a_definitions_tier_ids_and_rank_header_first() -> None:
    def row(sid: str, name: str, path: str, start: int, end: int, tier: str) -> dict:
        return {
            "symbol_id": sid,
            "qualified_name": name,
            "path": path,
            "start_line": start,
            "end_line": end,
            "tier": tier,
        }

    rows = [
        row("ts_f", "DB::f", "src/a.cpp", 10, 20, "treesitter"),  # same definition, parser tier
        row("sc_f", "DB::f", "src/a.cpp", 10, 10, "scip"),
        row("sc_g", "DB::g", "src/a.cpp", 30, 40, "scip"),  # a hunk inside g
        row("sc_h", "DB::h", "src/a.cpp", 90, 99, "scip"),  # untouched
        row("sc_k", "DB::k", "src/b.cpp", 1, 5, "scip"),  # files-only: every function
    ]
    touched, seed_ids = diff._touched_rows_for_diff(
        rows, {"src/a.cpp": [(35, 36)]}, {"src/a.cpp": {"f"}}
    )
    assert [(r["symbol_id"], r["via"]) for r in touched] == [
        ("sc_f", "header"),
        ("sc_g", "hunk"),
        ("sc_k", "file"),
    ]
    assert touched[0]["tier_ids"] == ["sc_f", "ts_f"]
    assert seed_ids == ["sc_f", "ts_f", "sc_g", "sc_k"]
