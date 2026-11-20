"""The Thrift IDL lane: symbols, reference sites, includes and registration."""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, Mock

import pytest

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


def _uses_type(path: str, src: bytes) -> set[tuple[str, str]]:
    pf = th.parse_thrift(path, src)
    edges = bind_sites(pf, th.reference_sites(pf), gate_name_table({}, "thrift"), th.SITE_RULES)
    return {
        (e.from_qualified_name.split("::")[1], e.to_qualified_name.split("::")[1])
        for e in edges
        if e.edge_kind == "uses_type"
    }


def test_fields_without_a_numeric_id_record_their_types() -> None:
    # the IDL grammar makes `FieldID ':'` optional
    src = b"struct User { 1: string id }\nstruct Envelope { optional User user }\n"
    assert (2, "User", "field_type") in th.thrift_units("idl/e.thrift", src.decode()).sites
    assert ("Envelope", "User") in _uses_type("idl/e.thrift", src)
    with_id = b"struct User { 1: string id }\nstruct Envelope { 1: optional User user }\n"
    assert ("Envelope", "User") in _uses_type("idl/e.thrift", with_id)


def test_id_less_fields_in_arguments_throws_and_mixed_lists() -> None:
    src = b"""struct Id { 1: i64 v }
struct Tag { 1: string t }
exception Missing { 1: string why }
struct Mixed {
  required Id id = {"v": 1}
  list<Tag> (python.immutable = "") tags;
  3: Tag& primary xsd_optional (doc = "x"),
  map<string, Id> byName
}
service Lookup {
  Mixed find(Id id, 2: Tag tag) throws (Missing missing)
}
"""
    sites = th.thrift_units("idl/l.thrift", src.decode()).sites
    assert {
        (5, "Id", "field_type"),
        (6, "Tag", "field_type"),
        (7, "Tag", "field_type"),
        (8, "Id", "field_type"),
    } <= set(sites)
    got = _uses_type("idl/l.thrift", src)
    assert {("Mixed", "Id"), ("Mixed", "Tag")} <= got
    assert {("Lookup.find", "Id"), ("Lookup.find", "Tag"), ("Lookup.find", "Missing")} <= got
    # field names, default values and annotations are not types
    names = {n for _, n, _ in sites}
    assert not names & {"id", "tags", "primary", "byName", "v", "python", "doc", "x"}


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


async def test_root_file_types_bind_through_the_production_name_gate() -> None:
    from server.code.app.index.ingest_edges import _lane_site_edges

    shared = th.parse_thrift("shared.thrift", b"struct Payload {}\nservice Base {}\n")
    caller = th.parse_thrift(
        "main.thrift",
        b'include "shared.thrift"\nservice Child extends shared.Base {\n'
        b"  shared.Payload get()\n}\n",
    )
    table = {
        symbol.name: [(symbol.qualified_name, symbol.qualified_name, symbol.kind)]
        for symbol in shared.symbols
    }
    table["Base"].append(("native-base", "Native::Base", "interface"))
    table["Payload"].append(("native-payload", "Native::Payload", "struct"))
    repo = Mock(name_candidates=AsyncMock(return_value=table))
    edges = await _lane_site_edges(repo, "o", "n", caller)
    assert {
        (edge.from_qualified_name, edge.to_qualified_name, edge.edge_kind, edge.confidence)
        for edge in edges
    } == {
        ("main.thrift::Child", "shared.thrift::Base", "inherits", 0.6),
        ("main.thrift::Child.get", "shared.thrift::Payload", "uses_type", 0.6),
    }
    repo.name_candidates.assert_awaited_once_with("o", "n", ["Base", "Payload"])


def test_function_annotations_do_not_become_type_sites() -> None:
    src = b"""struct Audit { 1: string value }
service S {
  void ping()
  (Audit = "metadata")
  Audit fetch()
}
"""
    assert th.thrift_units("idl/main.thrift", src.decode()).sites == [(5, "Audit", "field_type")]
    assert _uses_type("idl/main.thrift", src) == {("S.fetch", "Audit")}


@pytest.mark.parametrize(
    "default", ["+1", "-1", "+1.5e+2", "-1.5e-2", ".5", "+.5", "-.5", "+.5e-2"]
)
@pytest.mark.parametrize("field_id", ["", "+1: "])
def test_signed_defaults_preserve_the_following_id_less_field(default: str, field_id: str) -> None:
    src = (
        "struct Item { 1: string value }\n"
        f"struct Box {{\n  {field_id}double scale = {default}\n  Item item\n}}\n"
    ).encode()
    assert _uses_type("idl/main.thrift", src) == {("Box", "Item")}


def _nested_attrs(n: int) -> str:
    return (
        "struct Leaf {}\nstruct A {\n"
        + "1: A a xsd_attrs {\n" * n
        + "1: Leaf leaf\n"
        + "}\n" * n
        + "2: Leaf tail\n}\n"
    )


def test_nested_xsd_attrs_preserve_source_order_without_recursion() -> None:
    depth = 1_500
    units = th.thrift_units("idl/main.thrift", _nested_attrs(depth))
    assert [symbol.name for symbol in units.symbols] == ["Leaf", "A"]
    assert units.sites == [
        *((line, "A", "field_type") for line in range(3, depth + 3)),
        (depth + 3, "Leaf", "field_type"),
        (depth * 2 + 4, "Leaf", "field_type"),
    ]


def test_nested_xsd_attrs_cost_linear_time() -> None:
    def cost(n: int) -> float:
        text = _nested_attrs(n)
        started = time.perf_counter()
        th.thrift_units("idl/main.thrift", text)
        return time.perf_counter() - started

    assert_linear(cost, 500, 2_000)


def test_nested_type_annotations_are_not_type_sites() -> None:
    src = b"""struct Audit {}
struct Item {}
struct Box {
  1: list<string (Audit = "metadata")> names
  2: map<string (Audit = "metadata"), list<Item> (Audit = "metadata")> values
}
"""
    assert th.thrift_units("idl/main.thrift", src.decode()).sites == [(5, "Item", "field_type")]
    assert _uses_type("idl/main.thrift", src) == {("Box", "Item")}


def test_an_annotated_alias_keeps_its_symbol_and_type_edge() -> None:
    src = b"""struct Audit {}
struct Meta {}
typedef list<Audit> (Meta = "metadata") AuditList
"""
    units = th.thrift_units("idl/main.thrift", src.decode())
    alias = next(symbol for symbol in units.symbols if symbol.name == "AuditList")
    assert (alias.kind, alias.start_line, alias.end_line) == ("type", 3, 3)
    assert units.sites == [(3, "Audit", "field_type")]
    assert _uses_type("idl/main.thrift", src) == {("AuditList", "Audit")}


def test_an_annotated_return_keeps_its_signature_span_and_source_edge() -> None:
    src = b"""struct Audit {}
struct Meta {}
service S {
  list<Audit>
  (Meta = "metadata")
  fetch()
  string (Meta = "metadata") ping()
  Audit following()
}
"""
    units = th.thrift_units("idl/main.thrift", src.decode())
    by_name = {symbol.name: symbol for symbol in units.symbols}
    fetch = by_name["fetch"]
    assert (fetch.start_line, fetch.end_line) == (4, 6)
    assert fetch.signature == 'list<Audit>(Meta = "metadata") fetch()'
    assert by_name["ping"].signature == 'string(Meta = "metadata") ping()'
    assert units.sites == [(4, "Audit", "field_type"), (8, "Audit", "field_type")]
    assert _uses_type("idl/main.thrift", src) == {("S.fetch", "Audit"), ("S.following", "Audit")}


def test_native_type_hints_do_not_hide_nested_types_or_create_sites() -> None:
    src = b"""struct Audit {}
struct Item {}
typedef list cpp_type "vector<Audit>" <Item> Items
struct Box {
  1: map cpp_type "Map" <string, list cpp_type "Vector" <Item>> entries
}
service S {
  list cpp_type "vector<Audit>" <Item> fetch()
}
"""
    units = th.thrift_units("idl/main.thrift", src.decode())
    assert [symbol.name for symbol in units.symbols] == [
        "Audit",
        "Item",
        "Items",
        "Box",
        "S",
        "fetch",
    ]
    assert units.sites == [
        (3, "Item", "field_type"),
        (5, "Item", "field_type"),
        (8, "Item", "field_type"),
    ]
    assert _uses_type("idl/main.thrift", src) == {
        ("Items", "Item"),
        ("Box", "Item"),
        ("S.fetch", "Item"),
    }
