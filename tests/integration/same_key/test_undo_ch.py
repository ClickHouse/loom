"""An Undo against ClickHouse: the facts and edges a restored row holds open again,
and how they weigh against the values written around the restore."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from tests.integration._resources import unique_org
from tests.integration.same_key._rows import (
    KEY,
    NS,
    USER,
    _another_orgs_erasure_pending,
    _date_undos_at,
    _edges_like_triples,
    _embedding_status,
    _follow_retirement,
    _london_edges,
    _london_undone_over_paris,
    _marker_weights,
    _mutations_done,
    _open_cities,
    _open_edges,
    _open_facts,
    _open_weights,
    _recorded_reconcile,
    _restore_id,
    _restored_markers,
    _retired_embedding,
    _retry_one,
    _revise,
    _row,
    _row_and_write,
    _stated,
    _support,
    _today,
    _undo_in_the_second_of,
    _undo_of,
    _walked,
)


async def test_undo_brings_back_the_facts_the_restored_row_holds(ch, org) -> None:
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(london["memory_id"]),
        )
    )
    assert out.restored
    assert await _open_cities(ch, org, {"london", "paris"}) == {"london", "paris"}


async def test_an_undo_leaves_the_graph_to_a_revision_made_before_its_graph_step(
    ch, org, monkeypatch
) -> None:
    # The Undo restores London and releases the key's lock; before its graph step runs,
    # the row is revised to Rome and Berlin is written. The Undo must not assert Rome.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import triple_projections
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reconcile, revised = triple_projections.reconcile_row_facts, []

    async def revised_first(*args: Any, **kwargs: Any) -> None:
        if kwargs.get("op") != "memory.restore" or revised:
            return await reconcile(*args, **kwargs)
        revised.append(True)
        await _revise("memory.update_by_id", org, london, "I live in Rome", ((*live, "rome"),))
        await _stated(ch, org, "s3", "I moved to Berlin", (*live, "berlin"), memory_key="k3")
        await _mutations_done(ch, org)
        await reconcile(*args, **kwargs)

    monkeypatch.setattr(triple_projections, "reconcile_row_facts", revised_first)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_leaves_the_graph_to_a_revision_made_while_its_graph_step_reads(
    ch, org, monkeypatch
) -> None:
    # As above, but the revision lands while the graph step reads what the row holds.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    linked, revised = graph_undo.linked_triples, []

    async def revised_first(*args: Any, **kwargs: Any) -> Any:
        if not revised:
            revised.append(True)
            await _revise("memory.update_by_id", org, london, "I live in Rome", ((*live, "rome"),))
            await _stated(ch, org, "s3", "I moved to Berlin", (*live, "berlin"), memory_key="k3")
            await _mutations_done(ch, org)
        return await linked(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "linked_triples", revised_first)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored and revised
    await _mutations_done(ch, org)
    # Nor does it assert the revised value at its own time: Rome was not stated then.
    undone = await ch.query_all(
        "SELECT object FROM loom.entity_triples WHERE org = {org:String} AND restore_id != ''",
        params={"org": org},
    )
    assert undone == []
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_closes_its_markers_for_a_revision_made_after_its_hash_check(
    ch, org, monkeypatch
) -> None:
    # The revision lands after the Undo checked the row's hash, before its markers:
    # the revision's own close ran too early to see them, so the Undo closes them.
    from server.identity.app import namespace_ontology
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, revised = graph_undo.rows_still_holding, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        revised.append(False)
        return await checked(*args, **kwargs)

    async def revised_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if revised == [False]:
            revised.append(True)
            await _revise(
                "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
            )
            await _mutations_done(ch, org)
        return await cardinality(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", revised_after_the_check)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored and True in revised
    await _mutations_done(ch, org)
    # The open facts are what the live rows hold: Rome and Paris, not London.
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_an_undo_closes_its_markers_for_a_retirement_made_after_its_hash_check(
    ch, org, monkeypatch
) -> None:
    # As above, but the restored London row is retired behind a Rome row: it keeps its
    # value, so only its status says it no longer holds London.
    from server.identity.app import namespace_ontology
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, retired = graph_undo.rows_still_holding, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        retired.append(False)
        return await checked(*args, **kwargs)

    async def retired_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if retired == [False]:
            retired.append(True)
            rome = await _stated(
                ch, org, "s3", "I like Rome", ("user", "like", "rome"), memory_key="k3"
            )
            await _follow_retirement(ch, org, london, rome)
        return await cardinality(*args, **kwargs)

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", retired_after_the_check)
    assert (await handle_memory_restore(_undo_of(paris))).restored and True in retired
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_a_replayed_undo_closes_its_markers_for_a_revision_made_during_it(
    ch, org, monkeypatch
) -> None:
    # As above, but the Undo's own close fails: its replay finds the revised row no
    # longer holding the restored value, and must still close London's marker.
    from server.identity.app import namespace_ontology
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "I like London", ("user", "like", "london"))
    paris = await _stated(ch, org, "s2", "I like Paris", ("user", "like", "paris"))
    await _follow_retirement(ch, org, london, paris)
    checked, closed, revised = graph_undo.rows_still_holding, graph_undo.close_dropped_facts, []
    cardinality = namespace_ontology.load_relation_cardinality

    async def check(*args: Any, **kwargs: Any) -> Any:
        revised.append(False)
        return await checked(*args, **kwargs)

    async def revised_after_the_check(*args: Any, **kwargs: Any) -> Any:
        if revised == [False]:
            revised.append(True)
            await _revise(
                "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
            )
            await _mutations_done(ch, org)
        return await cardinality(*args, **kwargs)

    async def close_fails(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("close down")

    monkeypatch.setattr(graph_undo, "rows_still_holding", check)
    monkeypatch.setattr(graph_undo, "close_dropped_facts", close_fails)
    monkeypatch.setattr(namespace_ontology, "load_relation_cardinality", revised_after_the_check)
    assert (await handle_memory_restore(_undo_of(paris))).restored and True in revised
    monkeypatch.setattr(graph_undo, "close_dropped_facts", closed)
    assert (await _retry_one(await _recorded_reconcile(ch, org), ch)).resolved
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "paris")}
    assert await _open_facts(ch, org, want) == want


async def test_undo_makes_the_restored_value_current_and_retires_nothing(
    ch, org, monkeypatch
) -> None:
    # An Undo is the newest assertion: the restored London is the current home, and
    # Paris's fact closes. The Paris row stays live: an Undo never retires a row.
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(
        ch, org, "s1", "I currently live in London", ("user", "live_in", "london")
    )
    paris = await _stated(ch, org, "s2", "I currently live in Paris", ("user", "live_in", "paris"))
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    # Undone in the second Paris was written, after it: the Undo is the newer.
    restored_at = await _undo_in_the_second_of(ch, org, "paris", monkeypatch)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    # The edge plane agrees: London's edge is the one current edge of the user.
    assert await _open_edges(ch, org, {"london"}) == ["london"]
    # The retirement closed London's own rows, so the Undo's open rows carry its
    # weight: London is current for the readers on either plane, Paris is not.
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    assert await _open_weights(ch, org, "paris") == {"triples": 0, "edges": 0}
    assert await _walked(org, ["london"], labeled=True) == ["london"]
    assert await _walked(org, ["london"], labeled=False) == ["london"]
    # A replay of the Undo finds its observations and weighs London no more.
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restored_at=restored_at,
        restore_id=await _restore_id(ch, org),
    )
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    (row,) = await ch.query_all(
        "SELECT status FROM loom.memory FINAL WHERE org = {org:String} AND memory_id = {id:UUID}",
        params={"org": org, "id": str(paris["memory_id"])},
    )
    assert row["status"] == "active"


async def test_an_undo_keeps_its_edge_open_past_another_relations_end(ch, org) -> None:
    # The pair's latest edge is a bounded work fact; the restored London is open-ended,
    # and so is its edge: past the work fact's end both walks still reach London.
    from server.memory.app.restore import handle_memory_restore

    until = naive_utc_now() + timedelta(days=17)
    london = await _stated(ch, org, "s1", "I live in London", ("user", "live_in", "london"))
    paris = await _stated(ch, org, "s2", "I live in Paris", ("user", "live_in", "paris"))
    work = ("user", "work_in", "london")
    await _stated(ch, org, "s3", "I work in London", work, memory_key="job", fact_valid_to=until)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    later = until + timedelta(days=30)
    assert await _walked(org, ["london"], labeled=True, as_of=later) == ["london"]
    assert await _walked(org, ["london"], labeled=False, as_of=later) == ["london"]


async def test_an_undo_weighs_its_fact_after_the_retirements_closes_land(ch, org) -> None:
    # The retirement's closes are mutations. Held back (merges stopped), London's old
    # row is still stored open when the Undo runs; the Undo reads with the pending
    # closes applied, so its own row carries London's weight once they land.
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore
    from server.memory.app.triple_follow import follow_row_supersessions

    london = await _stated(ch, org, "s1", "My favourite city is London")
    paris = await _stated(ch, org, "s2", "My favourite city is Paris")
    await _edges_like_triples(ch, org)
    retired = {**london, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(paris["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    # Merges stop on shared tables: only from the retirement's closes to the Undo.
    tables = ("loom.entity_triples", "loom.entity_edges")
    for table in tables:
        await ch.execute(f"SYSTEM STOP MERGES {table}")
    try:
        await follow_row_supersessions(
            SimpleNamespace(org=org, namespace=NS),
            [(str(london["memory_id"]), retired["superseded_by"])],
            ch=ch,
        )
        (pending,) = await ch.query_all(
            "SELECT count() AS n FROM system.mutations WHERE database = 'loom'"
            " AND table = 'entity_triples' AND NOT is_done"
            " AND position(command, {org:String}) > 0",
            params={"org": org},
        )
        assert pending["n"] > 0, "the close landed before the Undo: nothing is tested"
        out = await handle_memory_restore(
            MemoryRestoreInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key=KEY,
                if_superseded_by=str(paris["memory_id"]),
            )
        )
    finally:
        for table in tables:
            await ch.execute(f"SYSTEM START MERGES {table}")
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}


async def test_an_undo_waits_for_another_orgs_subquery_mutation_to_reach_the_graph(ch, org) -> None:
    # While the erasure is pending the Undo restores the row and writes nothing to
    # the graph: the failure is recorded, and its replay makes London current, weighed
    # once, on both planes.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    async with _another_orgs_erasure_pending(ch):
        out = await handle_memory_restore(_undo_of(paris))
        failure = await _recorded_reconcile(ch, org)
        assert await _restored_markers(ch, org) == 0
    assert out.restored
    assert failure.payload["memory_ids"] == [str(london["memory_id"])]
    assert failure.payload["restored_at"] and failure.payload["content_sha256"]
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _open_edges(ch, org, {"london"}) == ["london"]
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    assert await _walked(org, ["london"], labeled=True) == ["london"]
    assert await _walked(org, ["london"], labeled=False) == ["london"]


async def test_a_deferred_undo_adds_no_weight_to_a_fact_another_live_row_holds(ch, org) -> None:
    # Two live rows say London (weight 2). An Undo of one, deferred by another org's
    # erasure and replayed after it, leaves London's weight at 2.
    from types import SimpleNamespace

    from server.memory.app.triple_projections import reconcile_row_facts

    london = await _stated(ch, org, "s1", "My favourite city is London")
    await _stated(ch, org, "s3", "My favourite city is London", memory_key="k3")
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 2, "edges": 2}
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    ids = [str(london["memory_id"])]
    async with _another_orgs_erasure_pending(ch):
        await reconcile_row_facts(
            scope,
            ids,
            reopen=True,
            op="memory.restore",
            ch=ch,
            hashes={ids[0]: london["content_sha256"]},
        )
        failure = await _recorded_reconcile(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 2, "edges": 2}


async def test_an_undo_starts_a_fact_dated_ahead_on_its_date(ch, org) -> None:
    # "I live in Rome. Moving to Berlin on Dec 1": the row retires behind Paris and is
    # Undone. Berlin starts on Dec 1 again, and Rome is current until then.
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    december = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=47)
    row, write, results = await _row_and_write(
        ch, org, "s1", "I live in Rome. Moving to Berlin on Dec 1.", [(*live, "rome")]
    )
    await write_entity_triples(write, results, ch=ch, entity_map={})
    # The same row states Berlin, dated Dec 1.
    entry = write.entries[0].model_copy(
        update={"relations": [(*live, "berlin")], "valid_at": december}
    )
    dated = write.model_copy(update={"entries": [entry]})
    await write_entity_triples(dated, results, ch=ch, entity_map={})
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    markers = await ch.query_all(
        "SELECT object, valid_from, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        # Paris, which Berlin's start ended, is stated again by the retirement too.
        " AND adapter_name = 'row-restore' AND object != 'paris' ORDER BY object"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    berlin, rome = markers
    assert (berlin["object"], berlin["valid_from"], berlin["valid_to"]) == (
        "berlin",
        december,
        None,
    )
    assert (rome["object"], rome["valid_to"]) == ("rome", december)
    assert rome["valid_from"] < december
    edges = await ch.query_all(
        "SELECT entity_a, valid_from, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a IN ('berlin', 'rome') SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    edges = {(e["entity_a"], e["valid_from"], e["valid_to"]) for e in edges}
    assert {("berlin", december, None), ("rome", rome["valid_from"], december)} <= edges
    # No Berlin edge is current before Dec 1.
    assert not [e for e in edges if e[0] == "berlin" and e[2] is None and e[1] < december]


async def test_a_replayed_undo_restates_a_fact_dated_ahead_as_stored(ch, org, monkeypatch) -> None:
    # As above, but the repair fails after the markers land. The replay reads them back
    # as stored, Berlin from Dec 1 to the second, so Rome still closes on Dec 1.
    from server.memory.app import graph_undo
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    december = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=47)
    row, write, results = await _row_and_write(
        ch, org, "s1", "I live in Rome. Moving to Berlin on Dec 1.", [(*live, "rome")]
    )
    await write_entity_triples(write, results, ch=ch, entity_map={})
    entry = write.entries[0].model_copy(
        update={"relations": [(*live, "berlin")], "valid_at": december}
    )
    dated = write.model_copy(update={"entries": [entry]})
    await write_entity_triples(dated, results, ch=ch, entity_map={})
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    repair = graph_undo.repair_intervals

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("repair down")

    monkeypatch.setattr(graph_undo, "repair_intervals", _fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    failure = await _recorded_reconcile(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    markers = await ch.query_all(
        "SELECT object, valid_from, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        # Paris, which Berlin's start ended, is stated again by the retirement too.
        " AND adapter_name = 'row-restore' AND object != 'paris' ORDER BY object"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    berlin, rome = markers  # one each: the replay wrote none
    assert (berlin["object"], berlin["valid_from"], berlin["valid_to"]) == (
        "berlin",
        december,
        None,
    )
    assert (rome["object"], rome["valid_to"]) == ("rome", december)


async def test_a_bulk_write_keeps_the_value_with_the_newest_observation(ch, org) -> None:
    # One write: London from Oct 15, Paris from Oct 14, London from Oct 13. London has
    # the newest observation and stays current; Paris closes when it starts.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    live = ("user", "live_in")
    stated = [
        ("k1", "I moved to London this week", "london", datetime(2026, 10, 15)),
        ("k2", "Paris was my home base for a while", "paris", datetime(2026, 10, 14)),
        ("k3", "Back then I had a flat near the Thames", "london", datetime(2026, 10, 13)),
    ]
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {"memory_key": k, "value": v, "relations": [(*live, city)], "valid_at": at}
                for k, v, city, at in stated
            ],
        )
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    (paris,) = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'paris' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert paris["valid_to"] == datetime(2026, 10, 15)


async def test_an_undo_of_a_fact_open_until_later_adds_no_weight(ch, org) -> None:
    # "I live in London until next year": the readers count that row as current, so
    # the Undo's own row of it weighs nothing.
    from types import SimpleNamespace

    from server.memory.app import graph_undo

    until = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=60)
    london = await _stated(
        ch, org, "s1", "I live in London", ("user", "live_in", "london"), fact_valid_to=until
    )
    await _edges_like_triples(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    # The Undo's own rows end where London does, so London is not current past then.
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND co_occur = 0"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", until),
        ("triples", until),
    ]


async def test_an_undo_is_weighed_against_what_is_valid_at_the_restore(ch, org) -> None:
    # London retires behind Paris; another row says London from next week. That row is
    # not valid at the Undo, so the Undo's own London rows carry the weight then.
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse
    from server.memory.app.restore import handle_memory_restore

    london = await _stated(ch, org, "s1", "My favourite city is London")
    paris = await _stated(ch, org, "s2", "My favourite city is Paris")
    await _follow_retirement(ch, org, london, paris)
    later = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=5)
    await _stated(ch, org, "s3", "My favourite city is London", memory_key="k3", valid_at=later)
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    as_of = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    for labeled in (True, False):
        walk = FactsTraverseInput(
            org=org, namespace=NS, seed_entity="user", max_hops=1, labeled=labeled, as_of=as_of
        )
        nodes = {n.entity: n.via_co_occur for n in (await handle_facts_traverse(walk)).nodes}
        assert nodes.get("london", 0) >= 1, (labeled, nodes)


async def test_an_undo_keeps_no_end_a_supersession_set(ch, org) -> None:
    # London undated; Berlin from next month closes London when Berlin begins; Berlin's
    # fact then closes. That end was Berlin's, not London's: an Undo of London leaves
    # London open.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    starts = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s2", "I move to Berlin", (*live, "berlin"), memory_key="k2", valid_at=starts
    )
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "berlin", now, now)])
    await _mutations_done(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND valid_from >= {now:DateTime}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "now": now},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", None),
        ("triples", None),
    ]
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_an_undo_of_a_row_holding_two_values_keeps_the_later_one_current(ch, org) -> None:
    # "I lived in Rome. I now live in Berlin.": Berlin, stated after Rome, is current.
    # The row retires behind Paris and is Undone: Berlin is current again, not Rome.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    row = await _stated(
        ch,
        org,
        "s1",
        "I lived in Rome. I now live in Berlin.",
        (*live, "rome"),
        also=((*live, "berlin"),),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, row, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_weighs_a_fact_whose_row_a_close_ends_ahead(ch, org) -> None:
    # A close dated December ends London's row ahead and stamps it now: that row is
    # not current for the readers, so the Undo's own London row carries the weight.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=60)
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "london", december, now)])
    await _mutations_done(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    (marker,) = await ch.query_all(
        "SELECT co_occur FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert marker["co_occur"] == 1


async def test_an_undo_keeps_its_weight_past_the_end_of_a_row_weighed_now(ch, org) -> None:
    # An older London row carries weight until next month; the Undo's London has no
    # end. Past that month London is still current, so the Undo's rows weigh 1.
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app.facts import handle_facts_traverse
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    next_month = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=17)
    await _support(ch, org, "london", next_month)
    await _edges_like_triples(ch, org)
    before = await _london_edges(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    assert await _marker_weights(ch, org, before) == {"triples": [1], "edges": [1]}
    for labeled in (True, False):
        walk = FactsTraverseInput(
            org=org,
            namespace=NS,
            seed_entity="user",
            max_hops=1,
            labeled=labeled,
            as_of=next_month + timedelta(days=13),
        )
        nodes = {n.entity: n.via_co_occur for n in (await handle_facts_traverse(walk)).nodes}
        assert nodes.get("london", 0) >= 1, (labeled, nodes)


async def test_an_undo_dated_ahead_keeps_its_weight_past_a_row_that_ends_first(ch, org) -> None:
    # London from Dec 1 retires behind Paris and is Undone: its rows start on Dec 1.
    # An older London row weighs until next month only, so it never covers them.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=47)
    london = await _stated(
        ch, org, "s1", "I move to London on Dec 1", (*live, "london"), valid_at=december
    )
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    await _support(ch, org, "london", now + timedelta(days=17))
    await _edges_like_triples(ch, org)
    before = await _london_edges(ch, org)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    (start,) = await ch.query_all(
        "SELECT valid_from FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' AND object = 'london'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert start["valid_from"] == december
    assert await _marker_weights(ch, org, before) == {"triples": [1], "edges": [1]}


async def test_two_undos_a_microsecond_apart_each_keep_their_own_markers(
    ch, org, monkeypatch
) -> None:
    # Two rows say "I lived in Rome. I now live in Berlin." and retire behind Paris.
    # Undone a microsecond apart, the second must not take the first's Berlin marker
    # for its own: Berlin, stated after Rome, stays current.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    value = "I lived in Rome. I now live in Berlin."
    first = await _stated(ch, org, "s1", value, (*live, "rome"), also=((*live, "berlin"),))
    second = await _stated(
        ch, org, "s3", value, (*live, "rome"), also=((*live, "berlin"),), memory_key="k3"
    )
    await _mutations_done(ch, org)
    paris = await _stated(ch, org, "s2", "I live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, first, paris)
    await _follow_retirement(ch, org, second, paris)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    at = datetime.now(UTC).replace(tzinfo=None)
    undo_second = _undo_of(paris).model_copy(update={"session_id": "s3", "memory_key": "k3"})
    for undo, instant in ((_undo_of(paris), at), (undo_second, at + timedelta(microseconds=1))):
        _date_undos_at(monkeypatch, lambda t=instant: t)
        assert (await handle_memory_restore(undo)).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_an_undo_takes_no_end_from_an_older_row_of_its_fact(ch, org) -> None:
    # An older row said London until December; a later row said London with no end,
    # and a supersession closed it. The latest row's end was the close's: the Undo
    # keeps London open, not until December.
    from types import SimpleNamespace

    from server.memory.app import graph_undo
    from server.memory.app.projections import close_triples, entity_triple_row

    live = ("user", "live_in")
    now = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    december = now + timedelta(days=60)
    hour_ago = now - timedelta(hours=1)
    older = entity_triple_row(
        org,
        NS,
        *live,
        "london",
        provenance="t",
        confidence=1.0,
        adapter_name="t",
        last_seen=hour_ago,
        seen_at=hour_ago,
        valid_to=december,
    )
    await ch.insert("loom.entity_triples", [older], settings={"wait_for_async_insert": 1})
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"))
    await close_triples(ch, org=org, namespace=NS, closes=[(*live, "london", now, now)])
    await _mutations_done(ch, org)
    await _edges_like_triples(ch, org)
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restore_id=uuid.uuid4().hex,
        restored_at=naive_utc_now(),
    )
    await _mutations_done(ch, org)
    ends = await ch.query_all(
        "SELECT 'triples' AS plane, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore'"
        " UNION ALL SELECT 'edges', valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND last_seen >= {now:DateTime} AND invalid_at IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "now": now},
    )
    assert sorted((str(e["plane"]), e["valid_to"]) for e in ends) == [
        ("edges", None),
        ("triples", None),
    ]


async def test_undo_of_an_open_fact_keeps_its_date_for_later_writes(ch, org, monkeypatch) -> None:
    # London from May retires behind Paris from June; another row says London from
    # July. Undoing the May row is the newest assertion, so Rome from August arrives
    # older than it and closes, however often the Undo is replayed.
    from types import SimpleNamespace

    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _follow_retirement(ch, org, london, paris)
    await _stated(
        ch, org, "s3", "I live in London", (*live, "london"), valid_at=datetime(2026, 7, 1)
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _edges_like_triples(ch, org)
    restored_at = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    _date_undos_at(monkeypatch, lambda: restored_at)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(paris["memory_id"]),
        )
    )
    assert out.restored
    # A replay, as the repair runs it: the Undo's instant and id.
    scope = SimpleNamespace(org=org, namespace=NS, agent="bot")
    await graph_undo.reopen_held_facts(
        scope,
        [str(london["memory_id"])],
        ch=ch,
        restored_at=restored_at,
        restore_id=await _restore_id(ch, org),
    )
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' AND valid_from = {at:DateTime}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "at": restored_at},
    )
    assert counted["n"] == 1
    await _mutations_done(ch, org)
    # London's edges are the current ones, one of them from the Undo, however often
    # it is replayed.
    assert set(await _open_edges(ch, org, {"london"})) == {"london"}
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'london' AND valid_from = {at:DateTime} AND valid_to IS NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "at": restored_at},
    )
    assert counted["n"] == 1
    # London from July was open with its weight: the Undo adds none.
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 1}
    await _stated(ch, org, "s4", "I live in Rome", (*live, "rome"), valid_at=datetime(2026, 8, 1))
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_failed_interval_repair_is_recorded_for_replay(ch, org, monkeypatch) -> None:
    from server.memory.app import projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
    )

    await _stated(ch, org, "s1", "I currently live in London", ("user", "live_in", "london"))
    repair = projections._repair_contradictory_triples

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("repair down")

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _fails)
    await _revise(
        "memory.set", org, {"session_id": "s2"}, "I moved to Rome", (("user", "live_in", "rome"),)
    )
    # The failure log is a fire-and-forget async insert: wait for its flush.
    for _ in range(100):
        failures = await list_projection_failures(
            org=org, projection="entity_triples_repair", ch=ch
        )
        if failures:
            break
        await asyncio.sleep(0.1)
    (failure,) = failures
    monkeypatch.setattr(projections, "_repair_contradictory_triples", repair)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    # The triples landed before the repair failed: the replay repaired, not inserted.
    (counted,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'rome' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert counted["n"] == 1


async def test_a_replayed_undo_is_dated_at_the_undo(ch, org, monkeypatch) -> None:
    # The Undo's reconcile fails; Rome is stated after it. Replayed, the Undo brings
    # London back as of the Undo, so Rome, the newer value, stays current.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app import graph_undo
    from server.memory.app.projection_failures import (
        TRIPLES_RECONCILE,
        list_projection_failures,
    )
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reopen = graph_undo.reopen_held_facts

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("reconcile down")

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _fails)
    undo = MemoryRestoreInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key=KEY,
        if_superseded_by=str(paris["memory_id"]),
    )
    assert (await handle_memory_restore(undo)).restored
    monkeypatch.setattr(graph_undo, "reopen_held_facts", reopen)
    # Rome arrives after the Undo, in its second or a later one.
    await _stated(ch, org, "s3", "I moved to Rome", (*live, "rome"), memory_key="k3")
    await _mutations_done(ch, org)
    for _ in range(100):
        failures = await list_projection_failures(org=org, projection=TRIPLES_RECONCILE, ch=ch)
        if failures:
            break
        await asyncio.sleep(0.1)
    (failure,) = failures
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_undo_reopens_only_what_the_restored_value_holds(ch, org, monkeypatch) -> None:
    # The row said Paris, was revised to London, then retired behind Rome. Another
    # row still says Paris, but the restored row no longer does: Undo reopens London.
    from server.core.hashing import sha256_value
    from server.memory import store as M
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.projections import write_entity_triples
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    paris = await _stated(ch, org, "s1", "I currently live in Paris", (*live, "paris"))
    await _stated(ch, org, "s3", "I currently live in Paris", (*live, "paris"))
    text = "I currently live in London"
    london = {
        **paris,
        "version": M.next_version(),
        "value": json.dumps(text),
        "value_json": json.dumps({"v": text}),
        "content_sha256": sha256_value(text),
    }
    await M.insert_memory_versions([london], ch=ch)
    write = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        entries=[{"memory_key": KEY, "value": text, "relations": [(*live, "london")]}],
    )
    result = _BulkSetResult(
        memory_key=KEY, version=london["version"], memory_id=str(london["memory_id"])
    )
    await write_entity_triples(write, [result], ch=ch, entity_map={})
    rome = await _stated(ch, org, "s2", "I currently live in Rome", (*live, "rome"))
    await _follow_retirement(ch, org, london, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    await _undo_in_the_second_of(ch, org, "rome", monkeypatch)
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key=KEY,
            if_superseded_by=str(rome["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_an_undo_stays_newer_than_a_value_dated_before_it(ch, org) -> None:
    # London (May) retires behind Paris (June); an Undo brings London back now. Rome,
    # dated July and imported after the Undo, is older than the Undo's assertion.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _follow_retirement(ch, org, london, paris)
    undo = MemoryRestoreInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key=KEY,
        if_superseded_by=str(paris["memory_id"]),
    )
    assert (await handle_memory_restore(undo)).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _stated(
        ch,
        org,
        "s3",
        "I live in Rome",
        (*live, "rome"),
        memory_key="k3",
        valid_at=datetime(2026, 7, 1),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_restored_row_edited_to_an_older_value_leaves_the_newer_one_current(
    ch, org
) -> None:
    # The edit to Rome keeps the row's May date. Rome closes behind the Undo's London
    # marker, then London closes: Paris (June) is current, as without the Undo.
    from server.memory.api.models_crud import MemoryGetByIdInput, MemoryUpdateByIdInput
    from server.memory.app.handlers import handle_memory_get_by_id
    from server.memory.app.update import handle_memory_update_by_id

    london = await _london_undone_over_paris(ch, org, "live_in")
    got = await handle_memory_get_by_id(
        MemoryGetByIdInput(org=org, memory_id=str(london["memory_id"]))
    )
    out = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org=org,
            memory_id=str(london["memory_id"]),
            value="I live in Rome",
            relations=[("user", "live_in", "rome")],
            if_match_sha256=got.content_sha256,
        )
    )
    assert out.updated
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}


async def test_a_revision_dropping_the_undone_value_makes_the_value_it_beat_current(
    ch, org
) -> None:
    # The restored London row no longer says where the user lives: the Paris row
    # still does, so Paris is current again rather than no value at all.
    london = await _london_undone_over_paris(ch, org, "live_in")
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want
    # London's marker is the Undo's; Paris's, stated again for want of a value, is not.
    markers = await ch.query_all(
        "SELECT object, adapter_name FROM loom.entity_triples WHERE org = {org:String}"
        " AND restore_id != '' ORDER BY object SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [(m["object"], m["adapter_name"]) for m in markers] == [
        ("london", "row-restore"),
        ("paris", "row-fallback"),
    ]


async def test_a_retirement_of_the_undone_value_makes_the_value_it_beat_current(ch, org) -> None:
    # As above, but London's row is retired behind a row that only says Rome.
    london = await _london_undone_over_paris(ch, org, "live_in")
    rome = await _stated(ch, org, "s3", "I like Rome", ("user", "like", "rome"))
    await _follow_retirement(ch, org, london, rome)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_a_value_closed_with_a_future_end_is_not_current(ch, org) -> None:
    # London (2027) closes Paris (June) at 2027-01-01. London's row then drops it:
    # Paris is current again, not closed at a day no value follows.
    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1))
    london = await _stated(
        ch, org, "s2", "I live in London", (*live, "london"), valid_at=datetime(2027, 1, 1)
    )
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_unrelated_revisions_add_no_weight_to_a_value_valid_until_a_dated_move(
    ch, org
) -> None:
    # Paris holds until the move to Berlin next month starts. Each revision of an
    # unrelated Oslo row checks the relation, finds Paris valid now and states nothing.
    live = ("user", "live_in")
    ahead = naive_utc_now() + timedelta(days=45)
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1))
    await _stated(
        ch, org, "s2", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    oslo = await _stated(
        ch,
        org,
        "s3",
        "I lived in Oslo",
        (*live, "oslo"),
        memory_key="trip",
        valid_at=datetime(2026, 1, 1),
    )
    await _mutations_done(ch, org)
    assert (await _open_weights(ch, org, "paris"))["triples"] == 1
    for value in ("I like Rome", "I like Rome a lot", "I like Rome most"):
        await _revise("memory.update_by_id", org, oslo, value, (("user", "like", "rome"),))
        await _mutations_done(ch, org)
    assert (await _open_weights(ch, org, "paris"))["triples"] == 1


async def test_a_recovered_value_starts_at_the_revision_and_walks_on_both_planes(ch, org) -> None:
    # London undone over Paris at T1, then revised away at T2: Paris is stated again
    # from T2, its edge too, and a walk between T1 and T2 still reaches only London.
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(
        ch, org, "s1", "I live in London", (*live, "london"), valid_at=datetime(2026, 5, 1)
    )
    paris = await _stated(
        ch, org, "s2", "I live in Paris", (*live, "paris"), valid_at=datetime(2026, 6, 1)
    )
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the DateTime columns store whole seconds
    between = naive_utc_now()
    await asyncio.sleep(1.1)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    assert await _walked(org, ["paris"], labeled=True) == ["paris"]
    # London's edge ends with its triple: no live row states it.
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]
    assert await _walked(org, ["london"], labeled=True, as_of=between) == ["london"]
    assert await _walked(org, ["london"], labeled=False, as_of=between) == ["london"]


def _with_conflicted_peer(
    org: str, status: str = "conflicted"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Paris and its `conflicted` peer, each listing the other; Paris has `status`."""
    paris = _row(org, "s1", "User lives in Paris", datetime(2023, 5, 30))
    peer = _row(org, "s9", "User lives in Rome", datetime(2023, 5, 30), memory_key="city")
    peer |= {"status": "conflicted", "conflict_with": [str(paris["memory_id"])]}
    paris |= {"status": status, "conflict_with": [str(peer["memory_id"])]}
    return paris, peer


async def test_an_undo_gives_a_conflicted_row_back_its_conflict(ch, org) -> None:
    # Retired while under review: its peer still lists it, so it returns to the queue.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris, peer = _with_conflicted_peer(org)
    await M.insert_memory_versions([peer], ch=ch)
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([paris, london, retired], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    head = await M.get_by_id(
        str(paris["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
    assert (head.status, head.conflict_with) == ("conflicted", [str(peer["memory_id"])])
    # Search ranks on the embedding row's status: it carries the conflict too.
    assert await _embedding_status(ch, paris) == "conflicted"


async def _status_of(ch: Any, row: dict[str, Any]) -> str:
    """The status of `row`'s key's head, read by key: an id held under two keys could read
    as either."""
    from server.memory import store as M

    identity = M.Identity.model_validate(row)
    head = await M.get_by_key(
        identity, row["memory_key"], scope=WHOLE_NAMESPACE, ch=ch, strong=True
    )
    assert head is not None
    return head.status


@pytest.mark.parametrize(
    "column", ["org", "namespace", "agent", "user_id", "session_id", "memory_key", "memory_id"]
)
async def test_an_undo_reads_its_earlier_status_by_its_own_key(ch, org, column) -> None:
    # A conflicted version below the retirement under another key part (a namespace restored
    # into itself or from another one holds the id there), or of another id under the key,
    # is not this row's to bring back; nor is one in another org.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    # Only a leaked copy could make the Undo answer `conflicted`.
    paris, peer = _with_conflicted_peer(org, status="active")
    if column == "org":
        other = unique_org("same-key-other")
    elif column == "memory_id":
        other = str(uuid.uuid4())
    else:
        other = "other"
    leaked = {**paris, column: other, "version": M.next_version(), "status": "conflicted"}
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([peer, paris, leaked, london, retired], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    assert await _status_of(ch, paris) == "active"
    # Search ranks on the embedding row's status: it is active too.
    assert await _embedding_status(ch, paris) == "active"


async def test_the_status_before_a_retirement_skips_versions_above_it(ch, org) -> None:
    # A plain write takes no key lock, so one can land above the head an Undo read: the
    # status is read below the retirement, not from that write.
    from server.memory import store as M

    paris = _row(org, "s1", "User lives in Paris", datetime(2023, 5, 30), status="conflicted")
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    rewritten = {**paris, "version": M.next_version(), "status": "active"}
    await M.insert_memory_versions([paris, retired, rewritten], ch=ch)
    assert await M.status_before(M._hydrate(retired), ch=ch) == "conflicted"


async def test_an_undo_brings_back_the_newest_earlier_status(ch, org) -> None:
    # Reviewed, then rewritten active, then retired: the Undo returns the row to active, not
    # to the review it left before the rewrite. The peer is left listing the row on purpose,
    # so the older `conflicted` version would come back if it were the one read.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris, peer = _with_conflicted_peer(org)
    rewritten = {**paris, "version": M.next_version(), "status": "active"}
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**rewritten, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([peer, paris, rewritten, london, retired], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    assert await _status_of(ch, paris) == "active"


async def test_an_undo_reads_past_an_earlier_retirement(ch, org) -> None:
    # Reviewed, retired, then retired again: the Undo of the last retirement skips the first
    # and returns the row to its review, which its peer still holds.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris, peer = _with_conflicted_peer(org)
    first = {**paris, "version": M.next_version(), "status": "superseded"}
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([peer, paris, first, london, retired], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    assert await _status_of(ch, paris) == "conflicted"


@pytest.mark.parametrize("peer_end", ["deleted", "resolved"])
async def test_an_undo_after_its_peer_left_review_restores_active(ch, org, peer_end) -> None:
    # The peer was deleted or resolved while the row was retired: no live peer
    # lists it, so the Undo has nothing to be in conflict with.
    from server.memory import store as M
    from server.memory.app.restore import handle_memory_restore

    paris, peer = _with_conflicted_peer(org)
    london = _row(org, "s2", "User lives in London", datetime(2023, 6, 30))
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    if peer_end == "deleted":
        ended = {"status": "tombstoned", "tombstoned_at": datetime.now(UTC).replace(tzinfo=None)}
    else:
        ended = {"status": "active", "conflict_with": []}
    peer_ended = {**peer, "version": M.next_version(), **ended}
    await M.insert_memory_versions([paris, peer, london, retired, peer_ended], ch=ch)
    await _retired_embedding(ch, paris)
    assert (await handle_memory_restore(_undo_of(london))).restored
    head = await M.get_by_id(
        str(paris["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
    assert head.status == "active"
    assert await _embedding_status(ch, paris) == "active"


async def test_a_replayed_undo_leaves_a_revision_since_to_its_own_reconcile(
    ch, org, monkeypatch
) -> None:
    # The Undo of London fails; the row is then revised to Berlin. The replay never
    # dates Berlin at the Undo: that revision's own reconcile owns the row.
    from server.memory.app import graph_undo
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    reopen = graph_undo.reopen_held_facts

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("reconcile down")

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "reopen_held_facts", reopen)
    failure = await _recorded_reconcile(ch, org)
    await _revise("memory.update_by_id", org, london, "I live in Berlin", ((*live, "berlin"),))
    await _mutations_done(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    restores = await ch.query_all(
        "SELECT object FROM loom.entity_triples WHERE org = {org:String}"
        " AND adapter_name = 'row-restore' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert restores == []


async def test_a_second_undo_in_the_second_of_the_first_makes_its_value_current(
    ch, org, monkeypatch
) -> None:
    # Undo London, Rome supersedes it, Undo London again: all in one stored second.
    # The second Undo has its own instant: its own assertion, not a replay of the first.
    from server.memory.app import projections
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    second = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    first_undo, rome_at, second_undo = (second.replace(microsecond=us) for us in (1, 2, 3))
    _date_undos_at(monkeypatch, lambda: first_undo)
    monkeypatch.setattr(projections, "naive_utc_now", lambda: rome_at)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    await _mutations_done(ch, org)
    rome = await _stated(ch, org, "s3", "I moved to Rome", (*live, "rome"), memory_key="k3")
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    _date_undos_at(monkeypatch, lambda: second_undo)
    assert (await handle_memory_restore(_undo_of(rome))).restored
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_dated_and_an_undated_writer_racing_leave_one_current_value(
    ch, org, monkeypatch
) -> None:
    # Undated London and Rome dated yesterday: both insert before either repairs, and
    # both agree on London, the later day, whichever arrived first.
    from server.memory.app import projections

    repair = projections._repair_contradictory_triples
    arrived: list[int] = []
    both = asyncio.Event()

    async def _together(*a: Any, **k: Any) -> Any:
        arrived.append(1)
        if len(arrived) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 10)
        return await repair(*a, **k)

    monkeypatch.setattr(projections, "_repair_contradictory_triples", _together)
    live = ("user", "live_in")
    yesterday = _today() - timedelta(days=1)
    await asyncio.gather(
        _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1"),
        _stated(
            ch, org, "s2", "I live in Rome", (*live, "rome"), memory_key="k2", valid_at=yesterday
        ),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_three_values_each_repaired_against_all_three_leave_one_current(
    ch, org, monkeypatch
) -> None:
    # A cycle under the old pairwise rules: A dated Oct 13 noon, B undated, C dated
    # Oct 14, arriving in the order C, B, A. Each repair reads all three open before
    # any close lands; by (start day, seen_at) all three agree that B is current.
    from types import SimpleNamespace

    from server.memory.app import projections

    def row(obj: str, valid_from: datetime | None, second: int) -> dict[str, Any]:
        seen = datetime(2026, 10, 15, 0, 0, second)
        return {
            "org": org,
            "namespace": NS,
            "subject": "user",
            "relation": "live_in",
            "object": obj,
            "provenance": "extracted",
            "co_occur": 1,
            "last_seen": seen,
            "seen_at": seen,
            "confidence": 1.0,
            "adapter_name": "itest",
            "valid_from": valid_from,
            "valid_to": None,
            "invalid_at": None,
        }

    rows = [
        row("a", datetime(2026, 10, 13, 12), 3),
        row("b", None, 2),
        row("c", datetime(2026, 10, 14), 1),
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    close = projections.close_triples
    read: list[int] = []
    all_read = asyncio.Event()

    async def _after_every_read(*a: Any, **k: Any) -> Any:
        read.append(1)
        if len(read) == len(rows):
            all_read.set()
        await asyncio.wait_for(all_read.wait(), 10)
        return await close(*a, **k)

    monkeypatch.setattr(projections, "close_triples", _after_every_read)
    scope = SimpleNamespace(org=org, namespace=NS)
    await asyncio.gather(
        *(projections.repair_intervals(scope, [r], ch=ch, at=r["seen_at"]) for r in rows)
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"b"}) == {"b"}


async def test_a_replayed_undo_leaves_a_value_asserted_later_in_its_second_current(
    ch, org, monkeypatch
) -> None:
    # The Undo of London at T lands, then fails. Rome, dated T, is asserted later in
    # the same second and is current. Replayed with its own instant, the Undo keeps
    # its place: before Rome, so Rome stays current.
    from server.memory.app import graph_undo, projections
    from server.memory.app.restore import handle_memory_restore

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I currently live in London", (*live, "london"))
    paris = await _stated(ch, org, "s2", "I currently live in Paris", (*live, "paris"))
    await _follow_retirement(ch, org, london, paris)
    second = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(seconds=1)
    undo_at, rome_at = second.replace(microsecond=100), second.replace(microsecond=200)
    _date_undos_at(monkeypatch, lambda: undo_at)
    repair = graph_undo.repair_intervals

    async def _lands_then_fails(*a: Any, **k: Any) -> Any:
        await repair(*a, **k)
        raise RuntimeError("reconcile down after it landed")

    monkeypatch.setattr(graph_undo, "repair_intervals", _lands_then_fails)
    assert (await handle_memory_restore(_undo_of(paris))).restored
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    monkeypatch.setattr(projections, "naive_utc_now", lambda: rome_at)
    await _stated(
        ch, org, "s3", "I live in Rome", (*live, "rome"), memory_key="k3", valid_at=second
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert (await _retry_one(await _recorded_reconcile(ch, org), ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_a_replayed_insert_of_an_older_value_leaves_the_newer_one_current(
    ch, org, monkeypatch
) -> None:
    # London's insert fails, then Paris is stated. The replay inserts London dated at
    # its own write, so it lands behind Paris instead of closing it.
    from server.memory.app import projections
    from server.memory.app.projection_errors import bulk_projection_payload
    from server.memory.app.projection_failures import ProjectionFailureRow

    lives = ("user", "live_in", "london")
    _row, write, results = await _row_and_write(
        ch, org, "s1", "I currently live in London", [lives]
    )
    insert = projections._insert_triples

    async def _insert_fails(*args: Any, **kw: Any) -> None:
        raise RuntimeError("insert timed out")

    monkeypatch.setattr(projections, "_insert_triples", _insert_fails)
    with pytest.raises(Exception) as raised:
        await projections.write_entity_triples(write, results, ch=ch, entity_map={})
    monkeypatch.setattr(projections, "_insert_triples", insert)
    await _stated(ch, org, "s2", "I currently live in Paris", ("user", "live_in", "paris"))
    payload = bulk_projection_payload(write, results, error=raised.value)
    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        source_resource_id="",
        memory_keys=[results[0].memory_key],
        error_type="RuntimeError",
        error_message="insert timed out",
        payload=json.loads(json.dumps(payload)),
    )
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "paris")}) == {("live_in", "paris")}
