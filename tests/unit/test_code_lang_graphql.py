"""GraphQL schema types are symbols, the fields of the root operation types are
functions, and a field's named type and an `implements` list are reference sites."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract.lane_units import bind_sites, span_chunks
from server.code.domain.extract.lang_detect import detect_by_extension
from server.code.domain.extract.lang_graphql import (
    CHUNK_OPTIONS,
    SITE_RULES,
    parse_graphql,
    reference_sites,
)
from server.code.domain.extract.treesitter import ParsedFile
from tests.unit.code_fixtures import assert_linear

SCHEMA = b'''# the shop API

"""
Anything with an id.
"""
interface Node {
  id: ID!
}

"A person who buys."
type Customer implements Node & Entity @key(fields: "id") {
  id: ID!
  "Orders, newest first."
  orders(first: Int = 10, state: [OrderState!] @deprecated): [Order!]!
  tier: Tier
}

enum Tier { STANDARD GOLD @deprecated(reason: "x") }
union Hit = | Customer | Order
scalar Moment @specifiedBy(url: "https://example.com/moment")
input OrderFilter { state: OrderState = PAID, since: Moment }
type Order implements Node { id: ID! placedAt: Moment buyer: Customer }

type Query {
  customer(id: ID!): Customer
  search(term: String!, filter: OrderFilter = {state: PAID}): [Hit]
}
'''

BILLING = b"""extend type Query {
  "Invoices after a moment."
  invoices(after: Moment): [Invoice!]!
}

type Invoice { order: Order! issuedAt: Moment }
"""


def _symbols(src: bytes, path: str = "shop/schema.graphql") -> dict[str, tuple]:
    pf = parse_graphql(path, src)
    return {
        s.qualified_name.split("::")[1]: (s.kind, s.signature, s.start_line, s.end_line)
        for s in pf.symbols
    }


def test_types_and_root_fields_are_symbols() -> None:
    got = _symbols(SCHEMA)
    assert got == {
        "Node": ("interface", "interface Node", 3, 8),
        "Customer": ("struct", "type Customer", 10, 16),
        "Tier": ("enum", "enum Tier", 18, 18),
        "Hit": ("union", "union Hit", 19, 19),
        "Moment": ("type", "scalar Moment", 20, 20),
        "OrderFilter": ("struct", "input OrderFilter", 21, 21),
        "Order": ("struct", "type Order", 22, 22),
        "Query": ("struct", "type Query", 24, 27),
        "Query.customer": ("function", "customer(id: ID!): Customer", 25, 25),
        "Query.search": (
            "function",
            "search(term: String!, filter: OrderFilter): [Hit]",
            26,
            26,
        ),
    }, "a description starts its definition; only root type fields are functions"
    pf = parse_graphql("shop/schema.graphql", SCHEMA)
    assert pf.symbols[0].qualified_name == "shop.schema::Node"
    assert pf.lang == "graphql" and pf.tree is None and pf.includes == []


def test_an_extended_root_type_adds_functions_but_no_type() -> None:
    got = _symbols(BILLING, "shop/billing.graphql")
    assert got == {
        "Query.invoices": ("function", "invoices(after: Moment): [Invoice!]!", 2, 3),
        "Invoice": ("struct", "type Invoice", 6, 6),
    }


def test_a_schema_block_names_the_root_types() -> None:
    src = b"""type Shop { me: Me }
type Query { stale: Me }
type Mutation { reset: Me }
schema @link(url: "x") { query: Shop mutation: Mutation }
"""
    got = _symbols(src)
    assert set(got) == {"Shop", "Shop.me", "Query", "Mutation", "Mutation.reset"}, (
        "`Shop` replaces `Query` as the query root, even when named after it"
    )


def test_field_types_and_implements_are_sites() -> None:
    pf = parse_graphql("shop/schema.graphql", SCHEMA)
    assert reference_sites(pf) == [
        (11, "Node", "implements"),
        (11, "Entity", "implements"),
        (14, "Order", "field_type"),
        (15, "Tier", "field_type"),
        (21, "OrderState", "field_type"),
        (21, "Moment", "field_type"),
        (22, "Node", "implements"),
        (22, "Moment", "field_type"),
        (22, "Customer", "field_type"),
        (25, "Customer", "field_type"),
        (26, "Hit", "field_type"),
    ], "built-in scalars, argument types, union members and directives name no site"


def test_sites_bind_in_file_then_across_the_repository() -> None:
    schema = parse_graphql("shop/schema.graphql", SCHEMA)
    billing = parse_graphql("shop/billing.graphql", BILLING)
    table: dict[str, list[tuple[str, str, str]]] = {}
    for s in [*schema.symbols, *billing.symbols]:
        table.setdefault(s.name, []).append((s.id, s.qualified_name, s.kind))

    def edges(pf: ParsedFile) -> set[tuple[str, str, str, float]]:
        return {
            (
                e.from_qualified_name.split("::")[1],
                e.to_qualified_name.split("::")[1],
                e.edge_kind,
                e.confidence,
            )
            for e in bind_sites(pf, reference_sites(pf), table, SITE_RULES)
        }

    assert edges(schema) == {
        ("Customer", "Node", "inherits", 1.0),
        ("Customer", "Order", "uses_type", 1.0),
        ("Customer", "Tier", "uses_type", 1.0),
        ("Order", "Node", "inherits", 1.0),
        ("Order", "Moment", "uses_type", 1.0),
        ("Order", "Customer", "uses_type", 1.0),
        ("Query.customer", "Customer", "uses_type", 1.0),
        ("OrderFilter", "Moment", "uses_type", 1.0),
        ("Query.search", "Hit", "uses_type", 1.0),
    }, "`Entity` and `OrderState` are defined nowhere, so they bind to nothing"
    assert edges(billing) == {
        ("Query.invoices", "Invoice", "uses_type", 1.0),
        ("Invoice", "Order", "uses_type", 0.6),
        ("Invoice", "Moment", "uses_type", 0.6),
    }


def test_operations_fragments_and_directive_definitions_define_nothing() -> None:
    src = b"""query Me($id: ID!) @cached { customer(id: $id) { id ...Parts } }
fragment Parts on Customer { tier orders { id } }
{ customer(id: "1") { id } }
mutation { reset }
directive @key(fields: String!) repeatable on OBJECT | INTERFACE
type After { id: ID! }
"""
    assert set(_symbols(src)) == {"After"}


def test_graphql_files_are_this_lane_not_text() -> None:
    assert detect_by_extension("api/schema.graphql") == "graphql"
    assert detect_by_extension("api/schema.gql") == "graphql"


def test_chunks_carry_each_type_with_its_comments() -> None:
    pf = parse_graphql("shop/schema.graphql", SCHEMA)
    chunks = span_chunks(pf, 6000, **CHUNK_OPTIONS)
    units = {
        c.scope.split("::")[1]: (c.start_line, c.end_line) for c in chunks if c.kind == "function"
    }
    assert units["Node"] == (3, 8), "the block description rides its type"
    assert units["Customer"] == (10, 16)
    assert units["Query"] == (24, 27), "the root fields ride their type's chunk"
    assert [c for c in chunks if c.kind == "file_header"][0].start_line == 1


@pytest.mark.parametrize(
    "src",
    [
        b'"""unclosed\ntype A { b: B }\n',
        b'type A { b(c: "unclosed): B }\ntype D { e: E }\n',
        b"type A { b: [[[B }\n",
        b"type A implements\n",
        b"schema { query: }\n",
        b"extend\n",
    ],
)
def test_broken_input_parses_without_error(src: bytes) -> None:
    parse_graphql("a.graphql", src)
    reference_sites(parse_graphql("a.graphql", src))


@pytest.mark.parametrize(
    "unit",
    [
        "type T { a: A b(c: C): [D!]! }\n",
        '"d" scalar S\n',
        "type T implements A & B & C\n",
        "schema ",
        '"""',
        "{ ( [ ",
    ],
)
def test_a_file_of_many_units_parses_in_linear_time(unit: str) -> None:
    def cost(n: int) -> float:
        src = (unit * n).encode()
        t0 = time.perf_counter()
        reference_sites(parse_graphql("a.graphql", src))
        return time.perf_counter() - t0

    assert_linear(cost, 2_000, 8_000)
