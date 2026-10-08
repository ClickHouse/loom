"""A Go return type keeps its package qualifier, and the
return-type read runs a second round for the callees a now-typed receiver
resolves — `conn, err := GetConn()` / `batch, _ := conn.PrepareBatch()` /
`batch.Append(…)` bind to `driver.Conn::PrepareBatch` and
`driver.Batch::Append` from another package."""

from __future__ import annotations

from server.code.domain.extract.edges_treesitter import callee_targets, extract_edges_and_misses
from server.code.domain.extract.lang_go import parse_go
from server.code.domain.extract.name_scope import gate_name_table

DRIVER = b"""package driver

type Batch interface {
    Append(v ...any) error
    Send() error
}

type Conn interface {
    PrepareBatch(ctx context.Context, query string) (Batch, error)
    Exec(ctx context.Context, query string) error
}
"""

HELPER = b"""package tests

import "example.com/ch/lib/driver"

func GetConn(t *testing.T) (driver.Conn, error) { return nil, nil }

func NewStore() *Store { return &Store{} }

type Store struct{}

// a second PrepareBatch in the repository: the bare name ties until the
// receiver is typed (clickhouse-go: five `Exec`, four `PrepareBatch`)
func (s *Store) PrepareBatch(ctx context.Context, query string) (*Store, error) { return s, nil }

func (s *Store) Exec(ctx context.Context, query string) error { return nil }
"""

TEST = b"""package tests

import (
    "context"
    "example.com/ch/lib/driver"
)

func TestFloat(t *testing.T) {
    ctx := context.Background()
    conn, err := GetConn(t)
    if err != nil {
        t.Fatal(err)
    }
    conn.Exec(ctx, "DROP TABLE x")
    batch, err := conn.PrepareBatch(ctx, "INSERT INTO x")
    batch.Append(1.5)
    batch.Send()
    _ = driver.Conn(nil)
}
"""


def _table(*pfs):
    t: dict[str, list[tuple[str, str, str]]] = {}
    for pf in pfs:
        for s in pf.symbols:
            t.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))
    return gate_name_table(t, "go")


def test_return_type_keeps_the_package_qualifier() -> None:
    pf = parse_go("tests/main_test.go", HELPER)
    rt = {s.name: s.return_type for s in pf.symbols if s.kind == "function"}
    assert rt["GetConn"] == "driver.Conn"
    assert rt["NewStore"] == "Store", "a bare result stays bare"
    drv = parse_go("lib/driver/driver.go", DRIVER)
    methods = {s.qualified_name: s.return_type for s in drv.symbols if s.kind == "method"}
    assert methods["lib.driver.driver::Conn::PrepareBatch"] == "Batch"


def test_second_round_resolves_the_chained_callee() -> None:
    drv = parse_go("lib/driver/driver.go", DRIVER)
    helper = parse_go("tests/main_test.go", HELPER)
    test = parse_go("tests/float_test.go", TEST)
    table = _table(drv, helper, test)
    returns_by_qn = {
        s.qualified_name: s.return_type for pf in (drv, helper) for s in pf.symbols if s.return_type
    }
    ctx = {"go_packages": {"driver": "lib.driver"}}
    first = callee_targets(test, table, ctx)
    assert first["GetConn"] == "tests.tests::GetConn"
    assert "conn.PrepareBatch" not in first, "the receiver is untyped before the first read"
    returns = {c: returns_by_qn[qn] for c, qn in first.items() if qn in returns_by_qn}
    second = callee_targets(test, table, ctx, returns)
    assert second["conn.PrepareBatch"] == "lib.driver.driver::Conn::PrepareBatch"
    returns.update({c: returns_by_qn[qn] for c, qn in second.items() if qn in returns_by_qn})
    edges, misses = extract_edges_and_misses(test, table, callee_returns=returns, lane_context=ctx)
    bound = {e.to_qualified_name for e in edges if e.edge_kind == "calls"}
    assert {
        "lib.driver.driver::Conn::Exec",
        "lib.driver.driver::Conn::PrepareBatch",
        "lib.driver.driver::Batch::Append",
        "lib.driver.driver::Batch::Send",
    } <= bound
    assert not [m for m in misses if m.callee_name in ("Exec", "PrepareBatch", "Append", "Send")]
