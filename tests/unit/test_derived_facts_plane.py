"""Unit tests for the multi-hop derived-facts plane.

CH-free: a fake ClickHouse client captures the SQL/params (read leg) and the
insert rows (write leg), so these lock in the load-bearing behavior without
needing a running ClickHouse.

Covers:
  * fetch_derived_aggregate (read leg) — canonical IN-list matching
    (incl. the canonicalized scalar fallback and the object substring→exact
    gate), aggregate result shaping, empty→None.
  * _write_derived_facts (write leg) — per-occurrence row build:
    category canonicalization, subject default, numeric coercion, occurred_at
    fallback, the per-memory cap, and the empty-derived no-op.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app import retrieval as ms
from server.memory.domain.search_parsing import expand_predicate_class

# ----------------------------------------------------------------------
# Read leg — fetch_derived_aggregate
# ----------------------------------------------------------------------


class _GrabCH:
    """Captures query_all calls; returns a scripted list of result sets."""

    def __init__(self, returns: list[list[dict[str, Any]]]) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._returns = list(returns)

    async def query_all(self, sql, params=None):
        self.calls.append((sql, params or {}))
        return self._returns.pop(0) if self._returns else []


async def _agg_where(**kw) -> tuple[str, dict[str, Any]]:
    """Run fetch_derived_aggregate with cnt=0 (short-circuits to None) and
    return the captured (agg_sql, params) so we can assert the WHERE shape.
    predicate is omitted so no verb-synonym LLM call fires."""
    ch = _GrabCH([[{"cnt": 0}]])
    out = await ms.fetch_derived_aggregate(ch, org="dev", namespace="ns", **kw)
    assert out is None  # cnt==0 path
    return ch.calls[0]


@pytest.mark.asyncio
async def test_scalar_category_fallback_is_canonicalized() -> None:
    """With no expansion supplied, the
    scalar category equality must compare the CANONICALIZED form, else an
    un-singularized query ('data breaches') misses the singularized write
    rows ('data breach')."""
    sql, params = await _agg_where(category="data breaches")
    assert "category = {category:String}" in sql
    assert params["category"] == "data breach"


@pytest.mark.asyncio
async def test_category_in_list_when_expanded() -> None:
    sql, params = await _agg_where(
        category="breach", category_canonical_expanded=["data breach", "breach"]
    )
    assert "category IN {categories_expanded:Array(String)}" in sql
    assert params["categories_expanded"] == ["data breach", "breach"]
    assert "category = {category:String}" not in sql


@pytest.mark.asyncio
async def test_object_self_form_only_keeps_substring() -> None:
    """Lookup always returns at least the
    self-form, so a len==1 object expansion must NOT flip the proven
    substring match to exact (which would drop multi-token objects)."""
    sql, _ = await _agg_where(object_like="helmet", object_canonical_expanded=["helmet"])
    assert "positionCaseInsensitive(object, {obj:String})" in sql
    assert "object IN" not in sql


@pytest.mark.asyncio
async def test_object_real_alias_uses_exact_in_list() -> None:
    sql, params = await _agg_where(
        object_like="helmet", object_canonical_expanded=["helmet", "bike helmet"]
    )
    assert "object IN {objects_expanded:Array(String)}" in sql
    assert params["objects_expanded"] == ["helmet", "bike helmet"]


@pytest.mark.asyncio
async def test_subject_scalar_fallback_canonicalized() -> None:
    sql, params = await _agg_where(subject="user")
    assert "subject = {subject:String}" in sql
    assert params["subject"] == "user"


@pytest.mark.asyncio
async def test_aggregate_result_shaping_count_sum_and_span() -> None:
    """Populated path: count/sum/avg pass through, and span_days is derived
    from min/max occurred_at."""
    agg_row = {
        "cnt": 3,
        "s": 540.0,
        "a": 180.0,
        "first_at": datetime(2023, 1, 1),
        "last_at": datetime(2023, 3, 2),  # 60 days later
    }
    items = [
        {
            "object": "trek bike",
            "numeric_value": 540.0,
            "unit": "usd",
            "occurred_at": datetime(2023, 1, 1),
        },
    ]
    ch = _GrabCH([[agg_row], items])
    out = await ms.fetch_derived_aggregate(ch, org="dev", namespace="ns", subject="user")
    assert out is not None
    assert out["count"] == 3
    assert out["sum"] == 540.0
    assert out["avg"] == 180.0
    assert round(out["span_days"]) == 60
    assert out["items"][0]["object"] == "trek bike"


@pytest.mark.asyncio
async def test_empty_match_returns_none() -> None:
    ch = _GrabCH([[{"cnt": 0}]])
    out = await ms.fetch_derived_aggregate(ch, org="dev", namespace="ns")
    assert out is None


# ----------------------------------------------------------------------
# Predicate-class expansion — spend/buy/pay must reach stored `acquire`
# ----------------------------------------------------------------------


def test_predicate_class_spend_reaches_acquire() -> None:
    """'total spent on collectibles' must find stored rows that carry the stem
    'acquir'. The deterministic class
    must bridge the expenditure/acquisition verbs both ways."""
    from server.memory.domain.search_text import canonicalize_verb

    cls = expand_predicate_class(canonicalize_verb("spend"))
    assert canonicalize_verb("acquire") in cls  # "acquir"
    assert canonicalize_verb("buy") in cls
    assert canonicalize_verb("purchase") in cls
    assert canonicalize_verb("pay") in cls
    # Symmetric: stored-side stems reach the query-side verbs too.
    assert canonicalize_verb("spend") in expand_predicate_class(canonicalize_verb("acquire"))


def test_predicate_class_visit_reaches_attend() -> None:
    """'how many times did I visit X' must find stored visit rows that carry
    'attend'. The
    visitation class must bridge query-side 'visit'/'go' to stored
    'attend'/'consult' without an LLM synonym round-trip."""
    from server.memory.domain.search_text import canonicalize_verb

    cls = expand_predicate_class(canonicalize_verb("visit"))
    assert canonicalize_verb("attend") in cls
    assert canonicalize_verb("consult") in cls
    assert canonicalize_verb("went") in cls  # irregular -> "go"
    # Symmetric: stored-side 'attend' reaches query-side 'visit'.
    assert canonicalize_verb("visit") in expand_predicate_class(canonicalize_verb("attend"))
    # The classes stay disjoint: visitation never bleeds into spend.
    assert canonicalize_verb("spend") not in cls


def test_predicate_class_unrelated_verb_expands_to_nothing() -> None:
    from server.memory.domain.search_text import canonicalize_verb

    assert expand_predicate_class(canonicalize_verb("dance")) == set()
    assert expand_predicate_class("") == set()


@pytest.mark.asyncio
async def test_aggregate_predicate_in_list_includes_class_verbs() -> None:
    """With LLM synonym expansion off, the IN-list must still carry the
    full predicate class, so a 'spend' query matches 'acquir' rows."""
    from server.memory.domain.search_text import canonicalize_verb

    ch = _GrabCH([[{"cnt": 0}]])
    out = await ms.fetch_derived_aggregate(
        ch, org="dev", namespace="ns", predicate="spent", expand_synonyms=False
    )
    assert out is None
    sql, params = ch.calls[0]
    assert "predicate IN {verbs:Array(String)}" in sql
    verbs = set(params["verbs"])
    assert canonicalize_verb("spend") in verbs
    assert canonicalize_verb("acquire") in verbs
    assert canonicalize_verb("buy") in verbs


# ----------------------------------------------------------------------
# Write leg — _write_derived_facts
# ----------------------------------------------------------------------


class _InsertCH:
    """Captures insert(table, rows, settings)."""

    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows, settings or {}))


def _input_with_derived(derived: list[dict[str, Any]]) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="dev",
        namespace="ns",
        agent="a",
        user_id="alice",
        session_id="s1",
        entries=[_BulkSetEntry(memory_key="k1", value="v", derived=derived)],
    )


async def _write_rows(derived: list[dict[str, Any]], **entry_over) -> list[dict[str, Any]]:
    from server.memory.app.extract.handler import _write_derived_facts

    inp = _input_with_derived(derived)
    if entry_over:
        inp.entries[0] = inp.entries[0].model_copy(update=entry_over)
    ch = _InsertCH()
    # entity_map={} keeps it CH-free (no resolve_entities_for_write round-trip)
    await _write_derived_facts(
        inp, [_BulkSetResult(memory_key="k1", version=0)], ch=ch, entity_map={}
    )
    if not ch.inserts:
        return []
    table, rows, _ = ch.inserts[0]
    assert table == "loom.derived_facts"
    return rows


@pytest.mark.asyncio
async def test_write_canonicalizes_category_and_predicate() -> None:
    rows = await _write_rows(
        [
            {
                "predicate": "bought",
                "object": "trek bike",
                "category": "Bikes",
                "numeric_value": "540",
                "unit": "USD",
                "occurred_at": "2023-05-01",
            },
        ]
    )
    assert len(rows) == 1
    r = rows[0]
    assert r["category"] == "bike"  # canonical_entity_text singularizes
    assert r["predicate"] == "buy"  # canonicalize_verb (irregular)
    assert r["subject"] == "user"  # default
    assert r["numeric_value"] == 540.0  # str coerced to float
    assert r["memory_key"] == "k1"
    assert r["user_id"] == "alice"


@pytest.mark.asyncio
async def test_write_bad_numeric_coerces_to_none() -> None:
    rows = await _write_rows(
        [
            {"predicate": "spend", "object": "gear", "numeric_value": "not-a-number"},
        ]
    )
    assert rows[0]["numeric_value"] is None


@pytest.mark.asyncio
async def test_write_occurred_at_falls_back_to_temporal_anchor() -> None:
    rows = await _write_rows(
        [{"predicate": "visit", "object": "clinic"}],  # no occurred_at on the operand
        temporal_anchor="2024-02-10",
    )
    assert rows[0]["occurred_at"] is not None
    assert str(rows[0]["occurred_at"]).startswith("2024-02-10")


@pytest.mark.asyncio
async def test_write_empty_derived_inserts_nothing() -> None:
    from server.memory.app.extract.handler import _write_derived_facts

    inp = _input_with_derived([])
    ch = _InsertCH()
    await _write_derived_facts(
        inp, [_BulkSetResult(memory_key="k1", version=0)], ch=ch, entity_map={}
    )
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_write_caps_at_the_extractor_cap_per_memory() -> None:
    from server.memory.app.extract import MAX_DERIVED_PER_FACT

    derived = [{"predicate": "buy", "object": f"item{i}", "numeric_value": i} for i in range(20)]
    rows = await _write_rows(derived)
    assert len(rows) == MAX_DERIVED_PER_FACT  # writer cap == extractor cap (12)


@pytest.mark.asyncio
async def test_write_drops_rows_with_too_short_predicate() -> None:
    # predicate canonicalizes to <2 chars or object empty → skipped
    rows = await _write_rows(
        [
            {"predicate": "a", "object": "x"},  # predicate too short
            {"predicate": "buy", "object": ""},  # object empty
            {"predicate": "buy", "object": "valid"},  # kept
        ]
    )
    assert len(rows) == 1
    assert rows[0]["object"] == "valid"


@pytest.mark.asyncio
async def test_write_keeps_every_operand_the_extractor_can_emit() -> None:
    """The extractor emits up to MAX_DERIVED_PER_FACT operands; the writer
    kept 8, so operands 9-12 of every fact vanished without a trace."""
    from server.memory.app.extract import MAX_DERIVED_PER_FACT

    assert MAX_DERIVED_PER_FACT == 12
    derived = [
        {"predicate": "buy", "object": f"item{i}", "numeric_value": i}
        for i in range(MAX_DERIVED_PER_FACT)
    ]
    rows = await _write_rows(derived)
    assert len(rows) == MAX_DERIVED_PER_FACT
    assert {r["object"] for r in rows} == {f"item{i}" for i in range(MAX_DERIVED_PER_FACT)}


@pytest.mark.asyncio
async def test_aggregate_is_bounded_by_the_read_identity() -> None:
    """A read-scoped caller's COUNT/SUM spans only its own rows: the same
    agent / user / session bounds the fact leg's `_build_where` applies."""
    sql, params = await _agg_where(
        agent="bot", user_id="u1", session_id="s1", session_ids=["s1", "s2"]
    )
    for clause in (
        "agent = {agent:String}",
        "user_id = {user_id:String}",
        "session_id = {session_id:String}",
        "session_id IN {session_ids:Array(String)}",
    ):
        assert clause in sql
    assert (params["agent"], params["user_id"], params["session_id"]) == ("bot", "u1", "s1")
    assert params["session_ids"] == ["s1", "s2"]


@pytest.mark.asyncio
async def test_aggregate_without_identity_reads_the_namespace() -> None:
    sql, params = await _agg_where(agent="*")
    assert "agent =" not in sql and "user_id" not in sql and "session_id" not in sql
    assert set(params) == {"org", "ns", "subject"}
