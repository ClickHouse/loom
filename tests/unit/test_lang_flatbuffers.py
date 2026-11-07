"""FlatBuffers schemas as tables / enums / unions / services."""

from __future__ import annotations

import time

from server.code.domain.extract.lang_flatbuffers import (
    fbs_chunks,
    fbs_external_edges,
    fbs_local_edges,
    fbs_units,
    parse_fbs,
)

SCHEMA = b"""include "header.fbs";
namespace ledger.proto.log;

enum RecordKind : ubyte { Data, Prepare, Learn }

/// One appended record.
table Record {
  header: ledger.proto.Header;
  lsn: uint64;
  kind: RecordKind;
  payload: [ubyte];
}

union Body { Record, Ack }

table Ack { lsn: uint64; }

rpc_service LogStore {
  Append(Record): Ack;
}

root_type Record;
file_identifier "LEDG";
"""


def test_symbols_and_facets() -> None:
    pf = parse_fbs("src/proto/log.fbs", SCHEMA)
    by = {s.name: s for s in pf.symbols}
    assert (
        by["Record"].kind == "struct"
        and by["Record"].signature == "table"
        and by["Record"].qualified_name == "ledger::proto::log::Record"
    )
    assert by["Record"].doc_comment == "One appended record."
    assert by["RecordKind"].kind == "enum" and by["RecordKind"].signature == "enum : ubyte"
    assert by["Body"].kind == "union" and by["LogStore"].kind == "service"
    assert (
        by["Append"].kind == "function"
        and by["Append"].qualified_name == "ledger::proto::log::LogStore::Append"
    )
    assert pf.includes == ["header.fbs"]
    u = fbs_units("src/proto/log.fbs", SCHEMA.decode())
    assert [(f.kind, f.name) for f in u.facets] == [
        ("root_type", "Record"),
        ("file_identifier", "LEDG"),
    ]


def test_edges_local_and_external() -> None:
    pf = parse_fbs("src/proto/log.fbs", SCHEMA)
    local, external = fbs_local_edges(pf)
    pairs = {
        (e.from_qualified_name.split("::")[-1], e.to_qualified_name.split("::")[-1]) for e in local
    }
    assert {
        ("Record", "RecordKind"),
        ("Body", "Record"),
        ("Body", "Ack"),
        ("Append", "Record"),
        ("Append", "Ack"),
    } <= pairs
    assert all(e.edge_kind == "uses_type" for e in local)
    assert [(f.split("::")[-1], n) for f, n, _l in external] == [("Record", "Header")], (
        "a scalar is never a reference; the included type waits for the table"
    )
    ext = fbs_external_edges(pf, external, {"Header": [("h1", "ledger::proto::Header", "struct")]})
    assert [(e.to_qualified_name, e.confidence) for e in ext] == [("ledger::proto::Header", 0.9)]


def test_chunks_one_per_block_with_header() -> None:
    pf = parse_fbs("src/proto/log.fbs", SCHEMA)
    ch = fbs_chunks(pf)
    assert ch[0].kind == "file_header" and "include" in ch[0].content
    skel = [c for c in ch if c.kind == "class_skeleton"]
    assert [c.scope.split("::")[-1] for c in skel] == [
        "RecordKind",
        "Record",
        "Body",
        "Ack",
        "LogStore",
    ]
    svc = next(c for c in skel if c.scope.endswith("LogStore"))
    assert len(svc.symbol_ids) == 2, "the service chunk carries its method symbols too"
    n = SCHEMA.count(b"\n")
    seen = [0] * (n + 2)
    for c in ch:
        for ln in range(c.start_line, c.end_line + 1):
            seen[ln] += 1
    assert all(x <= 1 for x in seen)


def test_a_union_member_with_a_long_blank_run_is_read_in_linear_time() -> None:
    """A member that cannot match (`ab`, blanks, `_`) is refused after one pass over the
    blanks, and an aliased member still names its type."""
    blanks = " " * 300
    schema = f"union Body {{ Record, ab{blanks}_, Ack : Rec{blanks}=  2 , }}\n".encode()
    start = time.process_time()
    pf = parse_fbs("src/u.fbs", schema)
    local, external = fbs_local_edges(pf)
    assert time.process_time() - start < 0.5
    assert local == []
    assert sorted(name for _from, name, _line in external) == ["Rec", "Record"]


def test_a_block_header_with_a_long_blank_run_is_read_in_linear_time() -> None:
    """A header that never opens its block (`table A`, blanks, `x`) is refused after one
    pass over the blanks; headers with a base type and with attributes still open one,
    their groups read."""
    blanks = " " * 2_000
    schema = f"table A{blanks}x\nenum E : byte (bit_flags) {{ X = 1 }}\ntable T (x) {{}}\n".encode()
    start = time.process_time()
    pf = parse_fbs("src/b.fbs", schema)
    assert time.process_time() - start < 0.5
    assert {s.name: s.signature for s in pf.symbols} == {"E": "enum : byte", "T": "table (x)"}
