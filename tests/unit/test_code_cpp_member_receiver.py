"""A C++ member call has a receiver.

`_receiver_text` never read a C++ `field_expression`, so `volume->getDisk()`
reached the resolver as a bare call and took the file's one `getDisk` — the
enclosing class's own — at 1.0, where the compiler tier saw `IVolume::getDisk`.
"""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges
from server.code.domain.extract.name_scope import gate_name_table

VOLUME_H = b"""
namespace DB {
class IVolume {
public:
    int getDisk() const { return 1; }
};
}
"""
STORAGE_CPP = b"""
#include "IVolume.h"
namespace DB {
class DataPartStorageOnDiskBase {
public:
    int getDisk() const;
    int helper() const;
    IVolume * volume;
};
int DataPartStorageOnDiskBase::getDisk() const
{
    return volume->getDisk();
}
int DataPartStorageOnDiskBase::helper() const
{
    return this->getDisk() + getDisk() + (volume)->getDisk();
}
}
"""


def _edges():
    files = {
        "src/Disks/IVolume.h": VOLUME_H,
        "src/Storages/DataPartStorageOnDiskBase.cpp": STORAGE_CPP,
    }
    parsed = {rel: parse_file(rel, src) for rel, src in files.items()}
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    edges = extract_edges(
        parsed["src/Storages/DataPartStorageOnDiskBase.cpp"], gate_name_table(table, "cpp")
    )
    return [
        (e.line, e.to_qualified_name, round(e.confidence, 1))
        for e in edges
        if e.edge_kind == "calls"
    ]


def test_a_member_call_on_another_object_is_not_the_files_own_method() -> None:
    calls = _edges()
    at12 = {t for line, t, _c in calls if line == 12}  # volume->getDisk()
    assert "DB::DataPartStorageOnDiskBase::getDisk" not in at12
    # the receiver is untyped (no C++ typer yet), so the repo-wide rung guesses
    # among every `getDisk` — here two, so it abstains; nothing self-recursive
    assert at12 in (set(), {"DB::IVolume::getDisk"}), at12


def test_this_and_bare_calls_keep_the_enclosing_class() -> None:
    calls = _edges()
    at16 = [
        (t, c) for line, t, c in calls if line == 16
    ]  # this->getDisk() + getDisk() + (volume)->getDisk()
    own = [x for x in at16 if x[0] == "DB::DataPartStorageOnDiskBase::getDisk"]
    # `this->getDisk()` and the bare `getDisk()` both bind the class's own
    # method (one edge: the walk dedupes a line's repeats of one target)
    assert own == [("DB::DataPartStorageOnDiskBase::getDisk", 1.0)], at16
    # `(volume)->getDisk()` is an expression receiver: no same-file guess
    assert not any(
        t == "DB::DataPartStorageOnDiskBase::getDisk" and line == 12 for line, t, _c in calls
    )
