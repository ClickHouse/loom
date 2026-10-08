"""Enum members and literal-valued const-object keys are
`field` symbols, and `Obj.member` reads bind to them as `reads` edges."""

from __future__ import annotations

from server.code.app.index.ingest_parse import parse_file
from server.code.domain.extract.lang_typescript_imports import import_bindings
from server.code.domain.extract.lang_typescript_refs import member_read_edges, member_read_sites

QUEUES = (
    b'export enum QueueName {\n  TraceUpsert = "trace-upsert",\n  TraceDelete = "trace-delete",\n}\n'
    b'export const Events = {\n  UserCreated: "user.created",\n  Retries: 3,\n  handler: () => 1,\n} as const\n'
    b"export function local() {\n  return QueueName.TraceDelete\n}\n"
)


def test_enum_members_and_const_keys_are_fields() -> None:
    pf = parse_file("src/queues.ts", QUEUES)
    fields = sorted((s.qualified_name, s.start_line) for s in pf.symbols if s.kind == "field")
    assert fields == [
        ("src.queues::Events::Retries", 7),
        ("src.queues::Events::UserCreated", 6),
        ("src.queues::QueueName::TraceDelete", 3),
        ("src.queues::QueueName::TraceUpsert", 2),
    ]
    # the function-valued key stays a function unit, not a field
    assert any(
        s.qualified_name == "src.queues::Events::handler" and s.kind == "function"
        for s in pf.symbols
    )


def test_same_file_read_binds_at_one() -> None:
    pf = parse_file("src/queues.ts", QUEUES)
    keep = {s.name for s in pf.symbols if s.kind in {"enum", "const"}}
    sites = member_read_sites(pf, keep)
    assert sites == [(11, "QueueName", "TraceDelete")]
    edges = member_read_edges(pf, sites, {}, {})
    assert [
        (e.edge_kind, e.from_qualified_name, e.to_qualified_name, e.confidence) for e in edges
    ] == [
        ("reads", "src.queues::local", "src.queues::QueueName::TraceDelete", 1.0),
    ]


def test_import_bound_read_binds_and_calls_or_assignments_are_no_sites() -> None:
    pf = parse_file(
        "src/worker.ts",
        b'import { QueueName, Events } from "./queues"\n'
        b"export function run(req: any) {\n"
        b"  const q = QueueName.TraceUpsert\n"
        b"  Events.UserCreated\n"
        b"  QueueName.toString()\n"
        b"  req.body\n"
        b"  Events.Retries = 4\n"
        b"  return q\n}\n",
    )
    bindings = import_bindings(pf)
    sites = member_read_sites(pf, set(bindings))
    assert sites == [(3, "QueueName", "TraceUpsert"), (4, "Events", "UserCreated")]
    table = {
        "QueueName": [("e1", "src.queues::QueueName", "enum")],
        "Events": [("c1", "src.queues::Events", "const")],
        "TraceUpsert": [("f1", "src.queues::QueueName::TraceUpsert", "field")],
        "UserCreated": [
            ("f2", "src.queues::Events::UserCreated", "field"),
            ("x9", "src.other::UserCreated", "function"),
        ],
    }
    edges = member_read_edges(pf, sites, table, bindings)
    assert [(e.to_symbol_id, e.line, e.confidence) for e in edges] == [
        ("f1", 3, 1.0),
        ("f2", 4, 1.0),
    ]
    # an import from a library module binds nothing
    lib = parse_file(
        "src/lib.ts", b'import { Level } from "pino"\nexport const l = () => Level.Info\n'
    )
    assert (
        member_read_edges(
            lib,
            member_read_sites(lib, {"Level"}),
            {"Level": [("z", "src.levels::Level", "enum")]},
            import_bindings(lib),
        )
        == []
    )
