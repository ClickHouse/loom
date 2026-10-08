"""A C++ call written with a scope names that scope's method.

`IStorage::getInMemoryMetadataPtr()` inside `MergeTreeData::getInMemoryMetadataPtr`
(an override calling its base) must bind to the base, not the override itself:
without the scope, rung 1 sees the file's one `getInMemoryMetadataPtr` and rung 2
the enclosing class's. On a large C++ tree this self-binding accounted for most
0.8-confidence edges where the compiler tier saw the base's method.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges
from server.code.domain.extract.name_scope import gate_name_table

BASE = b"""
namespace DB {
class IStorage {
public:
    virtual int getInMemoryMetadataPtr() const { return 1; }
    virtual int helper() const { return 2; }
};
}
"""
DERIVED = b"""
#include "IStorage.h"
namespace DB {
class MergeTreeData : public IStorage {
public:
    int getInMemoryMetadataPtr() const override;
    int helper() const override;
};
int MergeTreeData::getInMemoryMetadataPtr() const
{
    return IStorage::getInMemoryMetadataPtr() + 1;
}
int MergeTreeData::helper() const
{
    return helper() + getInMemoryMetadataPtr();
}
}
"""


def _corpus():
    files = {"src/Storages/IStorage.h": BASE, "src/Storages/MergeTreeData.cpp": DERIVED}
    parsed = {rel: parse_file(rel, src) for rel, src in files.items()}
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return parsed, gate_name_table(table, "cpp")


def test_a_scoped_call_binds_the_scopes_method_not_the_override_itself() -> None:
    parsed, table = _corpus()
    edges = extract_edges(parsed["src/Storages/MergeTreeData.cpp"], table)
    calls = {
        (e.line, e.to_qualified_name, round(e.confidence, 1))
        for e in edges
        if e.edge_kind == "calls"
    }
    scoped = {t for line, t, _c in calls if line == 11}
    assert scoped == {"DB::IStorage::getInMemoryMetadataPtr"}, calls
    assert not any(e.from_qualified_name == e.to_qualified_name and e.line == 11 for e in edges)


def test_a_bare_call_inside_a_method_still_reaches_the_enclosing_class() -> None:
    """`helper()` and `getInMemoryMetadataPtr()` written bare inside
    `MergeTreeData::helper` keep rung 2: the enclosing class's methods (the
    first is recursion, which C++ allows and the compiler tier records)."""
    parsed, table = _corpus()
    edges = extract_edges(parsed["src/Storages/MergeTreeData.cpp"], table)
    at15 = {e.to_qualified_name for e in edges if e.edge_kind == "calls" and e.line == 15}
    assert at15 == {"DB::MergeTreeData::helper", "DB::MergeTreeData::getInMemoryMetadataPtr"}, at15
