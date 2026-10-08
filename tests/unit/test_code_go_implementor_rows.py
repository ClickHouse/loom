"""The implementing type's own row is read when its methods
cover an interface declared in another package, and result types compare
without their package qualifiers — on clickhouse-go that takes the
struct→interface edges over 495 files from 2 to 83 (the driver's `Conn`
among them)."""

from __future__ import annotations

from server.code.domain.extract.edges_treesitter import Edge, extract_edges_and_misses
from server.code.domain.extract.go_implementors import (
    compatible,
    implementor_edges,
    implementor_type_names,
)
from server.code.domain.extract.lang_go import parse_go

DRIVER = b"""package driver

type Rows interface{ Next() bool }

type Conn interface {
    Query(ctx context.Context, query string) (Rows, error)
    Stats() Stats
    Close() error
}

type Stats struct{ Open int }
"""

IMPL_A = b"""package clickhouse

import "example.com/ch/lib/driver"

type clickhouse struct{ open int }

func (c *clickhouse) Query(ctx context.Context, query string) (rows driver.Rows, err error) {
    return nil, nil
}
"""

IMPL_B = b"""package clickhouse

import "example.com/ch/lib/driver"

func (c *clickhouse) Stats() driver.Stats { return driver.Stats{Open: c.open} }

func (c *clickhouse) Close() (err error) { return nil }
"""


def _table(*pfs):
    t: dict[str, list[tuple[str, str, str]]] = {}
    for pf in pfs:
        for s in pf.symbols:
            t.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return t


def test_qualified_result_types_are_compatible() -> None:
    assert compatible("() (*ServerVersion, error)", "() (*driver.ServerVersion, error)")
    assert compatible(
        "(ctx context.Context) (Rows, error)", "(ctx context.Context) (rows driver.Rows, err error)"
    )
    assert compatible("() Stats", "() driver.Stats")
    assert compatible("() map[string]Stats", "() map[string]driver.Stats")
    assert not compatible("() (Rows, error)", "() (Rows, bool)")
    assert not compatible("(a int) error", "(a int, b int) error")


def test_type_rows_are_wanted_when_methods_cover_the_interface() -> None:
    drv = parse_go("lib/driver/driver.go", DRIVER)
    a = parse_go("clickhouse.go", IMPL_A)
    b = parse_go("conn_stats.go", IMPL_B)
    # the ingest's table for driver.go: the names driver.go references — the
    # method rows repo-wide, but not the struct `clickhouse` (nothing in
    # driver.go names it)
    full = _table(drv, a, b)
    table = {
        k: v for k, v in full.items() if k in ("Query", "Stats", "Close", "Next", "Rows", "Conn")
    }
    sigs = {s.id: s.signature for pf in (a, b) for s in pf.symbols if s.kind == "function"}
    assert implementor_edges(drv, table, Edge, sigs) == [], "no type row, no edge (the 11070 gap)"
    assert implementor_type_names(drv, table) == ["clickhouse"]
    table["clickhouse"] = full["clickhouse"]
    edges = implementor_edges(drv, table, Edge, sigs)
    assert [
        (e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.confidence) for e in edges
    ] == [
        ("inherits", "root.clickhouse::clickhouse", "lib.driver.driver::Conn", 0.8),
    ]


def test_extractor_merges_the_type_rows_from_the_lane_context() -> None:
    drv = parse_go("lib/driver/driver.go", DRIVER)
    a = parse_go("clickhouse.go", IMPL_A)
    b = parse_go("conn_stats.go", IMPL_B)
    full = _table(drv, a, b)
    table = {
        k: v for k, v in full.items() if k in ("Query", "Stats", "Close", "Next", "Rows", "Conn")
    }
    sigs = {s.id: s.signature for pf in (a, b) for s in pf.symbols if s.kind == "function"}
    without, _m = extract_edges_and_misses(drv, table, lane_context={"go_method_sigs": sigs})
    assert not [e for e in without if e.edge_kind == "inherits"]
    with_rows, _m = extract_edges_and_misses(
        drv,
        table,
        lane_context={"go_method_sigs": sigs, "go_type_rows": {"clickhouse": full["clickhouse"]}},
    )
    assert [
        (e.from_qualified_name, e.to_qualified_name) for e in with_rows if e.edge_kind == "inherits"
    ] == [
        ("root.clickhouse::clickhouse", "lib.driver.driver::Conn"),
    ]
