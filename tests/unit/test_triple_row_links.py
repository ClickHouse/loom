"""The triple writer links each triple a row's value stated to that row, and no
facet edge, which is the agent's and shared by every row that names it."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app.projections import write_entity_triples


class _CH:
    def __init__(self) -> None:
        self.inserts: dict[str, list[dict[str, Any]]] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts[table] = rows

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return []


async def test_a_row_links_its_stated_triples_and_no_facet() -> None:
    value = "My favourite city is Paris"
    write = MemoryBulkSetInput(
        org="o",
        namespace="ns",
        agent="bot",
        entries=[
            {
                "memory_key": "user_city",
                "value": value,
                "relations": [("user", "favourite_city", "paris")],
                "topics": ["travel"],
            }
        ],
    )
    ch = _CH()
    result = _BulkSetResult(memory_key="user_city", version=1, memory_id="m1")
    await write_entity_triples(write, [result], ch=ch, entity_map={})
    triples = {(t["subject"], t["object"]) for t in ch.inserts["loom.entity_triples"]}
    assert ("bot", "travel") in triples  # the facet edge is written
    links = ch.inserts["loom.entity_triple_rows"]
    assert [(r["memory_id"], r["subject"], r["object"]) for r in links] == [("m1", "user", "paris")]
    assert links[0]["content_sha256"] == sha256_value(value)


async def test_an_entry_that_wrote_no_row_links_nothing() -> None:
    write = MemoryBulkSetInput(
        org="o",
        namespace="ns",
        agent="bot",
        entries=[{"memory_key": "k", "value": "v", "relations": [("user", "likes", "tea")]}],
    )
    ch = _CH()
    await write_entity_triples(
        write, [_BulkSetResult(memory_key="k", version=0)], ch=ch, entity_map={}
    )
    assert "loom.entity_triple_rows" not in ch.inserts


def test_names_are_matched_in_the_decoded_value() -> None:
    import json

    from server.memory.app.entity_projections import _text
    from server.memory.domain.triples_follow_rows import still_states

    # Stored with ensure_ascii, "Zürich" is `Zürich` until decoded.
    stored = json.dumps("I currently live in Zürich")
    assert not still_states(stored, ("user", "live_in", "zürich"))
    assert still_states(_text(stored), ("user", "live_in", "zürich"))
    assert _text(json.dumps({"city": "Zürich"})) == "city Zürich"


async def test_a_row_link_lands_before_its_triple() -> None:
    # A follow closing the triple meanwhile finds the link once the triple is in
    # (`reopen_held`), so the link is never the later of the two.
    import asyncio

    class _OrderedCH(_CH):
        def __init__(self) -> None:
            super().__init__()
            self.events: list[tuple[str, str]] = []

        async def insert(self, table, rows, settings=None):
            self.events.append(("start", table))
            await asyncio.sleep(0)
            self.events.append(("end", table))

    write = MemoryBulkSetInput(
        org="o",
        namespace="ns",
        agent="bot",
        entries=[{"memory_key": "k", "value": "v", "relations": [("user", "likes", "tea")]}],
    )
    ch = _OrderedCH()
    result = _BulkSetResult(memory_key="k", version=1, memory_id="m1")
    await write_entity_triples(write, [result], ch=ch, entity_map={})
    events = ch.events
    assert events.index(("end", "loom.entity_triple_rows")) < events.index(
        ("start", "loom.entity_triples")
    )


@pytest.mark.parametrize(
    ("extra", "written"),
    [
        ({}, True),
        ({"sensitivity": "high"}, False),
        ({"valid_to": datetime(2020, 1, 1)}, False),
        ({"expires_at": datetime(2020, 1, 1)}, False),
        ({"expires_at": datetime(2020, 1, 1, tzinfo=UTC)}, False),
        ({"valid_to": datetime(2999, 1, 1)}, True),
    ],
)
async def test_a_point_write_states_relations_only_for_a_row_that_may_hold_them(
    monkeypatch, extra, written
) -> None:
    # The rule `GRAPH_HOLDER_SQL` keeps for the rows a fact stays open for: a row
    # already expired or past its validity holds none, as a 'high' row holds none.
    from server.memory.app import entity_projections as ep

    seen: list = []

    async def write(row, value, relations, *, op, ch):
        seen.append(relations)

    async def reconcile(input, ids, *, reopen, op, ch):
        pass

    monkeypatch.setattr(ep, "_write_stated_relations", write)
    monkeypatch.setattr(ep, "reconcile_row_facts", reconcile)
    row = {"org": "o", "namespace": "n", "agent": "a", "memory_id": "m1", **extra}
    await ep.project_stated_relations(
        row, "I live in Rome", [("user", "live_in", "rome")], revised=True, op="o", ch=None
    )
    assert bool(seen) is written


async def test_a_point_write_reconciles_only_a_row_it_revised(monkeypatch) -> None:
    # A brand-new row held no facts before: there is nothing to reconcile, and the
    # sequentially consistent read it costs is skipped.
    from server.memory.app import entity_projections as ep

    reconciled: list = []

    async def reconcile(input, ids, *, reopen, op, ch):
        reconciled.append(ids)

    monkeypatch.setattr(ep, "reconcile_row_facts", reconcile)
    row = {"org": "o", "namespace": "n", "agent": "a", "memory_id": "m1"}
    await ep.project_stated_relations(row, "I live in Rome", [], revised=False, op="o", ch=None)
    assert reconciled == []
    await ep.project_stated_relations(row, "I live in Rome", [], revised=True, op="o", ch=None)
    assert reconciled == [["m1"]]


async def test_a_bulk_write_reconciles_only_the_rows_it_revised(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    for name in ("write_entity_vectors", "write_entity_edges", "write_entity_triples",
                 "_write_derived_facts", "_bulk_settle_verdicts", "_bulk_audit"):  # fmt: skip
        monkeypatch.setattr(bulk_set, name, nothing)
    reconciled: list = []

    async def reconcile(input, ids, *, reopen, op, ch):
        reconciled.append(ids)

    monkeypatch.setattr(bulk_set, "reconcile_row_facts", reconcile)
    write = MemoryBulkSetInput(
        org="o",
        namespace="n",
        agent="a",
        entries=[{"memory_key": "old", "value": "v1"}, {"memory_key": "new", "value": "v2"}],
    )
    state = _BulkWriteState(existing_ids={"old": "id-old"})
    state.results = [
        _BulkSetResult(memory_key="old", version=2, memory_id="id-old"),
        _BulkSetResult(memory_key="new", version=1, memory_id="id-new"),
    ]
    await bulk_set._bulk_run_projections(None, write, state, None, None, {}, {})
    assert reconciled == [["id-old"]]


async def test_an_undo_weighs_what_is_open_with_pending_closes_applied(monkeypatch) -> None:
    # A retirement's closes are mutations: the Undo's two weight reads apply them on
    # the fly instead of waiting for them, and its markers keep a stated end still
    # to come.
    from types import SimpleNamespace

    from server.memory.app import entity_projections as ep

    restored = datetime(2026, 10, 15, 9, 0, 0, 123456)
    until = datetime(2026, 12, 1)

    class _WeighingCH(_CH):
        def __init__(self) -> None:
            super().__init__()
            self.reads: list[str] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.reads.append(sql)
            if "FROM loom.entity_triples" in sql:
                return [{"subject": "user", "relation": "live_in", "object": "london",
                         "confidence": 1.0, "provenance": "extracted", "ends": until,
                         "restored": 0, "weighted": 1}]  # fmt: skip
            return [{"entity_a": "london", "entity_b": "user", "ends": until,
                     "restored": 0, "weighted": 1}]  # fmt: skip

    async def held(*a: Any, **k: Any) -> list:
        return [("user", "live_in", "london")]

    repaired: list = []

    async def repair(input: Any, triples: list, *, ch: Any, at: datetime) -> list:
        repaired.append((triples, at))
        return []

    monkeypatch.setattr(ep, "_linked_triples", held)
    monkeypatch.setattr(ep, "_stated_by_live_rows", held)
    monkeypatch.setattr(ep, "repair_intervals", repair)
    ch = _WeighingCH()
    scope = SimpleNamespace(org="o", namespace="ns")
    await ep.reopen_held_facts(scope, ["m1"], ch=ch, restored_at=restored)
    assert len(ch.reads) == 2
    assert all("apply_mutations_on_fly = 1" in sql for sql in ch.reads)
    assert not any("system.mutations" in sql for sql in ch.reads)
    (marker,) = ch.inserts["loom.entity_triples"]
    assert (marker["seen_at"], marker["valid_from"], marker["valid_to"]) == (
        restored,
        restored.replace(microsecond=0),
        until,
    )
    assert marker["adapter_name"] == "row-restore" and marker["co_occur"] == 0
    (edge,) = ch.inserts["loom.entity_edges"]
    assert edge["valid_to"] == until
    # The repair runs the normal rule, as a value seen at the restore.
    ((restated, at),) = repaired
    assert at == restored and restated[0]["seen_at"] == restored
