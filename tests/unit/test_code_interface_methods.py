"""An interface's method signatures are body-less function
units, so typed receivers bind to them and implementations override them."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.edges_treesitter import extract_edges_and_misses

SRC = (
    b"export interface Repo {\n  fetchUser(id: string): User\n  readonly name: string\n  persist?(u: User): Promise<void>\n}\n"
    b"export class PrismaRepo implements Repo {\n  fetchUser(id: string) { return db.user(id) }\n  persist(u: User) { return Promise.resolve() }\n}\n"
    b"export class Svc {\n  constructor(private repo: Repo) {}\n  run(id: string) { return this.repo.fetchUser(id) }\n}\n"
)


def test_method_signatures_are_units_under_the_interface() -> None:
    pf = parse_file("src/repo.ts", SRC)
    rows = [
        (s.kind, s.qualified_name.split("::", 1)[1], s.has_body, s.signature)
        for s in pf.symbols
        if "Repo::" in s.qualified_name and "PrismaRepo" not in s.qualified_name
    ]
    assert rows == [
        ("function", "Repo::fetchUser", False, "(id: string): User"),
        ("function", "Repo::persist", False, "(u: User): Promise<void>"),
    ]
    # the property signature `name` is not a unit
    assert not any(s.qualified_name.endswith("Repo::name") for s in pf.symbols)


def test_typed_receiver_binds_and_the_implementation_overrides() -> None:
    pf = parse_file("src/repo.ts", SRC)
    edges, misses = extract_edges_and_misses(pf, {})
    kinds = sorted(
        (
            e.edge_kind,
            e.from_qualified_name.split("::", 1)[1],
            e.to_qualified_name.split("::", 1)[1],
        )
        for e in edges
        if e.edge_kind in ("calls", "overrides", "inherits")
    )
    assert ("calls", "Svc::run", "Repo::fetchUser") in kinds
    assert ("overrides", "PrismaRepo::fetchUser", "Repo::fetchUser") in kinds
    assert ("overrides", "PrismaRepo::persist", "Repo::persist") in kinds
    assert ("inherits", "PrismaRepo", "Repo") in kinds


def test_an_awaited_call_types_its_receiver_by_the_unwrapped_promise() -> None:
    pf = parse_file(
        "src/x.ts",
        b"declare function getFoo(): Promise<Foo>\nexport async function run() {\n  const a = await getFoo()\n  a.doWork()\n  const p = getFoo()\n  p.doWork()\n}\n",
    )
    table = {
        "doWork": [
            ("c", "src.foo::Foo::doWork", "function"),
            ("c2", "src.bar::Bar::doWork", "function"),
        ],
        "getFoo": [("d", "src.x::getFoo", "function")],
    }
    edges, misses = extract_edges_and_misses(pf, table, callee_returns={"getFoo": "Promise<Foo>"})
    assert [
        (e.to_qualified_name, e.line)
        for e in edges
        if e.edge_kind == "calls" and "doWork" in e.to_qualified_name
    ] == [
        ("src.foo::Foo::doWork", 4),
    ]
    # the un-awaited promise is not a Foo: its call stays ambiguous, as before
    assert [(m.callee_name, m.line) for m in misses] == [("doWork", 6)]
