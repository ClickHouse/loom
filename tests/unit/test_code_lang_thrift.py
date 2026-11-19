"""The Thrift IDL lane: symbols, reference sites, includes and registration."""

from __future__ import annotations

import time

from server.code.app.index.ingest_parse import LANE_UNIT_LANGS, _lane_module
from server.code.domain.extract import lang_thrift as th
from server.code.domain.extract.lane_units import bind_sites
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.name_scope import gate_name_table
from tests.unit.code_fixtures import assert_linear

SRC = b"""namespace py acme
include "shared.thrift"
include "shared.thrift"

typedef i64 UserId  // trailing comment
enum Color { RED = 1, BLUE = 2 }

/* struct Ghost { } inside a comment is not a symbol */
struct User {
  1: required UserId id,
  2: optional list<Color> colors,
  3: map<string, shared.Address> addresses,
}

union Key { 1: UserId id, 2: string email }
exception NotFound { 1: string msg }

service Users extends shared.Base {
  User get(1: UserId id) throws (1: NotFound nf),
  oneway void ping();
  set<User> all()
}
"""


def _units() -> th.ThriftUnits:
    return th.thrift_units("idl/users.thrift", SRC.decode())


def test_symbols_kinds_and_names() -> None:
    got = {(s.kind, s.qualified_name) for s in _units().symbols}
    assert got == {
        ("type", "idl.users::UserId"),
        ("enum", "idl.users::Color"),
        ("struct", "idl.users::User"),
        ("struct", "idl.users::Key"),
        ("struct", "idl.users::NotFound"),
        ("interface", "idl.users::Users"),
        ("function", "idl.users::Users.get"),
        ("function", "idl.users::Users.ping"),
        ("function", "idl.users::Users.all"),
    }


def test_spans_and_signatures() -> None:
    by = {s.name: s for s in _units().symbols}
    assert (by["User"].start_line, by["User"].end_line) == (9, 13)
    assert (by["Users"].start_line, by["Users"].end_line) == (18, 22)
    assert by["get"].signature == "User get(1: UserId id) throws(1: NotFound nf)"
    assert by["ping"].signature == "oneway void ping()"
    assert by["all"].signature == "set<User> all()"
    assert by["NotFound"].signature == "exception NotFound"
    assert by["UserId"].signature == "typedef i64 UserId"


def test_reference_sites_read_through_containers_and_includes() -> None:
    sites = _units().sites
    assert (10, "UserId", "field_type") in sites
    assert (11, "Color", "field_type") in sites
    assert (12, "Address", "field_type") in sites  # `shared.Address` binds by its last part
    assert (18, "Base", "extends") in sites
    assert {(19, "User", "field_type"), (19, "NotFound", "field_type")} <= set(sites)
    assert (21, "User", "field_type") in sites
    names = {n for _, n, _ in sites}
    assert not names & th.BASE_TYPES
    assert "Ghost" not in {s.name for s in _units().symbols}


def test_includes_are_deduplicated_and_resolved_nearest_first() -> None:
    assert _units().includes == ["shared.thrift"]
    present = {"idl/shared.thrift", "shared.thrift"}
    assert th.resolve_imports("idl/users.thrift", ["shared.thrift"], present) == [
        "idl/shared.thrift"
    ]
    assert "idl/shared.thrift" in th.wanted_paths("idl/users.thrift", ["shared.thrift"])


def test_includes_inside_comments_are_not_includes() -> None:
    src = b'/*\ninclude "unused.thrift"\n*/\nstruct Live { 1: i32 x }\n'
    assert th.parse_thrift("idl/main.thrift", src).includes == []
    text = (
        '// include "a.thrift"\n# include "b.thrift"\ninclude "c.thrift" /* include "d.thrift" */\n'
    )
    assert th.thrift_units("m.thrift", text).includes == ["c.thrift"]
    # an unclosed string is not a file name
    assert th.thrift_units("m.thrift", 'include "x').includes == []


def test_unterminated_input_does_not_raise() -> None:
    for text in ("struct A {", "service S { void f(", "typedef list<", 'include "x', "/* open"):
        th.thrift_units("a.thrift", text)


def test_registered_as_a_lane() -> None:
    assert detect_by_extension("api/users.thrift") == "thrift"
    assert "thrift" in LANE_UNIT_LANGS
    assert _lane_module("thrift") is th
    pf = th.parse_thrift("a.thrift", SRC)
    assert pf.lang == "thrift" and pf.tree is None and pf.includes == ["shared.thrift"]


def test_edges_bind_same_file_then_repo_wide_and_builtins_to_nothing() -> None:
    pf = th.parse_thrift("idl/users.thrift", SRC)
    table = {
        "Address": [("sid1", "idl.shared::Address", "struct")],
        "Base": [("sid2", "idl.shared::Base", "interface")],
        "Ghost": [("sid3", "a::Ghost", "struct"), ("sid4", "b::Ghost", "struct")],
    }
    edges = bind_sites(pf, th.reference_sites(pf), gate_name_table(table, "thrift"), th.SITE_RULES)
    got = {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1], e.edge_kind)
        for e in edges
    }
    assert ("User", "UserId", "uses_type") in got  # same file
    assert ("User", "Color", "uses_type") in got  # inside list<>
    assert ("User", "Address", "uses_type") in got  # included, repository-unique
    assert ("Users", "Base", "inherits") in got
    assert ("Users.get", "NotFound", "uses_type") in got  # throws
    assert ("Users.all", "User", "uses_type") in got  # return inside set<>
    assert not any(t in th.BASE_TYPES for _f, t, _k in got)


def _cost(n: int) -> float:
    text = "".join(
        f"struct S{i} {{ 1: list<S{i - 1}> a }}\nservice V{i} {{ S{i} f(1: S{i} x) }}\n"
        for i in range(n)
    )
    best = float("inf")
    for _ in range(3):
        t = time.perf_counter()
        th.thrift_units("a.thrift", text)
        best = min(best, time.perf_counter() - t)
    return best


def test_a_file_of_many_definitions_parses_in_linear_time() -> None:
    assert_linear(_cost, 1_000, 4_000)


def test_unclosed_headers_repeated_cost_linear_time() -> None:
    def cost(n: int) -> float:
        text = "typedef list< struct A service S extends " * n
        t = time.perf_counter()
        th.thrift_units("a.thrift", text)
        return time.perf_counter() - t

    assert_linear(cost, 2_000, 8_000)
