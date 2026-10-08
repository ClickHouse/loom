"""A C++ member call's receiver is typed off the tree — a
parameter, a local, `auto` from a construction, a class field declared in
this file or in the sibling header — and a `std::` receiver abstains."""

from __future__ import annotations

import pytest

pytest.importorskip("tree_sitter_cpp")

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.cpp_receivers import CppReceiverTyper
from server.code.domain.extract.edges_treesitter import (
    HeaderDecls,
    extract_edges_and_misses,
)
from server.code.domain.extract.name_scope import gate_name_table

VOLUME_H = b"""
namespace DB {
class IVolume { public: int getDisk() const { return 1; } };
class Reader { public: int read() const { return 2; } };
class Sink { public: int flush() const { return 3; } };
}
"""
STORE_H = b"""
#include "IVolume.h"
namespace DB {
using ReaderPtr = std::shared_ptr<Reader>;
class Store {
public:
    int run(const IVolume & vol, Reader * rd) const;
    int other() const;
    IVolume * volume;
    ReaderPtr reader;
    std::vector<int> parts;
};
}
"""
STORE_CPP = b"""
#include "Store.h"
namespace DB {
int Store::run(const IVolume & vol, Reader * rd) const
{
    auto sink = std::make_shared<Sink>();
    auto local = Reader();
    return vol.getDisk() + rd->read() + sink->flush() + local.read();
}
int Store::other() const
{
    return volume->getDisk() + reader->read() + parts.size();
}
}
"""


def _corpus():
    files = {"src/IVolume.h": VOLUME_H, "src/Store.h": STORE_H, "src/Store.cpp": STORE_CPP}
    parsed = {rel: parse_file(rel, src) for rel, src in files.items()}
    table: dict[str, list[tuple[str, str, str]]] = {}
    for pf in parsed.values():
        for s in pf.symbols:
            table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return parsed, gate_name_table(table, "cpp")


def test_parameters_locals_and_constructions_type_the_receiver() -> None:
    parsed, table = _corpus()
    edges, _ = extract_edges_and_misses(
        parsed["src/Store.cpp"], table, None, None, {"present": set(parsed)}
    )
    at8 = {
        (e.to_qualified_name, round(e.confidence, 1))
        for e in edges
        if e.edge_kind == "calls" and e.line == 8
    }
    assert ("DB::IVolume::getDisk", 0.9) in at8  # `const IVolume & vol`
    assert ("DB::Reader::read", 0.9) in at8  # `Reader * rd` and `auto local = Reader()`
    assert ("DB::Sink::flush", 0.9) in at8  # `auto sink = std::make_shared<Sink>()`


def test_header_fields_type_the_receiver_and_a_std_container_abstains() -> None:
    parsed, table = _corpus()
    header = HeaderDecls()
    header.add_source("src/Store.h", STORE_H)
    edges, _ = extract_edges_and_misses(
        parsed["src/Store.cpp"], table, header, None, {"present": set(parsed)}
    )
    at12 = {
        (e.to_qualified_name, round(e.confidence, 1))
        for e in edges
        if e.edge_kind == "calls" and e.line == 12
    }
    assert ("DB::IVolume::getDisk", 0.9) in at12  # `IVolume * volume;` in the header
    assert ("DB::Reader::read", 0.9) in at12  # `ReaderPtr reader;` through the header's alias
    assert not any(t.endswith("::size") for t, _c in at12)  # `parts.size()` on a std::vector


def test_typer_reads_aliases_smart_pointers_and_std_marks() -> None:
    pf = parse_file("src/Store.h", STORE_H)
    t = CppReceiverTyper(pf)
    # the header's own methods have no bodies here; the typer's tables are what we check
    assert t._fold("ReaderPtr") == "Reader"
    assert t._members["Store"] == {
        "volume": "IVolume",
        "reader": "ReaderPtr",
        "parts": "std::vector",
    }
