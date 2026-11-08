"""TLA+ specs and TLC configs as symbols, chunks, edges, facets."""

from __future__ import annotations

import time

import pytest

from server.code.domain.extract.lang_tla import (
    is_tlc_config,
    parse_tla,
    parse_tlc_config,
    tla_chunks,
    tla_external_edges,
    tla_local_edges,
    tlc_edges,
    tlc_units,
)
from tests.unit.code_fixtures import assert_linear

SPEC = b"""---------------------------- MODULE Ledger ----------------------------
(* toy ledger *)
EXTENDS Integers, FiniteSets, Common

CONSTANTS MaxLsn
Lsns == 1..MaxLsn
VARIABLES
  log,       \\* records
  durable    \\* watermark
vars == <<log, durable>>
\\* stored on a quorum
Stored(l) == log[l].st = "quorum"
Init ==
  /\\ log = [l \\in Lsns |-> "free"]
  /\\ durable = 0
Advance ==
  /\\ Stored(durable + 1)
  /\\ durable' = durable + 1
Next == Advance \\/ Shared(durable)
Spec == Init /\\ [][Next]_vars
TypeOK == durable \\in 0..MaxLsn
===============================================================================
"""


def test_symbols_declarations_and_operators() -> None:
    pf = parse_tla("tla/Ledger.tla", SPEC)
    kinds = {}
    for s in pf.symbols:
        kinds[s.kind] = kinds.get(s.kind, 0) + 1
    assert kinds == {"module": 1, "constant": 1, "variable": 2, "operator": 8}
    ops = {s.name: s for s in pf.symbols if s.kind == "operator"}
    assert (
        ops["Stored"].signature == "Stored(l)"
        and ops["Stored"].doc_comment == "\\* stored on a quorum"
    )
    assert (ops["Init"].start_line, ops["Init"].end_line) == (13, 15), (
        "a definition runs to the next one"
    )
    assert {s.name for s in pf.symbols if s.kind == "variable"} == {"log", "durable"}
    assert pf.includes == ["Common.tla"], "standard modules are not files"
    assert pf.symbols[0].qualified_name == "Ledger" and ops["Init"].qualified_name == "Ledger::Init"


def test_chunks_cover_every_line_once_with_operators_as_functions() -> None:
    pf = parse_tla("tla/Ledger.tla", SPEC)
    ch = tla_chunks(pf)
    n = SPEC.count(b"\n")
    seen = [0] * (n + 2)
    for c in ch:
        for ln in range(c.start_line, c.end_line + 1):
            seen[ln] += 1
    assert all(x <= 1 for x in seen)
    assert ch[0].kind == "file_header" and ch[0].symbol_ids
    fn = [c for c in ch if c.kind == "function"]
    assert len(fn) == 8 and all(len(c.symbol_ids) == 1 for c in fn)
    stored = next(c for c in fn if c.scope == "Ledger::Stored")
    assert stored.content.startswith("\\* stored on a quorum\nStored(l)")


def test_edges_in_file_by_name_and_external_through_the_table() -> None:
    pf = parse_tla("tla/Ledger.tla", SPEC)
    local, external = tla_local_edges(pf)
    pairs = {(e.from_qualified_name, e.to_qualified_name) for e in local}
    assert ("Ledger::Advance", "Ledger::Stored") in pairs
    assert ("Ledger::Advance", "Ledger::durable") in pairs
    assert ("Ledger::Spec", "Ledger::Init") in pairs and ("Ledger::Spec", "Ledger::Next") in pairs
    assert all(e.edge_kind == "calls" for e in local)
    assert ("Ledger::Next", "Shared", 19) in external, (
        "an EXTENDS'd module's operator waits for the table"
    )
    table = {"Shared": [("sid1", "Common::Shared", "operator")]}
    ext = tla_external_edges(pf, external, table)
    assert [(e.from_qualified_name, e.to_qualified_name, e.confidence) for e in ext] == [
        ("Ledger::Next", "Common::Shared", 0.9)
    ]
    assert (
        tla_external_edges(
            pf,
            external,
            {"Shared": [("a", "X::Shared", "operator"), ("b", "Y::Shared", "operator")]},
        )
        == []
    )


def test_tlc_config_is_a_model_with_checks_facets_and_calls() -> None:
    cfg = b"CONSTANTS\n  MaxLsn = 3\nSPECIFICATION Spec\nINVARIANT TypeOK\nINVARIANTS Safe, Live\nPROPERTY Eventually\n"
    assert is_tlc_config("tla/Ledger.cfg", cfg) and not is_tlc_config(
        "tla/Ledger.cfg", b"[section]\nkey = 1\n"
    )
    assert not is_tlc_config("setup.txt", b"SPECIFICATION Spec\n"), "only a .cfg"
    pf = parse_tlc_config("tla/Ledger.cfg", cfg)
    assert (
        pf.includes == ["Ledger.tla"]
        and pf.symbols[0].kind == "model"
        and pf.symbols[0].qualified_name == "Ledger::TLC"
    )
    u = tlc_units("tla/Ledger.cfg", cfg)
    assert [(f.kind, f.name) for f in u.facets] == [
        ("checks", "Spec"),
        ("checks", "TypeOK"),
        ("checks", "Safe"),
        ("checks", "Live"),
        ("checks", "Eventually"),
    ]
    assert u.constants == [("MaxLsn", "3")]
    table = {
        "TypeOK": [("t1", "Ledger::TypeOK", "operator"), ("t2", "Other::TypeOK", "operator")],
        "Spec": [("s1", "Ledger::Spec", "operator")],
    }
    edges = tlc_edges(pf, table)
    assert {(e.to_qualified_name) for e in edges} == {"Ledger::TypeOK", "Ledger::Spec"}, (
        "the includer's module wins a tie"
    )


@pytest.mark.parametrize("unit", ["a == 1\nTHEOREM a\n", "*)\na == 1\n", "a == 1\n\\* c\n"])
def test_a_module_of_many_definitions_parses_and_chunks_in_linear_time(unit: str) -> None:
    def cost(n: int) -> float:
        opener = "(*\n" if unit.startswith("*)") else ""  # every `*)` below closes it
        src = ("---- MODULE M ----\nVARIABLES v\n" + opener + unit * n).encode()
        t0 = time.perf_counter()
        tla_chunks(parse_tla("M.tla", src))
        return time.perf_counter() - t0

    assert_linear(cost, 4_000, 16_000)


def test_a_model_line_of_a_long_space_run_parses_in_linear_time() -> None:
    def cost(n: int) -> float:
        src = ("INIT Init\nCONSTANTS\na" + " " * n + "b=1\n").encode()
        t0 = time.perf_counter()
        parse_tlc_config("MC.cfg", src)
        return time.perf_counter() - t0

    assert_linear(cost, 20_000, 80_000)


def test_a_doc_block_over_two_hundred_lines_is_kept_to_its_cap() -> None:
    body = "".join(f"  line {i}\n" for i in range(300))
    src = f"---- MODULE M ----\n(*\n{body}*)\nOp == 1\n====\n".encode()
    (op,) = [s for s in parse_tla("M.tla", src).symbols if s.name == "Op"]
    assert op.doc_comment.startswith("(*\nline 0\nline 1") and len(op.doc_comment) == 2000


def test_a_long_line_in_a_doc_block_over_many_definitions_is_read_once() -> None:
    def cost(opener: str) -> float:
        src = ("---- MODULE M ----\n" + opener + "\n" + "*)\na == 1\n" * 4_000).encode()
        t0 = time.perf_counter()
        parse_tla("M.tla", src)
        return time.perf_counter() - t0

    assert_linear(cost, "(*", "(*\n" + " " * 2_000_000)


def test_many_definitions_under_one_long_doc_block_cost_what_a_short_one_does() -> None:
    def cost(blank_lines: int) -> float:
        src = ("---- MODULE M ----\n(*\n" + "\n" * blank_lines + "A==*)\n" * 8_000).encode()
        t0 = time.perf_counter()
        parse_tla("M.tla", src)
        return time.perf_counter() - t0

    # the block is read once, so its 2,000 lines add little; read per definition they
    # cost about 4 times the rest of the parse
    assert_linear(cost, 1, 1_999, ratio=2.5)
