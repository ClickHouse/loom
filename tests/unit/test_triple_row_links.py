"""The triple writer links each triple a row's value stated to that row, and no
facet edge, which is the agent's and shared by every row that names it."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.ch_writes import batched_insert_settings
from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
from server.memory.app.projections import write_entity_triples

# What ClickHouse reads back as a fact's newest open row when it has none.
_NO_OPEN_ROW = (None, datetime(1970, 1, 1))


class _CH:
    def __init__(self) -> None:
        self.inserts: dict[str, list[dict[str, Any]]] = {}
        self.settings: dict[str, Any] = {}

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts[table] = rows
        self.settings[table] = settings

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

    from server.memory.app.graph_holders import as_text
    from server.memory.domain.triples_follow_rows import still_states

    # Stored with ensure_ascii, "Zürich" is `Zürich` until decoded.
    stored = json.dumps("I currently live in Zürich")
    assert not still_states(stored, ("user", "live_in", "zürich"))
    assert still_states(as_text(stored), ("user", "live_in", "zürich"))
    assert as_text(json.dumps({"city": "Zürich"})) == "city Zürich"


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
        ({"status": "conflicted"}, True),
        ({"status": "proposed"}, False),
        ({"tombstoned_at": datetime(2020, 1, 1)}, False),
    ],
)
async def test_a_point_write_states_relations_only_for_a_row_that_may_hold_them(
    monkeypatch, extra, written
) -> None:
    # The rule `GRAPH_HOLDER_SQL` keeps for the rows a fact stays open for: a row
    # already expired or past its validity holds none, as a 'high' row, a proposal or a
    # deleted row holds none.
    from server.memory.app import triple_projections

    seen: list = []

    async def write(row, value, relations, *, op, ch):
        seen.append(relations)

    async def reconcile(input, ids, *, reopen, op, ch):
        pass

    monkeypatch.setattr(triple_projections, "_write_stated_relations", write)
    monkeypatch.setattr(triple_projections, "reconcile_row_facts", reconcile)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "memory_id": "m1",
        "status": "active",
    }
    row.update(extra)
    await triple_projections.project_stated_relations(
        row, "I live in Rome", [("user", "live_in", "rome")], revised=True, op="o", ch=None
    )
    assert bool(seen) is written


async def test_a_point_write_reconciles_only_a_row_it_revised(monkeypatch) -> None:
    # A brand-new row held no facts before: there is nothing to reconcile, and the
    # sequentially consistent read it costs is skipped.
    from server.memory.app import triple_projections

    reconciled: list = []

    async def reconcile(input, ids, *, reopen, op, ch):
        reconciled.append(ids)

    monkeypatch.setattr(triple_projections, "reconcile_row_facts", reconcile)
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "memory_id": "m1"}
    await triple_projections.project_stated_relations(
        row, "I live in Rome", [], revised=False, op="o", ch=None
    )
    assert reconciled == []
    await triple_projections.project_stated_relations(
        row, "I live in Rome", [], revised=True, op="o", ch=None
    )
    assert reconciled == [["m1"]]


@pytest.mark.parametrize(
    ("held", "written", "reconciled"),
    [([False], False, False), ([True, False], True, True), ([True, True], True, False)],
    ids=["revised-before", "revised-while-writing", "still-held"],
)
async def test_a_first_write_states_relations_only_while_its_row_holds_the_value(
    monkeypatch, held, written, reconciled
) -> None:
    # The fence its edges have: a first write another write revised first states
    # nothing; one revised while it wrote closes what it wrote, as a revision would.
    from server.memory.app import graph_holders, triple_projections

    answers, seen, closed = list(held), [], []

    async def holding(org, namespace, versions, *, ch, holder, key):
        assert versions == [("m1", "sha")]
        # Narrowed to the row's own key, not the whole namespace.
        assert key == ("a", "u", "s", "k")
        return {"m1"} if answers.pop(0) else set()

    async def write(row, value, relations, *, op, ch):
        seen.append(relations)

    async def reconcile(input, ids, *, reopen, op, ch):
        closed.append(ids)

    monkeypatch.setattr(graph_holders, "rows_holding", holding)
    monkeypatch.setattr(triple_projections, "_write_stated_relations", write)
    monkeypatch.setattr(triple_projections, "reconcile_row_facts", reconcile)
    row = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "memory_id": "m1",
        "status": "active",
    }
    row.update(session_id="s", memory_key="k", content_sha256="sha")
    await triple_projections.project_stated_relations(
        row, "I live in Rome", [("user", "live_in", "rome")], revised=False, op="o", ch=None
    )
    assert bool(seen) is written and bool(closed) is reconciled and answers == []


async def test_a_merge_states_its_relations_without_reconciling_the_row(monkeypatch) -> None:
    # A merge leaves the row's value as it was: it dropped no fact, so the reads a
    # reconcile costs are skipped.
    from types import SimpleNamespace

    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import conflicts as cf
    from server.memory.app import set_commit

    mine = SimpleNamespace(
        memory_id="m1",
        memory_key="home",
        value="I live in Rome",
        agent="a",
        user_id="u1",
        session_id="-",
        valid_at=None,
        model_dump=lambda: {"memory_id": "m1"},
    )
    projected: list = []

    async def get_by_id(*a: Any, **k: Any) -> Any:
        return mine

    async def project(row: Any, value: Any, relations: list, *, revised: bool, **k: Any) -> None:
        projected.append(revised)

    async def reinforce(*a: Any, **k: Any) -> None:
        pass

    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(cf, "auto_reinforce_peer", reinforce)
    monkeypatch.setattr(set_commit, "record_drop", lambda *a, **k: None)
    monkeypatch.setattr(set_commit, "_peer_in_read_scope", lambda peer: True)
    monkeypatch.setattr(set_commit, "project_stated_relations", project)
    write = MemorySetInput(
        org="o",
        namespace="n",
        agent="a",
        user_id="u1",
        memory_key="city",
        value="I live in Rome",
        relations=[("user", "live_in", "rome")],
    )
    w = SimpleNamespace(input=write, retyped_from="", version=1, new_sha="h", memory_id="new")
    result = SimpleNamespace(existing_memory_id="m1", merge_scope="", peers=[], refines=[])
    out = await set_commit._set_merge(w, None, None, result, [])
    assert out.conflict_verdict == "merge"
    assert projected == [False]


async def test_a_bulk_write_reconciles_only_the_rows_it_revised(monkeypatch) -> None:
    from server.memory.app import bulk_set
    from server.memory.app.bulk_rows import _BulkWriteState

    async def nothing(*a: Any, **k: Any) -> None:
        return None

    for name in (
        "write_entity_vectors",
        "write_entity_edges",
        "write_entity_triples",
        "write_derived_facts",
        "_bulk_settle_verdicts",
        "_bulk_audit",
    ):
        monkeypatch.setattr(bulk_set, name, nothing)
    reconciled: list = []

    async def reconcile(input, ids, *, reopen, op, ch):
        reconciled.append(ids)

    closed: list = []

    async def end_overtaken(input, versions, *, op, ch, high):
        closed.extend(memory_id for memory_id, _, _ in versions)

    asked: list = []

    async def no_holders(org, namespace, versions, *, ch):
        asked.extend(memory_id for memory_id, _ in versions)
        return {}  # revised again meanwhile: nothing holds it

    monkeypatch.setattr(bulk_set, "edge_holders", no_holders)
    monkeypatch.setattr(bulk_set, "reconcile_row_facts", reconcile)
    monkeypatch.setattr(bulk_set, "end_overtaken_edges", end_overtaken)
    write = MemoryBulkSetInput(
        org="o",
        namespace="n",
        agent="a",
        entries=[
            {"memory_key": "old", "value": "v1", "entities": ["alice", "bob"]},
            {"memory_key": "new", "value": "v2", "entities": ["alice", "bob"]},
        ],
    )
    state = _BulkWriteState(existing_ids={"old": "id-old"})
    state.results = [
        _BulkSetResult(memory_key="old", version=2, memory_id="id-old"),
        _BulkSetResult(memory_key="new", version=1, memory_id="id-new"),
    ]
    await bulk_set._bulk_run_projections(None, write, state, None, None, {}, {})
    assert reconciled == [["id-old"]]
    # Only a revised row can have been revised again while its edges were written.
    assert asked == ["id-old"] and closed == ["id-old"]


async def test_an_undo_weighs_what_is_open_with_pending_closes_applied(monkeypatch) -> None:
    # A retirement's closes are mutations: the Undo's two weight reads apply them on
    # the fly instead of waiting for them, and its markers keep a stated end still
    # to come. Rome was stored before Berlin, so Berlin's marker is the later.
    from types import SimpleNamespace

    from server.memory.app import graph_holders, graph_undo

    restored = datetime(2026, 10, 15, 9, 0, 0, 123456)
    stored = datetime(2026, 10, 1)
    until = datetime(2026, 12, 1)

    class _WeighingCH(_CH):
        def __init__(self) -> None:
            super().__init__()
            self.reads: list[str] = []

        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            self.reads.append(sql)
            if "FROM loom.entity_triples" in sql:
                return [
                    {
                        "subject": "user",
                        "relation": "live_in",
                        "object": city,
                        "confidence": 1.0,
                        "provenance": "extracted",
                        "ends": until,
                        "seen": stored + timedelta(microseconds=i),
                        "start": None,
                        "open": 0,
                        "newest_open": _NO_OPEN_ROW,
                        "restored": 0,
                        "support": [(None, None)],
                    }
                    for city, i in (("berlin", 1), ("rome", 0))
                ]
            return [
                {
                    "entity_a": "berlin",
                    "entity_b": "user",
                    "ends": until,
                    "restored": 0,
                    "support": [(None, None)],
                }
            ]

    async def held(*a: Any, **k: Any) -> list:
        return [("user", "live_in", "berlin"), ("user", "live_in", "rome")]

    repaired: list = []

    async def repair(input: Any, triples: list, *, ch: Any, at: datetime, on_fly: bool) -> list:
        repaired.append((triples, at))
        return []

    monkeypatch.setattr(graph_undo, "linked_triples", held)
    monkeypatch.setattr(graph_holders, "stated_by_live_rows", _as_set(held))
    monkeypatch.setattr(graph_holders, "held_by_unlinked_rows", _none_held)
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    ch = _WeighingCH()
    scope = SimpleNamespace(org="o", namespace="ns")
    await graph_undo.reopen_held_facts(scope, ["m1"], ch=ch, restored_at=restored, restore_id="u1")
    assert len(ch.reads) == 2
    assert all("apply_mutations_on_fly = 1" in sql for sql in ch.reads)
    assert not any("system.mutations" in sql for sql in ch.reads)
    later = restored + timedelta(microseconds=1)
    markers = ch.inserts["loom.entity_triples"]
    assert [(m["object"], m["seen_at"], m["valid_from"], m["valid_to"]) for m in markers] == [
        ("rome", restored, restored.replace(microsecond=0), until),
        ("berlin", later, restored.replace(microsecond=0), until),
    ]
    assert all(m["adapter_name"] == "row-restore" and m["co_occur"] == 0 for m in markers)
    assert [e["valid_to"] for e in ch.inserts["loom.entity_edges"]] == [until]
    # The Undo markers are batched inserts.
    assert ch.settings == {
        "loom.entity_triples": batched_insert_settings(),
        "loom.entity_edges": batched_insert_settings(),
    }
    # The repair runs the normal rule, each value seen at its marker.
    ((restated, at),) = repaired
    assert at == restored
    assert [(t["object"], t["seen_at"]) for t in restated] == [
        ("rome", restored),
        ("berlin", later),
    ]


async def test_an_undo_refused_on_the_fly_writes_nothing_and_is_recorded(monkeypatch) -> None:
    # While another org's subquery mutation is pending, ClickHouse refuses every
    # on-the-fly read of the table: the Undo writes nothing to the graph, and the
    # failure is recorded, dated at the restore, for the repair worker to replay.
    from types import SimpleNamespace

    from server.memory.app import (
        edge_links,
        edge_repair,
        graph_holders,
        graph_undo,
        triple_projections,
    )

    refusal = (
        "Code: 36. DB::Exception: ALTER UPDATE/ALTER DELETE statement with subquery may"
        " be nondeterministic and cannot be applied on fly. (BAD_ARGUMENTS)"
    )

    class _RefusingCH(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "apply_mutations_on_fly" in sql:
                raise RuntimeError(refusal)
            if "FROM loom.entity_triples" in sql:
                return [
                    {
                        "subject": "user",
                        "relation": "live_in",
                        "object": "london",
                        "confidence": 1.0,
                        "provenance": "extracted",
                        "ends": None,
                        "seen": restored,
                        "start": None,
                        "open": 1,
                        "restored": 0,
                        "support": [(None, None)],
                    }
                ]
            return [
                {
                    "entity_a": "london",
                    "entity_b": "user",
                    "ends": None,
                    "restored": 0,
                    "support": [(None, None)],
                }
            ]

    async def held(*a: Any, **k: Any) -> list:
        return [("user", "live_in", "london")]

    async def repair(*a: Any, **k: Any) -> list:
        return []

    recorded: list = []

    async def record(input: Any, **kw: Any) -> None:
        recorded.append(kw)

    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    monkeypatch.setattr(graph_undo, "linked_triples", held)
    monkeypatch.setattr(graph_holders, "stated_by_live_rows", _as_set(held))
    monkeypatch.setattr(graph_holders, "held_by_unlinked_rows", _none_held)
    monkeypatch.setattr(edge_links, "record_input_projection_failure", record)
    monkeypatch.setattr(triple_projections, "record_input_projection_failure", record)
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    monkeypatch.setattr(graph_holders, "naive_utc_now", lambda: restored)
    monkeypatch.setattr(edge_repair, "naive_utc_now", lambda: restored)
    monkeypatch.setattr(triple_projections, "naive_utc_now", lambda: restored)
    ch = _RefusingCH()
    scope = SimpleNamespace(org="o", namespace="ns", agent="a")
    await triple_projections.reconcile_row_facts(
        scope, ["m1"], reopen=True, op="memory.restore", ch=ch, hashes={"m1": "h"}
    )
    assert ch.inserts == {}
    ((failure,),) = [recorded]
    assert failure["projection"] == "triples_reconcile"
    restore_id = failure["payload"].pop("restore_id")
    assert failure["payload"] == {
        "memory_ids": ["m1"],
        "reopen": True,
        "restored_at": restored.isoformat(),
        "content_sha256": {"m1": "h"},
    }
    # The id its replay finds the Undo's markers by.
    assert len(restore_id) == 32


async def _undo_of_rome_and(
    monkeypatch,
    berlin: dict[str, Any],
    rome: dict[str, Any] | None = None,
    held: list[str] | None = None,
) -> tuple[_CH, list]:
    """An Undo of a row holding Rome (undated, stored first, or `rome`'s columns) and
    Berlin (`berlin`'s columns), both closed by its retirement, or only the `held` of
    them. Returns the CH and the repaired triples."""
    from types import SimpleNamespace

    import server.identity.app.namespace_ontology as onto
    from server.memory.app import graph_holders, graph_undo

    stored = datetime(2026, 10, 1)

    class _ClosedCH(_CH):
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            if "FROM loom.entity_triples" in sql:
                base = {
                    "subject": "user",
                    "relation": "live_in",
                    "confidence": 1.0,
                    "provenance": "extracted",
                    "ends": None,
                    "start": None,
                    "open": 0,
                    "restored": 0,
                    "support": [],
                }
                rows = [
                    {**base, "object": "rome", "seen": stored, **(rome or {})},
                    {**base, "object": "berlin", "seen": stored, **berlin},
                ]
                return [_as_read(r, params) for r in rows if r["object"] in cities]
            return [
                {"entity_a": a, "entity_b": b, "ends": None, "restored": 0, "support": []}
                for a, b in params["pairs"]
            ]

    cities = held or ["berlin", "rome"]

    async def linked(*a: Any, **k: Any) -> list:
        return [("user", "live_in", "berlin"), ("user", "live_in", "rome")]

    async def stated(*a: Any, **k: Any) -> list:
        return [("user", "live_in", city) for city in cities]

    async def declared(*a: Any) -> dict:
        return {}

    repaired: list = []

    async def repair(input: Any, triples: list, *, ch: Any, at: datetime, on_fly: bool) -> list:
        repaired.extend(triples)
        return []

    monkeypatch.setattr(graph_undo, "linked_triples", linked)
    monkeypatch.setattr(graph_holders, "stated_by_live_rows", _as_set(stated))
    monkeypatch.setattr(graph_holders, "held_by_unlinked_rows", _none_held)
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    monkeypatch.setattr(onto, "load_relation_cardinality", declared)
    ch = _ClosedCH()
    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    await graph_undo.reopen_held_facts(
        SimpleNamespace(org="o", namespace="ns"),
        ["m1"],
        ch=ch,
        restored_at=restored,
        restore_id=_UNDO_ID,
    )
    return ch, repaired


_UNDO_ID = "undo-1"


def _as_read(row: dict[str, Any], params: Any) -> dict[str, Any]:
    """`row` as the Undo's read returns it: a stored marker is its own only when it
    carries the Undo's id (this Undo's unless given), and a fact's newest open row is
    its latest one unless given."""
    row = {"newest_open": (row["start"], row["seen"]) if row["open"] else _NO_OPEN_ROW, **row}
    if "marker_seen" in row:
        row["restored"] = int(row.pop("restore_id", _UNDO_ID) == params["rid"])
    return row


def _weights(rows: list[dict[str, Any]], *keys: str) -> dict[str, tuple]:
    return {
        r["entity_a"] if "entity_a" in r else r["object"]: tuple(r[k] for k in keys) for r in rows
    }


async def test_an_undo_of_two_values_weighs_only_the_one_left_current(monkeypatch) -> None:
    # "I lived in Rome. I now live in Berlin": Berlin's marker closes Rome's as it
    # starts, so Rome's marker is never current and adds no weight to its total.
    ch, _ = await _undo_of_rome_and(monkeypatch, {"seen": datetime(2026, 10, 1, 0, 0, 1)})
    assert _weights(ch.inserts["loom.entity_triples"], "co_occur") == {"rome": (0,), "berlin": (1,)}
    assert _weights(ch.inserts["loom.entity_edges"], "co_occur") == {"rome": (0,), "berlin": (1,)}


async def test_an_undo_keeps_a_fact_dated_ahead_starting_on_its_date(monkeypatch) -> None:
    # "I live in Rome. Moving to Berlin on Dec 1": Berlin starts on Dec 1 again, so
    # Rome is current, and weighed, until then.
    now = datetime(2026, 10, 15, 9, 0, 0)
    dec = datetime(2026, 12, 1)
    ch, repaired = await _undo_of_rome_and(monkeypatch, {"start": dec})
    assert _weights(ch.inserts["loom.entity_triples"], "valid_from", "co_occur") == {
        "rome": (now, 1),
        "berlin": (dec, 1),
    }
    assert _weights(ch.inserts["loom.entity_edges"], "valid_from", "co_occur") == {
        "rome": (now, 1),
        "berlin": (dec, 1),
    }
    assert [(t["object"], t["valid_from"]) for t in repaired] == [("rome", now), ("berlin", dec)]


async def test_an_undo_starts_at_the_restore_a_fact_another_open_row_dates_ahead(
    monkeypatch,
) -> None:
    # Another live row says Berlin from Dec 1 and is still open: the restored row's
    # own Berlin is undone from the restore.
    now = datetime(2026, 10, 15, 9, 0, 0)
    ch, _ = await _undo_of_rome_and(monkeypatch, {"start": datetime(2026, 12, 1), "open": 1})
    assert _weights(ch.inserts["loom.entity_triples"], "valid_from")["berlin"] == (now,)


async def test_an_undo_weighs_a_value_a_later_one_closes_only_after_it_starts(
    monkeypatch,
) -> None:
    # Rome (closed) from Dec 1; Berlin's open row dates it Dec 5. Berlin's marker starts
    # at the restore but is older than Rome's, so only Berlin's Dec 5 row closes Rome:
    # Rome is current from Dec 1 to Dec 5 and keeps its weight.
    dec = datetime(2026, 12, 1)
    ch, _ = await _undo_of_rome_and(
        monkeypatch, {"start": datetime(2026, 12, 5), "open": 1}, rome={"start": dec}
    )
    assert _weights(ch.inserts["loom.entity_triples"], "valid_from", "co_occur")["rome"] == (
        dec,
        1,
    )
    assert _weights(ch.inserts["loom.entity_edges"], "co_occur")["rome"] == (1,)


async def test_an_undo_weighs_a_value_its_own_open_row_keeps_newer(monkeypatch) -> None:
    # Rome's open row dates it Dec 5, Berlin's Dec 10: both markers start at the
    # restore, Berlin's a microsecond later, but Rome's Dec 5 row is newer than it, so
    # Rome closes only on Dec 10 and its marker is current until then.
    ch, _ = await _undo_of_rome_and(
        monkeypatch,
        {"start": datetime(2026, 12, 10), "open": 1},
        rome={"start": datetime(2026, 12, 5), "open": 1},
    )
    assert _weights(ch.inserts["loom.entity_triples"], "co_occur") == {"rome": (1,), "berlin": (1,)}


async def test_a_replay_restates_the_markers_it_finds_as_they_are_stored(monkeypatch) -> None:
    # "I live in Rome. Moving to Berlin on Dec 1": the first attempt's markers landed
    # (Rome from the restore, Berlin from Dec 1), then its repair failed. The replay
    # restates Berlin from Dec 1 again, so Rome still closes on Dec 1, not now.
    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    now, dec = restored.replace(microsecond=0), datetime(2026, 12, 1)

    def marker(start: datetime, seen: datetime) -> dict[str, Any]:
        return {
            "start": start,
            "seen": seen,
            "open": 1,
            "restored": 1,
            "marker_start": start,
            "marker_seen": seen,
        }

    later = restored + timedelta(microseconds=1)
    ch, repaired = await _undo_of_rome_and(
        monkeypatch, marker(dec, later), rome=marker(now, restored)
    )
    assert ch.inserts == {}  # the markers are not written twice
    assert [(t["object"], t["valid_from"], t["seen_at"]) for t in repaired] == [
        ("rome", now, restored),
        ("berlin", dec, later),
    ]


async def test_an_undo_weighs_a_value_by_its_newest_open_row(monkeypatch) -> None:
    # Berlin's open row says "moving Dec 5" (seen Sep 7); an earlier Undo's undated,
    # open marker of it was seen later, on Sep 15. The repair judges Berlin by its Dec 5
    # row, which stays newer than Rome's marker, so Berlin's marker carries the weight.
    berlin = {
        "seen": datetime(2026, 9, 15),
        "open": 1,
        "newest_open": (datetime(2026, 12, 5), datetime(2026, 9, 7)),
    }
    ch, _ = await _undo_of_rome_and(monkeypatch, berlin)
    assert _weights(ch.inserts["loom.entity_triples"], "co_occur")["berlin"] == (1,)


def _marker(start: datetime, seen: datetime) -> dict[str, Any]:
    """A fact whose latest row is the first attempt's marker, open."""
    return {"start": start, "seen": seen, "open": 1, "marker_start": start, "marker_seen": seen}


async def test_a_replay_with_fewer_facts_held_writes_no_second_marker(monkeypatch) -> None:
    # The first attempt held Rome and Berlin, so Berlin's marker is the second. Rome is
    # no longer held at the replay: it still finds Berlin's marker and adds none.
    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    now = restored.replace(microsecond=0)
    later = restored + timedelta(microseconds=1)
    ch, repaired = await _undo_of_rome_and(
        monkeypatch, _marker(now, later), rome=_marker(now, restored), held=["berlin"]
    )
    assert ch.inserts == {}
    assert [(t["object"], t["seen_at"]) for t in repaired] == [("berlin", later)]


async def test_a_replay_with_more_facts_held_gives_a_new_marker_its_own_verdict(
    monkeypatch,
) -> None:
    # The first attempt held only Rome; its marker is stored. Berlin is held at the
    # replay: its marker is seen after Rome's, never at it, so it is the newer value
    # and keeps its weight.
    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    ch, repaired = await _undo_of_rome_and(
        monkeypatch, {}, rome=_marker(restored.replace(microsecond=0), restored)
    )
    (berlin,) = ch.inserts["loom.entity_triples"]
    assert berlin["object"] == "berlin" and berlin["seen_at"] > restored
    assert berlin["co_occur"] == 1
    assert len({t["seen_at"] for t in repaired}) == 2


async def test_another_undos_marker_is_not_taken_for_this_ones(monkeypatch) -> None:
    # Berlin's latest row is a marker another Undo wrote, a microsecond after this
    # Undo's instant: this Undo writes its own Berlin marker, carrying its id.
    restored = datetime(2026, 10, 15, 9, 0, 0, 5)
    later = restored + timedelta(microseconds=1)
    theirs = {**_marker(restored.replace(microsecond=0), later), "restore_id": "undo-2"}
    ch, _ = await _undo_of_rome_and(monkeypatch, theirs)
    written = {m["object"]: m["restore_id"] for m in ch.inserts["loom.entity_triples"]}
    assert written == {"rome": _UNDO_ID, "berlin": _UNDO_ID}
    assert {e["restore_id"] for e in ch.inserts["loom.entity_edges"]} == {_UNDO_ID}


def test_the_row_links_migrations_the_legacy_checks_name_exist() -> None:
    # Renumbered and missed, a check would find no applied time and drop legacy rows.
    from server.core.paths import MIGRATIONS_DIR
    from server.memory.app.graph_holders import _ROW_LINKS_MIGRATION
    from server.memory.app.projections import EDGE_ROW_LINKS_MIGRATION

    assert (MIGRATIONS_DIR / _ROW_LINKS_MIGRATION).exists()
    assert (MIGRATIONS_DIR / EDGE_ROW_LINKS_MIGRATION).exists()


def _as_set(stub: Any) -> Any:
    """A stub of `stated_by_live_rows`, which answers a frozenset."""

    async def answer(*a: Any, **k: Any) -> frozenset:
        return frozenset(await stub(*a, **k))

    return answer


async def _none_held(*a: Any, **k: Any) -> frozenset:
    """No row written before row links holds anything."""
    return frozenset()


@pytest.mark.parametrize("ids", [["m1"], ["m1", "m2"]])
async def test_an_undo_reads_legacy_rows_whatever_links_its_rows_have(monkeypatch, ids) -> None:
    # A link of another value, or a successor written for it, leaves a row a legacy one:
    # the read decides by the links of its current value.
    from types import SimpleNamespace

    from server.memory.app import graph_holders, graph_undo

    read: list[list[str]] = []

    async def linked(*a: Any, **k: Any) -> list:
        return [("user", "live_in", "rome")]

    async def legacy(org: str, namespace: str, triples: Any, *, ch: Any, rows: list) -> frozenset:
        read.append(rows)
        return frozenset()

    monkeypatch.setattr(graph_undo, "linked_triples", linked)
    monkeypatch.setattr(graph_holders, "stated_by_live_rows", _none_held)
    monkeypatch.setattr(graph_holders, "held_by_unlinked_rows", legacy)
    scope = SimpleNamespace(org="o", namespace="ns")
    await graph_undo.reopen_held_facts(
        scope, ids, ch=_CH(), restored_at=datetime(2026, 10, 15), restore_id="u1"
    )
    assert read == [ids]


@pytest.mark.parametrize("sensitivity", ["high", "low"])
async def test_a_failed_undo_reopen_records_no_high_rows_hash(monkeypatch, sensitivity) -> None:
    """The fact reopen of an Undo fails and is recorded for the repair: a 'high' row's
    content_sha256 stays out of the payload, its id stays in (the repair reads the row
    as it is), and another row's hash is kept as its fence."""
    from types import SimpleNamespace

    from server.memory.app import edge_links, graph_undo, restore, triple_projections

    recorded: dict[str, Any] = {}

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("CH down")

    async def record(input: Any, **kw: Any) -> None:
        recorded.update(kw)

    monkeypatch.setattr(graph_undo, "reopen_held_facts", down)
    monkeypatch.setattr(edge_links, "record_input_projection_failure", record)
    monkeypatch.setattr(triple_projections, "record_input_projection_failure", record)
    scope = SimpleNamespace(org="o", namespace="n", agent="a", user_id="-", session_id="-")
    row = SimpleNamespace(memory_id="m1", content_sha256="f" * 64, sensitivity=sensitivity)
    await restore._reopen_facts(scope, row, 1_700_000_000_000_000_000, ch=None)
    payload = recorded["payload"]
    assert payload["memory_ids"] == ["m1"] and payload["reopen"] is True
    want = {} if sensitivity == "high" else {"m1": "f" * 64}
    assert payload["content_sha256"] == want
