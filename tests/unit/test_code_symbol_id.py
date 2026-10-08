"""symbol_id contract: the ONE shared hash both tiers key rows with.

GATE: overloads must produce distinct ids; the same overload
must produce the same id regardless of whitespace/default-arg noise —
that is what lets a treesitter row and a scip row for the same symbol
agree, and what keeps `read(int)` and `read(String)` from collapsing
under the ReplacingMergeTree key.
"""

from __future__ import annotations

from server.code.domain.extract.treesitter import (
    file_symbol_id,
    normalize_signature,
    symbol_id,
)


def test_overloads_distinct() -> None:
    a = symbol_id("DB::IColumn::insert", "(const Field & x)")
    b = symbol_id("DB::IColumn::insert", "(T value)")
    assert a != b


def test_whitespace_insensitive() -> None:
    a = symbol_id("DB::f", "(const Field & x, size_t n)")
    b = symbol_id("DB::f", "( const  Field&x ,size_t   n )")
    assert a == b


def test_default_args_stripped() -> None:
    a = symbol_id("helper", "(int a, int b = 3)")
    b = symbol_id("helper", "(int a, int b)")
    assert a == b


def test_stable_and_hex128() -> None:
    sid = symbol_id("DB::IColumn::insert", "(const Field & x)")
    assert len(sid) == 32
    int(sid, 16)  # valid hex
    assert sid == symbol_id("DB::IColumn::insert", "(const Field & x)")


def test_normalize_signature_examples() -> None:
    assert normalize_signature("( int a , int b = f(x) )") == "(int a,int b)"
    assert normalize_signature("(const Field & x)") == "(const Field&x)"


def test_file_symbol_id_distinct_from_symbols() -> None:
    assert file_symbol_id("src/A.h") != file_symbol_id("src/B.h")
    assert len(file_symbol_id("src/A.h")) == 32
