"""Query-shape routing + token budgeting contracts.

GATE: a symbol-shaped query must never reach the embedding path, and a
budgeted response must never contain a truncated unit body — only whole
bodies or elided (signature + pointer) entries.
"""

from __future__ import annotations

from server.code.app.search import classify_query_shape
from server.code.domain.units import CodeUnit, budget_units

# ---- shape classifier -------------------------------------------------------


def test_qualified_names_are_symbol_shaped() -> None:
    assert classify_query_shape("DB::IColumn::insert") == "symbol"
    assert classify_query_shape("IColumn::insert") == "symbol"
    assert classify_query_shape("~IColumn") == "hybrid"  # bare destructor: rare, hybrid ok
    assert classify_query_shape("DB::~IColumn") == "symbol"


def test_camel_and_snake_identifiers_are_symbol_shaped() -> None:
    assert classify_query_shape("parseQuotaKey") == "symbol"
    assert classify_query_shape("quota_key_parser") == "symbol"


def test_natural_language_is_hybrid() -> None:
    assert classify_query_shape("how does quota enforcement work") == "hybrid"
    assert classify_query_shape("where is backpressure handled") == "hybrid"
    # single lowercase word: ambiguous, must NOT skip semantic search
    assert classify_query_shape("backpressure") == "hybrid"
    assert classify_query_shape("insert") == "hybrid"


def test_file_pointers_are_symbol_shaped() -> None:
    assert classify_query_shape("src/Columns/IColumn.h") == "symbol"
    assert classify_query_shape("src/Columns/IColumn.h:120") == "symbol"


def test_screaming_case_is_hybrid() -> None:
    # ALL-CAPS single words are often error tokens/macros people describe
    # conceptually; a symbol miss would fall through anyway, but the default
    # must be hybrid per the false-symbol-is-worse rule.
    assert classify_query_shape("LOGICAL_ERROR") == "symbol"  # snake -> symbol
    assert classify_query_shape("TIMEOUT") == "hybrid"


# ---- token budgeting --------------------------------------------------------


def _unit(i: int, body_chars: int) -> CodeUnit:
    return CodeUnit(
        unit_id=f"u{i}",
        path=f"src/f{i}.cpp",
        start_line=1,
        end_line=10,
        symbol=f"DB::f{i}",
        kind="function",
        signature="(int)",
        body="x" * body_chars,
        score=1.0 - i * 0.1,
    )


def test_budget_keeps_whole_bodies_or_elides() -> None:
    units = [_unit(i, 2000) for i in range(10)]
    out, used = budget_units(units, token_budget=1200)
    assert out, "budget must fit at least one unit"
    full = [u for u in out if not u.elided]
    elided = [u for u in out if u.elided]
    assert full, "first units fit whole"
    assert all(len(u.body) == 2000 for u in full), "bodies are never truncated"
    assert all(u.body == "" for u in elided), "elided units carry no body"
    assert used <= 1200


def test_budget_never_reorders() -> None:
    units = [_unit(i, 100) for i in range(5)]
    out, _ = budget_units(units, token_budget=10000)
    assert [u.unit_id for u in out] == [f"u{i}" for i in range(5)]


def test_elided_unit_keeps_pointer_fields() -> None:
    units = [_unit(0, 100000)]
    out, _ = budget_units(units, token_budget=300)
    assert out and out[0].elided
    assert out[0].path and out[0].signature and out[0].start_line


def test_budget_represents_every_slot_before_fattening_bodies() -> None:
    # Heads are reserved first so fat top bodies cannot exhaust the budget and
    # drop a lower-ranked correct unit for want of its ~40-token head: all units
    # appear, bodies fill what remains by rank.
    units = [
        CodeUnit(
            path=f"src/F{i}.cpp",
            symbol=f"S{i}",
            signature="void f()",
            body="x" * 4000,
            score=1.0 - i * 0.01,
        )
        for i in range(10)
    ]
    out, used = budget_units(units, 3000)
    assert len(out) == 10  # every requested slot represented
    assert used <= 3000
    assert [u.path for u in out] == [u.path for u in units]  # order kept
    assert any(not u.elided for u in out)  # top bodies still land whole
    assert all(u.body == "" for u in out if u.elided)


def test_budget_bodyless_units_are_not_marked_elided() -> None:
    u = CodeUnit(path="src/A.cpp", symbol="S", signature="void f()", body="")
    out, _ = budget_units([u], 3000)
    assert out[0].elided is False


def test_python_dotted_names_are_symbol_shaped() -> None:
    from server.code.app.search import classify_query_shape

    assert classify_query_shape("ClickHouseCluster.wait_zookeeper_to_start") == "symbol"
    assert classify_query_shape("cluster.ClickHouseCluster.start") == "symbol"
    # prose with a dot stays hybrid (space rule)
    assert classify_query_shape("how does cluster.start work") == "hybrid"
