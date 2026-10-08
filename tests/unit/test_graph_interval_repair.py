from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app.projections import (
    _repair_contradictory_edges,
    _repair_contradictory_triples,
    write_entity_edges,
)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.execute_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def execute(self, sql: str, params: dict[str, Any] | None = None):
        self.execute_calls.append((sql, params or {}))

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ):
        self.insert_calls.append((table, rows, settings or {}))


def _bulk_input() -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="clickhouse",
        namespace="agent-memory",
        agent="validator",
        entries=[
            _BulkSetEntry(
                memory_key="user_employer",
                value="User works at ClickHouse.",
                relations=[("user", "works_at", "clickhouse")],
                valid_at=datetime(2025, 1, 1),
            )
        ],
    )


@pytest.mark.asyncio
async def test_contradictory_triple_repair_returns_interval_for_edge_close() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
            }
        ]
    )

    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
    )

    assert len(repairs) == 1
    assert repairs[0].old_object == "acme"
    assert repairs[0].valid_from == datetime(2023, 1, 1)
    sql, params = ch.execute_calls[0]
    assert "ALTER TABLE loom.entity_triples" in sql
    assert "UPDATE valid_to = arrayElement({valid_tos:Array(DateTime)}" in sql
    assert params["valid_tos"] == [datetime(2025, 1, 1)]
    assert params["keys"] == ["user\x1fworks_at\x1facme"]


@pytest.mark.asyncio
async def test_contradictory_edge_repair_closes_old_pair_only() -> None:
    ch = StubCH()
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
    )
    assert repairs == []

    from server.memory.domain.graph_quality import IntervalRepair

    repairs = [
        IntervalRepair(
            subject="user",
            relation="works_at",
            old_object="acme",
            new_object="clickhouse",
            valid_to=datetime(2025, 1, 1),
            valid_from=datetime(2023, 1, 1),
        )
    ]
    await _repair_contradictory_edges(_bulk_input(), repairs, ch=ch)

    sql, params = ch.execute_calls[-1]
    assert "ALTER TABLE loom.entity_edges" in sql
    # one UPDATE per write: the pair and its valid_from travel in the key
    import calendar

    vf = calendar.timegm(datetime(2023, 1, 1).timetuple())
    assert (
        "IN {keys:Array(String)}" in sql
        and "valid_from < arrayElement({valid_tos:Array(DateTime)}" in sql
    )
    assert params["keys"] == [f"acme\x1fuser\x1f{vf}"]
    assert params["valid_tos"] == [datetime(2025, 1, 1)]


@pytest.mark.asyncio
async def test_entity_edges_use_namespace_canonical_resolution() -> None:
    ch = StubCH()
    bulk = MemoryBulkSetInput(
        org="clickhouse",
        namespace="agent-memory",
        agent="validator",
        entries=[
            _BulkSetEntry(
                memory_key="northwind_private",
                value="Northwind AI inherited ClickGap context.",
                entities=["Northwind AI", "ClickGap"],
            )
        ],
    )

    await write_entity_edges(
        bulk,
        [type("Result", (), {"memory_key": "northwind_private"})()],
        ch=ch,
        entity_map={"northwind ai": "northwindai"},
    )

    edge_rows = ch.insert_calls[0][1]
    assert edge_rows[0]["entity_a"] == "clickgap"
    assert edge_rows[0]["entity_b"] == "northwindai"


def test_single_valued_gate_matches_stored_lemma_forms() -> None:
    """The write path stores LEMMATIZED verbs ("works_at" -> "work_at");
    the single-valued gate must match those stored forms, not just the
    surface forms listed in _SINGLE_VALUED_RELATIONS — a surface-only set
    would silently disable interval repair for every listed relation."""
    from server.memory.domain.graph_quality import is_single_valued_relation

    for stored in ("work_at", "live_in", "employ_by", "base_in"):
        assert is_single_valued_relation(stored), stored
    for surface in ("works_at", "lives_in", "employed_by", "based_in"):
        assert is_single_valued_relation(surface), surface
    assert not is_single_valued_relation("mentions")


def test_interval_repair_fires_for_stored_lemma_relation() -> None:
    """triple_interval_repairs receives triples exactly as stored (lemma
    forms) — it must produce a closure for them."""
    from datetime import datetime

    from server.memory.domain.graph_quality import triple_interval_repairs

    repairs = triple_interval_repairs(
        existing=[
            {
                "subject": "user",
                "relation": "work_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
                "valid_to": None,
            }
        ],
        new_subject="user",
        new_relation="work_at",
        new_object="clickhouse",
        new_valid_at=datetime(2024, 1, 1),
    )
    assert len(repairs) == 1
    assert repairs[0].old_object == "acme"
    assert repairs[0].valid_to == datetime(2024, 1, 1)


# ---------------------------------------------------------------------------
# Declared cardinality, undated facts, ties, invalid_at.
# ---------------------------------------------------------------------------


@pytest.fixture
def _declared(monkeypatch: pytest.MonkeyPatch):
    import server.identity.app.namespace_ontology as onto

    holder: dict[str, dict[str, str]] = {"declared": {}}

    async def _load(org: str, namespace: str) -> dict[str, str]:
        return holder["declared"]

    monkeypatch.setattr(onto, "load_relation_cardinality", _load)
    return holder


@pytest.mark.asyncio
async def test_repair_stamps_invalid_at_and_closes_equal_date_tie(_declared) -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2025, 1, 1),
                "valid_to": None,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2025, 1, 1),
            }
        ],
        ch=ch,
    )
    assert [r.old_object for r in repairs] == ["acme"]
    sql, params = ch.execute_calls[0]
    assert "invalid_at = arrayElement({invalid_ats:Array(DateTime)}" in sql
    assert isinstance(params["invalid_ats"][0], datetime)
    assert params["valid_tos"] == [datetime(2025, 1, 1)]


@pytest.mark.asyncio
async def test_undated_single_valued_fact_orders_by_arrival(_declared) -> None:
    """An undated new `status` value still closes the open one: the repair
    orders by arrival instead of skipping it."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "task-1",
                "relation": "status",
                "object": "in progress",
                "valid_from": datetime(2026, 9, 3),
                "valid_to": None,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [{"subject": "task-1", "relation": "status", "object": "live", "valid_from": None}],
        ch=ch,
    )
    assert [r.old_object for r in repairs] == ["in progress"]
    # The SELECT still scoped to the undated triple's subject + relation.
    _, qparams = ch.query_all_calls[0]
    assert qparams["subjects"] == ["task-1"] and qparams["relations"] == ["status"]
    assert isinstance(repairs[0].valid_to, datetime)


@pytest.mark.asyncio
async def test_declared_cardinality_wins_over_seed_list(_declared) -> None:
    ch = StubCH()
    existing = [
        {
            "subject": "user",
            "relation": "mentors",
            "object": "alice",
            "valid_from": datetime(2024, 1, 1),
            "valid_to": None,
        },
    ]
    # Undeclared "mentors" is multi-valued: nothing closes.
    ch.query_all_returns.append(list(existing))
    new = [
        {
            "subject": "user",
            "relation": "mentors",
            "object": "bob",
            "valid_from": datetime(2025, 1, 1),
        }
    ]
    assert await _repair_contradictory_triples(_bulk_input(), new, ch=ch) == []
    # Declared "one": alice closes.
    _declared["declared"] = {"mentor": "one"}
    ch.query_all_returns.append(list(existing))
    repairs = await _repair_contradictory_triples(_bulk_input(), new, ch=ch)
    assert [r.old_object for r in repairs] == ["alice"]
    # Declared "many" on a seed-list verb: nothing closes.
    _declared["declared"] = {"work_at": "many"}
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2023, 1, 1),
                "valid_to": None,
            }
        ]
    )
    assert (
        await _repair_contradictory_triples(
            _bulk_input(),
            [
                {
                    "subject": "user",
                    "relation": "works_at",
                    "object": "clickhouse",
                    "valid_from": datetime(2025, 1, 1),
                }
            ],
            ch=ch,
        )
        == []
    )


@pytest.mark.asyncio
async def test_last_value_in_a_batch_survives(_declared) -> None:
    """One write carrying the whole employment history must leave exactly the
    newest value open, not close every row against every other."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
                "valid_to": None,
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "globex",
                "valid_from": datetime(2022, 6, 1),
                "valid_to": None,
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2024, 3, 1),
                "valid_to": None,
            },
        ]
    )
    repairs = await _repair_contradictory_triples(
        _bulk_input(),
        [
            {
                "subject": "user",
                "relation": "works_at",
                "object": "acme",
                "valid_from": datetime(2020, 1, 1),
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "globex",
                "valid_from": datetime(2022, 6, 1),
            },
            {
                "subject": "user",
                "relation": "works_at",
                "object": "clickhouse",
                "valid_from": datetime(2024, 3, 1),
            },
        ],
        ch=ch,
    )
    closed = {(r.old_object, r.valid_to) for r in repairs}
    assert closed == {("acme", datetime(2022, 6, 1)), ("globex", datetime(2024, 3, 1))}


@pytest.mark.asyncio
async def test_many_repairs_fold_into_one_mutation_per_table(monkeypatch) -> None:
    """A write closes every interval it supersedes with one
    UPDATE per table (two when only some repairs carry a transaction time)."""
    from server.memory.app import projections as proj
    from server.memory.domain.graph_quality import IntervalRepair

    ch = StubCH()
    repairs = [
        IntervalRepair(
            subject="user",
            relation="works_at",
            old_object="acme",
            new_object="globex",
            valid_to=datetime(2024, 1, 1),
            valid_from=datetime(2023, 1, 1),
            invalid_at=datetime(2026, 9, 12),
        ),
        IntervalRepair(
            subject="user",
            relation="lives_in",
            old_object="paris",
            new_object="rome",
            valid_to=datetime(2025, 6, 1),
            valid_from=None,
            invalid_at=datetime(2026, 9, 12),
        ),
        IntervalRepair(
            subject="user",
            relation="drives",
            old_object="volvo",
            new_object="saab",
            valid_to=datetime(2025, 7, 1),
            valid_from=None,
            invalid_at=None,
        ),
    ]
    n = await proj.close_triples(
        ch,
        org="o",
        namespace="ns",
        closes=[(r.subject, r.relation, r.old_object, r.valid_to, r.invalid_at) for r in repairs],
    )
    assert n == 2 and len(ch.execute_calls) == 2
    with_inv, without = ch.execute_calls
    assert with_inv[1]["keys"] == ["user\x1fworks_at\x1facme", "user\x1flives_in\x1fparis"]
    assert with_inv[1]["valid_tos"] == [datetime(2024, 1, 1), datetime(2025, 6, 1)]
    assert with_inv[1]["invalid_ats"] == [datetime(2026, 9, 12)] * 2
    assert "invalid_at" not in without[0] and without[1]["keys"] == ["user\x1fdrives\x1fvolvo"]
    # the row's own close is looked up by position; the IN keeps the mutation to the keyed rows
    assert (
        "arrayElement({valid_tos:Array(DateTime)}, indexOf({keys:Array(String)}, concat(subject, char(31), relation, char(31), object)))"
        in with_inv[0]
    )
    assert "AND valid_to IS NULL" in with_inv[0]

    ch2 = StubCH()
    await _repair_contradictory_edges(_bulk_input(), repairs, ch=ch2)
    assert len(ch2.execute_calls) == 2
    keys = ch2.execute_calls[0][1]["keys"]
    assert len(keys) == 2 and keys[1] == "paris\x1fuser\x1f" or keys[1] == "user\x1fparis\x1f"
    assert not await proj.close_triples(ch, org="o", namespace="ns", closes=[])
