"""The fact and edge graph following the rows against ClickHouse: what a write,
a revision, a retirement or a replay leaves open, closed or linked to its row."""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.core.datetimes import naive_utc_now
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from tests.integration.same_key._rows import (
    _LONG_JAPANESE_NAME,
    _POINT_PATHS,
    KEY,
    NS,
    USER,
    _bulk_beside_a_pin,
    _edges_like_triples,
    _failed_repair,
    _follow_retirement,
    _HolderLandsAfterTheRead,
    _legacy_stated,
    _link_inferred,
    _listed,
    _live_values,
    _london_ends_with_the_next_revision,
    _london_undone_over_paris,
    _mentions,
    _mutations_done,
    _open_cities,
    _open_edges,
    _open_facts,
    _open_weights,
    _past,
    _recorded_reconcile,
    _replay_follow,
    _restored_dependent,
    _retry_one,
    _revise,
    _rome_raced_by_back_to_london,
    _row,
    _row_and_write,
    _row_saying,
    _stated,
    _status,
    _today,
    _walked,
    _walks_to,
    _written_earlier,
)


async def test_a_close_never_ends_a_triple_before_it_starts(ch, org) -> None:
    from server.memory.app.projections import close_triples, entity_triple_row

    def triple(obj: str, valid_from: datetime | None) -> dict[str, Any]:
        return entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            obj,
            provenance="extracted",
            confidence=1.0,
            adapter_name="itest",
            last_seen=datetime(2026, 10, 1),
            seen_at=datetime(2026, 10, 1),
            valid_from=valid_from,
        )

    # Each close carries its own date, so one landing on another row's would show.
    future, past, now = datetime(2027, 1, 1), datetime(2025, 1, 1), datetime(2026, 10, 5)
    rome_until = datetime(2026, 3, 1)
    rows = [
        triple("paris", future),
        triple("rome", past),
        triple("oslo", None),
        triple("lima", None),  # not closed
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    closes = [
        ("user", "live_in", "paris", now, now),
        ("user", "live_in", "rome", rome_until, now),
        ("user", "live_in", "oslo", now, now),
    ]
    await close_triples(ch, org=org, namespace=NS, closes=closes)
    # The close is a mutation: wait for it to land.
    for _ in range(50):
        got = await ch.query_all(
            "SELECT object, valid_to FROM loom.entity_triples WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        valid_to = {r["object"]: r["valid_to"] for r in got}
        if all(valid_to.get(o) is not None for o in ("paris", "rome", "oslo")):
            break
        await asyncio.sleep(0.2)
    assert valid_to == {"paris": future, "rome": rome_until, "oslo": now, "lima": None}


async def test_ending_a_future_triple_again_keeps_its_close(ch, org) -> None:
    # A row dated ahead ends at its own start; a later end_triples_now finds nothing to
    # end and must leave invalid_at, the close's own mark, where the first one put it.
    from server.memory.app.projections import end_triples_now, entity_triple_row

    start = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=30)
    row = entity_triple_row(
        org,
        NS,
        "user",
        "live_in",
        "paris",
        provenance="extracted",
        confidence=1.0,
        adapter_name="itest",
        last_seen=start,
        seen_at=start,
        valid_from=start,
    )
    row["valid_to"] = start + timedelta(days=30)
    await ch.insert("loom.entity_triples", [row], settings={"wait_for_async_insert": 1})

    async def _end() -> dict[str, Any]:
        await end_triples_now(ch, org=org, namespace=NS, triples=[("user", "live_in", "paris")])
        await _mutations_done(ch, org)
        (got,) = await ch.query_all(
            "SELECT valid_to, invalid_at FROM loom.entity_triples WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return got

    first = await _end()
    assert first["valid_to"] == start and first["invalid_at"] is not None
    await asyncio.sleep(1.1)  # a second later: a rewrite would move invalid_at
    assert await _end() == first


async def test_open_triples_are_looked_up_by_their_exact_key(ch, org) -> None:
    # Only the open copy counts: a closed one may be written again by a replay.
    from server.memory.app.projections import entity_triple_row, open_triples_among

    def triple(obj: str, **kw: Any) -> dict[str, Any]:
        return entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            obj,
            provenance="extracted",
            confidence=1.0,
            adapter_name="itest",
            last_seen=datetime(2026, 10, 1),
            seen_at=datetime(2026, 10, 1),
            **kw,
        )

    closed = datetime(2026, 10, 2)
    rows = [
        triple("berlin germany"),
        triple("berlin germany", valid_to=closed),
        triple("rome", valid_to=closed),
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    asked = [
        ("user", "live_in", "berlin germany"),
        ("user", "live_in", "berlin"),
        ("user", "live_in", "rome"),
    ]
    found = await open_triples_among(ch, org=org, namespace=NS, triples=asked)
    assert found == {("user", "live_in", "berlin germany")}


async def test_a_triple_closed_a_moment_ago_does_not_read_open(ch, org) -> None:
    # A close is a mutation that may still be pending when the next read runs.
    from server.memory.app.projections import close_triples, entity_triple_row, open_triples_among

    t = ("office", "located_in", "paris")
    row = entity_triple_row(
        org,
        NS,
        *t,
        provenance="extracted",
        confidence=1.0,
        adapter_name="itest",
        last_seen=datetime(2026, 10, 1),
        seen_at=datetime(2026, 10, 1),
    )
    await ch.insert("loom.entity_triples", [row], settings={"wait_for_async_insert": 1})
    await close_triples(ch, org=org, namespace=NS, closes=[(*t, datetime(2026, 10, 2), None)])
    assert await open_triples_among(ch, org=org, namespace=NS, triples=[t]) == set()


async def test_a_first_person_correction_closes_the_triple_its_row_stated(ch, org) -> None:
    # "My" is stored as `user`, which the row's text never names.
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_triple_another_live_row_states_stays_open_until_it_is_revised(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    sibling = await _stated(ch, org, "s3", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london", "paris"}) == {"london", "paris"}
    # The sibling is revised in place: its id stays, its value no longer states Paris.
    await _stated(ch, org, "s3", "My favourite city is Rome", memory_id=sibling["memory_id"])
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london", "rome"}) == {"london", "rome"}


@pytest.mark.parametrize(
    ("revised", "want"),
    [
        # Its words are unchanged: the claim its extractor stated still holds.
        ("My favourite city is Paris.", {"london", "paris"}),
        # Words alone cannot tell added detail from a retraction.
        ("My favourite city is Paris, for now", {"london"}),
    ],
)
async def test_a_live_row_revised_without_its_relations_keeps_what_it_still_states(
    ch, org, revised, want
) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    sibling = await _stated(ch, org, "s3", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London")
    # Revised in place, its extractor naming no relation this time.
    await _stated(ch, org, "s3", revised, extracted=False, memory_id=sibling["memory_id"])
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want


async def test_a_bulk_write_links_its_triple_to_the_row_it_stored(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": KEY,
                    "value": "My favourite city is Paris",
                    "relations": [("user", "favourite_city", "paris")],
                }
            ],
        )
    )
    stored = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    links = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha, object"
        " FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r.memory_id for r in out.written] == [stored[0]["id"]]
    assert links == [{**stored[0], "object": "paris"}]


async def test_a_replayed_bulk_edge_projection_skips_a_value_the_same_bulk_replaced(
    ch, org, monkeypatch
) -> None:
    # One bulk revises a row twice and its edge insert fails: the replay asks by value,
    # so the row holding Rome does not bring back London's edge.
    from server.core import ch as ch_mod
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.write import handle_memory_set

    def _entry(city: str) -> dict[str, Any]:
        return {
            "memory_key": "alice",
            "value": f"Alice likes {city}",
            "entities": ["alice", city.lower()],
        }

    await handle_memory_set(
        MemorySetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", **_entry("Paris")
        )
    )
    await after_write.drain()
    client = ch_mod.get_client()
    insert = client.insert

    async def edges_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_edges":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", edges_fail)
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[_entry("London"), _entry("Rome")],
        )
    )
    await after_write.drain()
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org, "entity_edges")
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second an edge ended in
    walked = await _walked(org, ["rome"], labeled=False, seed="alice", until=lambda w: "rome" in w)
    assert "rome" in walked and "london" not in walked


@pytest.mark.parametrize("verdict", ["conflict", "refines"])
async def test_a_verdict_on_a_value_the_same_bulk_replaced_settles_nothing(
    ch, org, monkeypatch, verdict
) -> None:
    # One bulk writes alice twice; only the first value is judged against Paris. The
    # row ends holding Rome, so neither it nor Paris may be flagged or retired.
    from types import SimpleNamespace

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictPeer

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    paris = await handle_memory_set(
        MemorySetInput(**ids, memory_key="bob", value="Bob likes Paris")
    )
    await after_write.drain()
    peer = ConflictPeer(
        memory_id=paris.memory_id,
        memory_key="bob",
        similarity=0.9,
        pinned=False,
        canonical="bob likes paris",
        agent="bot",
        user_id=USER,
        session_id="s1",
        value="Bob likes Paris",
    )
    judged = SimpleNamespace(verdict=verdict, peers=[peer], refined_peers=[peer])

    async def first_entry_judged(*a: Any, **k: Any) -> dict[int, Any]:
        return {0: judged}

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", first_entry_judged)
    out = await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            **ids,
            entries=[
                {"memory_key": "alice", "value": "Alice likes London"},
                {"memory_key": "alice", "value": "Alice likes Rome"},
            ],
        )
    )
    await after_write.drain()
    alice = {"memory_id": out.written[-1].memory_id}
    assert await _status(ch, org, alice) == "active"
    assert await _status(ch, org, {"memory_id": paris.memory_id}) == "active"


async def test_a_refine_of_a_row_a_stale_merge_rewrote_retires_nothing(
    ch, org, monkeypatch
) -> None:
    # Stored: A is Berlin, B is Jupiter. One bulk rewrites A, writes Berlin to B (a merge
    # into A, whose Berlin the first entry replaced, so it lands) and elaborates Jupiter
    # under C. C's refine was judged against B's old Jupiter: B now says Berlin, and
    # retiring it would lose the fact.
    from types import SimpleNamespace

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.conflicts import ConflictPeer, ConflictResult

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    a = await handle_memory_set(MemorySetInput(**ids, memory_key="a", value="Berlin"))
    b = await handle_memory_set(MemorySetInput(**ids, memory_key="b", value="Jupiter"))
    await after_write.drain()
    jupiter = ConflictPeer(
        memory_id=b.memory_id,
        memory_key="b",
        similarity=0.9,
        pinned=False,
        canonical="jupiter",
        agent="bot",
        user_id=USER,
        session_id="s1",
        value="Jupiter",
    )
    judged = {
        1: SimpleNamespace(verdict="merge", existing_memory_id=a.memory_id, peers=[]),
        2: ConflictResult(verdict="refines", peers=[jupiter], refined_peers=[jupiter]),
    }

    async def verdicts(*args: Any, **kw: Any) -> dict[int, Any]:
        return judged

    monkeypatch.setattr(bulk_set, "_bulk_conflict_verdicts", verdicts)
    await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            **ids,
            entries=[
                {"memory_key": "a", "value": "A red bicycle"},
                {"memory_key": "b", "value": "Berlin"},
                {"memory_key": "c", "value": "Jupiter, the largest planet"},
            ],
        )
    )
    await after_write.drain()
    assert await _status(ch, org, {"memory_id": b.memory_id}) == "active"
    assert (await _live_values(ch, org))[("s1", "b")] == ("Berlin", 0)


@pytest.mark.parametrize(
    "extra",
    [{}, {"valid_at": datetime(2019, 1, 1), "valid_to": datetime(2020, 1, 1)}],
    ids=["open", "closed-in-the-past"],
)
async def test_a_replayed_bulk_edge_projection_writes_what_the_bulk_write_would(
    ch, org, monkeypatch, extra
) -> None:
    # The edge insert fails once; the replay projects the entry as the write did,
    # a fact that ended in the past included.
    from server.core import ch as ch_mod
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    client = ch_mod.get_client()
    insert = client.insert

    async def edges_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_edges":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", edges_fail)
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": KEY,
                    "value": "Alice met Bob",
                    "entities": ["alice", "bob"],
                    **extra,
                }
            ],
        )
    )
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org, "entity_edges")
    assert (await _retry_one(failure, ch)).resolved
    (row,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'bob' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert row["n"] == 1


async def test_the_follows_own_successor_closes_when_its_row_retires(ch, org) -> None:
    # London's and Rome's extractors stated only a food: London's city is the follow's.
    pasta = ("user", "favourite_food", "pasta")
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    london = await _stated(ch, org, "s2", "My favourite city is London", triple=pasta)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london", "pasta"}) == {"london", "pasta"}
    rome = await _stated(ch, org, "s3", "My favourite city is Rome", triple=pasta)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"pasta", "rome"}) == {"pasta", "rome"}


async def test_repeated_first_person_corrections_keep_only_the_last(ch, org) -> None:
    # Only the first write's extractor named a relation; the follow writes the rest.
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    rome = await _stated(ch, org, "s3", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}


async def test_a_successor_closes_after_its_row_is_revised_in_place(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place: the id stays, the value (and so its hash) changes.
    acme = ("user", "works_at", "acme")
    london = await _stated(
        ch, org, "s2", "I live in London, at Acme", triple=acme, memory_id=london["memory_id"]
    )
    rome = await _stated(ch, org, "s3", "I live in Rome, at Acme", triple=acme)
    await _follow_retirement(ch, org, london, rome)
    assert await _open_cities(ch, org, {"acme", "rome"}) == {"acme", "rome"}


async def test_an_abandoned_repair_keeps_what_the_write_recorded(ch, org) -> None:
    from server.memory.app.projection_failures import (
        FOLLOW_ROWS,
        mark_projection_failures_abandoned,
        record_projection_failure,
    )

    await record_projection_failure(
        org=org, namespace=NS, projection=FOLLOW_ROWS, error="pairs left: 3", ch=ch
    )
    rows: list[dict[str, Any]] = []
    for _ in range(50):  # the failure row is an async insert
        rows = await ch.query_all(
            "SELECT toString(failure_id) AS id FROM loom.memory_projection_failures"
            " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if rows:
            break
        await asyncio.sleep(0.2)
    (row,) = rows
    await mark_projection_failures_abandoned(
        org=org, failure_ids=[row["id"]], reason="unfollowable_chain: 1 pair(s)", ch=ch
    )
    for _ in range(50):
        (got,) = await ch.query_all(
            "SELECT toString(status) AS status, error_message, resolved_at"
            " FROM loom.memory_projection_failures WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        if got["status"] == "abandoned":
            break
        await asyncio.sleep(0.2)
    assert got == {
        "status": "abandoned",
        "error_message": "pairs left: 3; abandoned: unfollowable_chain: 1 pair(s)",
        "resolved_at": None,
    }


async def test_a_deleted_row_is_found_by_id_only_when_asked(ch, org) -> None:
    from server.memory import store as M

    row = _row(org, "s1", "Paris", datetime(2026, 5, 1))
    deleted = {**row, "version": M.next_version(), "tombstoned_at": datetime(2026, 10, 1)}
    await M.insert_memory_versions([row, deleted], ch=ch)
    mid = str(row["memory_id"])
    seen = await M.get_by_id(
        mid,
        org=org,
        ch=ch,
        scope=WHOLE_NAMESPACE,
        strong=True,
        include_tombstoned=True,
        namespace=NS,
    )
    assert seen is not None and seen.tombstoned_at is not None
    assert (
        await M.get_by_id(mid, org=org, ch=ch, scope=WHOLE_NAMESPACE, strong=True, namespace=NS)
        is None
    )


async def test_a_revised_row_still_holds_open_the_successor_written_for_it(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place to the same words: London is still its own.
    revised = "I currently live in London."
    await _stated(ch, org, "s2", revised, extracted=False, memory_id=london["memory_id"])
    # Another session's London row retires behind Rome: the revised row keeps London.
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    want = {"london", "rome"}
    assert await _open_cities(ch, org, want) == want


async def test_a_revision_that_drops_the_successor_lets_it_close(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised to Berlin: London is no longer the row's, so it is not carried.
    berlin = ("user", "favourite_city", "berlin")
    await _stated(
        ch, org, "s2", "I currently live in Berlin", triple=berlin, memory_id=london["memory_id"]
    )
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    want = {"berlin", "rome"}
    assert await _open_cities(ch, org, want) == want


@pytest.mark.parametrize("path", _POINT_PATHS)
@pytest.mark.parametrize(
    ("revised", "want"),
    [
        ("I currently live in London.", {"london", "rome"}),
        ("I currently live in London, near the river", {"rome"}),
        ("I currently live in Berlin", {"rome"}),
        # Still names London, not as where the user lives.
        ("I currently live in Rome and visited London", {"rome"}),
    ],
)
async def test_a_point_revision_carries_the_successor_it_still_states(
    ch, org, path, revised, want
) -> None:
    if path == "memory.set high":
        want = {"rome"}  # a 'high' row holds no graph facts, whatever it says
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    await _revise(path, org, london, revised)
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want


# A 'high' row's named facts are not graphed: its words alone decide.
@pytest.mark.parametrize(
    ("path", "want"),
    [
        ("memory.set", {"london", "rome"}),
        ("memory.set high", {"rome"}),
        ("memory.update_by_id", {"london", "rome"}),
        ("memory.update", {"london", "rome"}),
    ],
)
async def test_a_point_revision_that_states_its_fact_keeps_it_through_added_detail(
    ch, org, path, want
) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    lives = ("user", "favourite_city", "london")
    await _revise(path, org, london, "I currently live in London, near the river", (lives,))
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want


async def test_a_point_write_naming_a_single_valued_fact_closes_the_older_one(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    await _stated(ch, org, "s1", "I currently live in London", ("user", "live_in", "london"))
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            memory_key="user_home",
            value="I moved to Rome",
            relations=[("user", "live_in", "rome")],
        )
    )
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "rome")}) == {("live_in", "rome")}


async def test_a_proposal_naming_a_single_valued_fact_changes_no_established_one(
    ch, org, newest_wins
) -> None:
    # A fact on trial supersedes nothing: the facts it names stay out of the graph.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    london = await _stated(ch, org, "s1", "I live in London", ("user", "live_in", "london"))
    await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            memory_key="user_home",
            value="I moved to Rome",
            relations=[("user", "live_in", "rome")],
            proposed=True,
        )
    )
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "london")}) == {("live_in", "london")}
    assert await _status(ch, org, london) == "active"


@pytest.mark.parametrize(
    ("legacy", "want"),
    [
        ("I like jazz", {("like", "jazz"), ("like", "rock")}),
        # Its key is linked to jazz, but its value no longer names it.
        ("I like folk", {("like", "rock")}),
    ],
)
async def test_a_fact_a_row_predating_row_links_states_survives_a_revision(
    ch, org, legacy, want
) -> None:
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, legacy, jazz, "legacy")
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want


async def test_a_fact_a_previous_build_row_states_survives_a_revision(ch, org) -> None:
    # Mid-rollout the previous build writes "I like jazz" with a key link and no row
    # link. Another row stating jazz is revised to rock: the previous build's row still
    # holds jazz.
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, "I like jazz", jazz, "older", written=naive_utc_now())
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


@pytest.mark.parametrize("changed", ["pinned", "revised"])
async def test_a_row_predating_row_links_holds_by_when_its_value_was_first_written(
    ch, org, changed
) -> None:
    # A pin, a lock, a tag or an Undo writes a new version of the same value: the row
    # still holds what its value stated before rows were linked.
    from server.memory import store as M

    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    want = {("like", "jazz"), ("like", "rock")}
    await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    (old,) = await ch.query_all(
        "SELECT memory_id FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_key = 'legacy'",
        params={"org": org},
    )
    head = await M.get_by_id(
        str(old["memory_id"]), org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
    if changed == "pinned":
        again = {"pinned": True}
    else:  # a value written since links, which states no fact, holds none
        retraction = "I no longer like jazz"
        again = {
            "value": json.dumps(retraction),
            "content_sha256": hashlib.sha256(retraction.encode()).hexdigest(),
        }
        want = {("like", "rock")}
    rewritten = M.build_memory_version_row(
        head, version=M.next_version(), updated_at=naive_utc_now(), **again
    )
    await M.insert_memory_versions([rewritten], ch=ch)
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want


async def test_an_inferred_link_leaves_a_row_predating_row_links_its_old_facts(ch, org) -> None:
    # A successor written for it is not what its value stated before rows were linked.
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    legacy = await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    await _link_inferred(ch, org, legacy, ("user", "like", "folk"))
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


async def test_a_row_written_since_row_links_that_stated_nothing_keeps_nothing_open(
    ch, org
) -> None:
    # "I no longer like jazz" produced no triples, so it has no row link; it is not a
    # row from before links, and must not keep jazz open once the jazz row is revised.
    from server.memory import store as M

    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await M.insert_memory_versions(
        [_row(org, "s9", "I no longer like jazz", naive_utc_now(), memory_key="r1")], ch=ch
    )
    linked = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    await _revise("memory.update_by_id", org, linked, "I like rock", (rock,))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("like", "rock")}) == {("like", "rock")}


async def test_a_fact_a_row_predating_row_links_states_survives_a_follow(ch, org) -> None:
    jazz, rock = ("user", "like", "jazz"), ("user", "like", "rock")
    await _legacy_stated(ch, org, "I like jazz", jazz, "legacy")
    loser = await _stated(ch, org, "s1", "I like jazz", jazz, memory_key="r1")
    winner = await _stated(ch, org, "s2", "I like rock", rock, memory_key="r1")
    await _follow_retirement(ch, org, loser, winner)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


@pytest.mark.parametrize("inferred", [False, True])
async def test_undo_brings_back_the_facts_a_row_predating_row_links_holds(
    ch, org, inferred
) -> None:
    # The follow closes a legacy row's facts by what its value names; the Undo has no
    # row link to find them by, and reopens them by the same rule. A successor linked
    # to the row (`inferred`) is no link to them.
    from server.memory.api.models_crud import MemoryRestoreInput
    from server.memory.app.restore import handle_memory_restore

    jazz, rock = ("alice", "like", "jazz"), ("alice", "like", "rock")
    legacy = await _legacy_stated(ch, org, "Alice likes jazz", jazz, KEY)
    if inferred:
        await _link_inferred(ch, org, legacy, ("alice", "like", "folk"))
    winner = await _stated(ch, org, "s2", "Alice likes rock", rock)
    await _follow_retirement(ch, org, legacy, winner)
    assert await _open_facts(ch, org, {("like", "rock")}) == {("like", "rock")}
    out = await handle_memory_restore(
        MemoryRestoreInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s0",
            memory_key=KEY,
            if_superseded_by=str(winner["memory_id"]),
        )
    )
    assert out.restored
    await _mutations_done(ch, org)
    want = {("like", "jazz"), ("like", "rock")}
    assert await _open_facts(ch, org, want) == want


async def test_a_merged_write_links_the_facts_it_names_to_the_row_it_merged_into(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    def write(key: str, relations: list, user: str = USER, **extra: Any) -> MemorySetInput:
        return MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=user,
            session_id="s1",
            memory_key=key,
            value="I currently live in London",
            relations=relations,
            **extra,
        )

    first = await handle_memory_set(write("home", []))
    merged = await handle_memory_set(write("city", [("user", "live_in", "london")]))
    assert merged.conflict_verdict == "merge"
    links = await ch.query_all(
        "SELECT toString(memory_id) AS id, toString(content_sha256) AS sha"
        " FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert links == [{"id": first.memory_id, "sha": first.content_sha256}]
    # Merged into another user's row, the facts link to nothing.
    theirs = await handle_memory_set(write("place", [("user", "live_in", "rome")], user="u2"))
    assert theirs.conflict_verdict == "merge" and "relations not linked" in theirs.next
    dry = await handle_memory_set(write("place", [("user", "live_in", "rome")], "u2", dry_run=True))
    assert dry.next == theirs.next
    links = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triple_rows WHERE org = {org:String}"
        " AND object = 'rome' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert links == [{"n": 0}]


async def test_a_row_restating_a_triple_while_the_follow_closes_it_keeps_it(
    ch, org, monkeypatch
) -> None:
    from server.memory.app import triple_projections

    london = await _stated(ch, org, "s1", "My favourite city is London")
    berlin = await _stated(ch, org, "s2", "My favourite city is Berlin")
    support = await _stated(ch, org, "s3", "My favourite city is Rome", pinned=1)
    close = triple_projections.close_triples

    async def _revised_back_first(*args: Any, **kw: Any) -> Any:
        # After the follow read its plan, before its close: the row says London again.
        await _stated(ch, org, "s3", "My favourite city is London", memory_id=support["memory_id"])
        return await close(*args, **kw)

    monkeypatch.setattr(triple_projections, "close_triples", _revised_back_first)
    await _follow_retirement(ch, org, london, berlin)
    await _mutations_done(ch, org)
    want = {"london", "berlin", "rome"}
    assert await _open_cities(ch, org, want) == want
    assert await _listed(org, "favourite_city", want) == want
    # Undone in place: London's two observations, both open, and no copy of either.
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [None, None]


@pytest.mark.parametrize("dated", [False, True])
async def test_a_value_stated_again_while_a_repair_closes_it_stays_current(
    ch, org, monkeypatch, dated
) -> None:
    # Back to London lands while Rome's close of London runs, and closes Rome. Dated
    # by its session day, before Rome's write, it is still the newer of the two.
    from server.memory.app import projections

    lives = ("user", "live_in", "london")
    await _stated(ch, org, "s1", "I currently live in London", lives)
    close, raced = projections.close_triples, []

    async def _back_to_london_first(*args: Any, **kw: Any) -> Any:
        # After the Rome repair read London open, before its close: London again.
        if not raced:
            raced.append(True)
            back_from = _today() if dated else None
            await _stated(ch, org, "s3", "I am back in London", lives, valid_at=back_from)
        return await close(*args, **kw)

    monkeypatch.setattr(projections, "close_triples", _back_to_london_first)
    await _stated(ch, org, "s2", "I currently live in Rome", ("user", "live_in", "rome"))
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "london")}) == {("live_in", "london")}


async def test_another_relation_to_the_same_object_does_not_gainsay_a_successor(ch, org) -> None:
    was = "I currently live in London and work in London"
    paris = await _stated(
        ch, org, "s1", "I currently live in Paris and work in London", ("user", "live_in", "paris")
    )
    london = await _stated(ch, org, "s2", was, extracted=False)
    await _follow_retirement(ch, org, paris, london)
    works = ("user", "work_in", "london")
    await _stated(ch, org, "s2", was + ".", works, memory_id=london["memory_id"])
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    # Berlin starts May 1, before the undated London seen today: the repair closes it.
    want = {("live_in", "london"), ("work_in", "london")}
    assert await _open_facts(ch, org, want) == want


async def test_a_name_in_another_script_is_carried_through_a_revision(ch, org) -> None:
    acme = await _stated(ch, org, "s1", "Bob is CEO of Acme", ("bob", "ceo_of", "acme"))
    bob = await _stated(ch, org, "s2", "Bob is CEO of Компания", extracted=False, pinned=1)
    await _follow_retirement(ch, org, acme, bob)
    await _stated(
        ch, org, "s2", "Bob is CEO of Компания!", extracted=False, memory_id=bob["memory_id"]
    )
    other = await _stated(ch, org, "s3", "Bob is CEO of Компания", ("bob", "ceo_of", "компания"))
    initech = await _stated(ch, org, "s4", "Bob is CEO of Initech", extracted=False)
    await _follow_retirement(ch, org, other, initech)
    await _mutations_done(ch, org)
    # Initech starts May 1, before the undated Компания seen today: the repair closes it.
    want = {"компания"}
    assert await _open_cities(ch, org, want) == want


@pytest.mark.parametrize(
    ("revised", "stated", "want"),
    [
        # The same words, and no relation saying otherwise: the successor holds, and
        # closes Berlin, which starts May 1, before the undated London seen today.
        ("I currently live in London.", (), {("live_in", "london")}),
        # More detail, which words alone cannot tell from a retraction: it closes.
        (
            "I currently live in London and work at Acme",
            (("user", "work_at", "acme"),),
            {("work_at", "acme"), ("live_in", "berlin")},
        ),
        # Names London, but its relations say Rome and a visit: the successor goes.
        (
            "I currently live in Rome and visited London",
            (("user", "live_in", "rome"), ("user", "visit", "london")),
            # Rome is closed by the later London row: `live_in` holds one value.
            {("visit", "london"), ("live_in", "berlin")},
        ),
    ],
)
async def test_a_revision_keeps_a_successor_only_while_its_relations_agree(
    ch, org, revised, stated, want
) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris", ("user", "live_in", "paris"))
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # Revised in place: London's residence is a successor the follow wrote for it.
    mid = london["memory_id"]
    if stated:
        await _stated(ch, org, "s2", revised, stated[0], also=stated[1:], memory_id=mid)
    else:
        await _stated(ch, org, "s2", revised, extracted=False, memory_id=mid)
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    # London's close of Rome lands first: Berlin's repair reads stored rows, and an
    # undated Rome still open would be newer than Berlin.
    await _mutations_done(ch, org)
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, want) == want
    # What a reader is told: London is still where the user lives, or no longer. The
    # wait above for `valid_to IS NULL` comes first: facts.list alone still lists a
    # fact closed this very second.
    lives = {o for r, o in want if r == "live_in"}
    assert ("london" in await _listed(org, "live_in", lives)) == ("london" in lives)


async def test_a_successors_repair_reads_the_close_its_follow_queued(ch, org) -> None:
    # The follow closes London, then repairs Berlin's interval. With that close still a
    # pending mutation (as under load), London read as open is newer than Berlin,
    # dated May 1: Berlin must not close behind it.
    london = await _stated(
        ch, org, "s1", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s2", "I currently live in Berlin", extracted=False)
    await ch.execute("SYSTEM STOP MERGES loom.entity_triples")  # no mutation runs
    try:
        await _follow_retirement(ch, org, london, berlin, wait=False)
    finally:
        await ch.execute("SYSTEM START MERGES loom.entity_triples")
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("live_in", "berlin")}) == {("live_in", "berlin")}
    # The follow's own Berlin, current since May 1: not closed and stated again later.
    rows = await ch.query_all(
        "SELECT valid_from FROM loom.entity_triples WHERE org = {org:String}"
        " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"valid_from": datetime(2026, 5, 1)}]


async def test_a_live_row_holds_the_successor_written_for_its_value(ch, org) -> None:
    # Its extractor names the residence another way: the follow's successor is still its.
    paris = await _stated(ch, org, "s1", "I currently live in Paris", ("user", "live_in", "paris"))
    london = await _stated(
        ch, org, "s2", "I currently live in London", ("user", "reside_in", "london")
    )
    await _follow_retirement(ch, org, paris, london)
    other = await _stated(
        ch, org, "s3", "I currently live in London", ("user", "live_in", "london")
    )
    berlin = await _stated(ch, org, "s4", "I currently live in Berlin", extracted=False)
    await _follow_retirement(ch, org, other, berlin)
    await _mutations_done(ch, org)
    # Berlin starts May 1, before the undated London seen today: the repair closes it.
    want = {("resid_in", "london"), ("live_in", "london")}
    assert await _open_facts(ch, org, want) == want


async def test_a_revision_whose_links_fail_to_write_still_holds_its_successor(
    ch, org, monkeypatch
) -> None:
    from server.core import ch as ch_mod
    from server.memory.app.projection_errors import TriplesInsertFailed

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False, pinned=1)
    await _follow_retirement(ch, org, paris, london)

    client = ch_mod.get_client()
    insert, attempted = client.insert, []

    async def _flaky(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triple_rows":
            attempted.append(rows)
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", _flaky)
    lives = ("user", "favourite_city", "london")
    # Raised with the write's arrival, for its replay to date the triples by.
    with pytest.raises(TriplesInsertFailed, match="ConnectionError"):
        await _stated(
            ch, org, "s2", "I currently live in London.", lives, memory_id=london["memory_id"]
        )
    monkeypatch.setattr(client, "insert", insert)
    assert attempted
    # Its successor was written for the value it was revised from: the same words.
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london", "rome"}) == {"london", "rome"}
    assert await _listed(org, "favourite_city", {"london", "rome"}) == {"london", "rome"}


async def test_a_replay_inserting_the_same_graph_rows_again_lands_them_once(ch, org) -> None:
    from server.memory.app.projections import entity_triple_row, graph_insert_settings

    now = naive_utc_now()
    row = entity_triple_row(
        org,
        NS,
        "user",
        "lives_in",
        "paris",
        provenance="extracted",
        confidence=1.0,
        adapter_name="t",
        last_seen=now.replace(microsecond=0),
        seen_at=now,
    )
    settings = graph_insert_settings("entity_triples", wait=True, dedup_token=f"replay:{org}")
    for _ in range(2):
        await ch.insert("loom.entity_triples", [row], settings=settings)
    (got,) = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert got["n"] == 1


async def test_a_replayed_insert_names_its_triples_as_the_write_resolved_them(
    ch, org, monkeypatch
) -> None:
    # London's triple lands, its key links fail; London then resolves to another name.
    # The replay must repair the London that landed, so Paris closes.
    from server.core import ch as ch_mod
    from server.memory.app import projections
    from server.memory.app.projection_errors import (
        TriplesInsertFailed,
        bulk_projection_payload,
        record_input_projection_failure,
    )

    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in Paris", (*live, "paris"))
    _row, write, results = await _row_and_write(
        ch, org, "s2", "I live in London", [(*live, "london")], memory_key="k2"
    )
    names: dict[str, str] = {}

    async def resolve(input: Any, entity_texts: list[str], *, ch: Any) -> dict[str, str]:
        return names

    client = ch_mod.get_client()
    insert, landed = client.insert, asyncio.Event()

    async def links_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triple_memories":
            await landed.wait()
            raise ConnectionError("transient")
        out = await insert(table, rows, **kw)
        if table == "loom.entity_triples":
            landed.set()
        return out

    monkeypatch.setattr(projections, "resolve_projection_entity_map", resolve)
    monkeypatch.setattr(client, "insert", links_fail)
    with pytest.raises(TriplesInsertFailed) as raised:
        await projections.write_entity_triples(write, results, ch=ch)
    monkeypatch.setattr(client, "insert", insert)
    await record_input_projection_failure(
        write,
        projection="entity_triples",
        error=raised.value,
        payload=bulk_projection_payload(write, results, error=raised.value),
        source_operation="memory.bulk_set",
    )
    failure = await _recorded_reconcile(ch, org, "entity_triples")
    names["london"] = "london city"
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_replay_that_inserted_never_inserts_again_after_a_failed_repair(
    ch, org, monkeypatch
) -> None:
    # The write's triples insert fails. Its replay inserts them, then its repair fails,
    # and the process stops before it writes anything else. The failure must already say
    # the triples landed: the next replay repairs them and never inserts them again,
    # whatever the dedup window has forgotten since.
    from server.core import ch as ch_mod
    from server.memory.app import projection_failures, projections
    from server.memory.app.projection_errors import (
        TRIPLES_INSERTED_AT,
        TriplesInsertFailed,
        bulk_projection_payload,
        record_input_projection_failure,
    )
    from server.memory.app.projection_failures import (
        list_projection_failures,
    )

    _row, write, results = await _row_and_write(
        ch, org, "s1", "I live in London", [("user", "live_in", "london")], memory_key="k1"
    )
    client = ch_mod.get_client()
    insert = client.insert
    inserted: list[str] = []

    async def triples_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triples":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    async def counting(table: str, rows: Any, **kw: Any) -> Any:
        inserted.append(table)
        return await insert(table, rows, **kw)

    async def fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("down")

    monkeypatch.setattr(client, "insert", triples_fail)
    with pytest.raises(TriplesInsertFailed) as raised:
        await projections.write_entity_triples(write, results, ch=ch, entity_map={})
    monkeypatch.setattr(client, "insert", insert)
    await record_input_projection_failure(
        write,
        projection="entity_triples",
        error=raised.value,
        payload=bulk_projection_payload(write, results, error=raised.value),
        source_operation="memory.bulk_set",
    )
    failure = await _recorded_reconcile(ch, org, "entity_triples")

    # The replay inserts, its repair fails, and nothing it would write next lands.
    with monkeypatch.context() as stopped:
        stopped.setattr(projections, "_repair_contradictory_triples", fails)
        stopped.setattr(projection_failures, "record_projection_failure", fails)
        stopped.setattr(projection_failures, "mark_projection_failures_resolved", fails)
        assert not (await _retry_one(failure, ch)).resolved

    (failure,) = await list_projection_failures(org=org, projection="entity_triples", ch=ch)
    assert failure.payload[TRIPLES_INSERTED_AT]
    # The build before inserts the `entries` of an `entity_triples` failure.
    assert "entries" not in failure.payload
    monkeypatch.setattr(client, "insert", counting)
    assert (await _retry_one(failure, ch)).resolved
    monkeypatch.setattr(client, "insert", insert)
    assert "loom.entity_triples" not in inserted
    await _mutations_done(ch, org)
    assert await _open_weights(ch, org, "london") == {"triples": 1, "edges": 0}
    # The retry marks the failure resolved with an ALTER UPDATE, a mutation that lands
    # in the background: the active list empties once it has.
    for _ in range(300):
        if not await list_projection_failures(org=org, ch=ch):
            break
        await asyncio.sleep(0.1)
    assert await list_projection_failures(org=org, ch=ch) == []


@pytest.mark.parametrize(("copies", "want"), [(1, {"rome"}), (2, {"london", "rome"})])
async def test_a_successor_is_judged_by_an_unredacted_copy_of_its_value(
    ch, org, copies, want
) -> None:
    from server.memory import store as M
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.store.redaction import redact_versions

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    # A pin or a touch writes another version with the same value and hash.
    versions = [london] + [{**london, "version": M.next_version()} for _ in range(copies - 1)]
    await M.insert_memory_versions(versions[1:], ch=ch)
    value = "I currently live in London."
    updated = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=str(london["memory_id"]), value=value)
    )
    # The latest copy of the value the successor was written for is scrubbed; an
    # earlier copy, if any, still says it.
    when = datetime.now(UTC).replace(tzinfo=None)
    mid = str(london["memory_id"])
    key = (
        str(london["org"]),
        str(london["namespace"]),
        str(london["agent"]),
        str(london["user_id"]),
        str(london["session_id"]),
        str(london["memory_key"]),
    )
    await redact_versions(
        key,
        mid,
        versions[-1]["version"],
        when=when,
        reason_blob="t",
        head=(mid, int(updated.new_version)),  # the key's live head: the successor
        ch=ch,
    )
    other = await _stated(ch, org, "s3", "I currently live in London")
    rome = await _stated(ch, org, "s4", "I currently live in Rome", extracted=False)
    await _follow_retirement(ch, org, other, rome)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, want) == want


# --- Which rows may hold graph facts: one rule (live, not deleted, not expired, validity
# not ended, not 'high') on every path that asks it.


async def test_a_winner_revised_to_high_projects_nothing_when_the_follow_replays(ch, org) -> None:
    from types import SimpleNamespace

    from server.memory import store as M
    from server.memory.app.triple_follow import follow_row_supersessions

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    retired = {**paris, "version": M.next_version(), "status": "superseded"}
    retired["superseded_by"] = str(london["memory_id"])
    await M.insert_memory_versions([retired], ch=ch)
    # Before the follow replays, the winner is revised to a protected value.
    secret = {**london, "version": M.next_version(), "sensitivity": "high"}
    secret["value"] = '"I currently live in Secretville"'
    await M.insert_memory_versions([secret], ch=ch)
    scope = SimpleNamespace(org=org, namespace=NS)
    await follow_row_supersessions(
        scope, [(str(paris["memory_id"]), str(london["memory_id"]))], ch=ch
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


@pytest.mark.parametrize("ended", ["expires_at", "valid_to"])
async def test_an_ended_sibling_does_not_keep_a_fact_open(ch, org, ended) -> None:
    paris = await _stated(ch, org, "s1", "My favourite city is Paris")
    await _stated(ch, org, "s3", "My favourite city is Paris", **{ended: _past()})
    london = await _stated(ch, org, "s2", "My favourite city is London")
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_successor_closes_the_older_value_a_sibling_still_states(ch, org) -> None:
    # Another live row still says Paris, so the follow keeps it; London is the newer
    # value of a single-valued relation and closes it, as a write of London would.
    lives, april = ("user", "lives_in", "paris"), datetime(2026, 4, 1)
    paris = await _stated(ch, org, "s1", "I currently live in Paris", lives, valid_at=april)
    await _stated(ch, org, "s3", "I currently live in Paris", lives, valid_at=april)
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_a_clause_the_winner_keeps_word_for_word_keeps_its_end(ch, org) -> None:
    # The winner has no link to London's fact, but still says it in the same words.
    december = naive_utc_now().replace(microsecond=0) + timedelta(days=60)
    lives = ("user", "live_in", "london")
    acme = await _stated(
        ch,
        org,
        "s1",
        "I live in London until December. I work at Acme",
        lives,
        fact_valid_to=december,
    )
    was = "I live in London until December. I work at Initech"
    initech = await _stated(ch, org, "s2", was, extracted=False)
    await _follow_retirement(ch, org, acme, initech)
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " AND object = 'london' SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [december]


async def test_an_ended_live_end_of_the_chain_holds_nothing(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(
        ch, org, "s2", "I currently live in London", extracted=False, expires_at=_past()
    )
    await _follow_retirement(ch, org, paris, london)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_revision_closes_the_inferred_fact_it_no_longer_states(ch, org) -> None:
    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    await _revise("memory.update_by_id", org, london, "I currently live in Berlin")
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_bulk_revision_closes_the_inferred_fact_it_no_longer_states(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    paris = await _stated(ch, org, "s1", "I currently live in Paris")
    london = await _stated(ch, org, "s2", "I currently live in London", extracted=False)
    await _follow_retirement(ch, org, paris, london)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            entries=[{"memory_key": KEY, "value": "I currently live in Berlin"}],
        )
    )
    assert [r.memory_id for r in out.written] == [str(london["memory_id"])]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


@pytest.mark.parametrize("late", [True, False])
async def test_a_projection_landing_after_a_revision_leaves_no_edge(
    ch, org, monkeypatch, late
) -> None:
    # The first write's edges are written beside its response; held back past the
    # revision (`late`), they must not walk once they land.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, projections
    from server.memory.app.write import handle_memory_set

    write_edges, release = projections.write_entity_edges, asyncio.Event()
    calls: list[str] = []

    async def _first_held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if late and len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    monkeypatch.setattr(projections, "write_entity_edges", _first_held)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    if not late:
        await after_write.drain()
    await _set("Alice likes jazz", [])
    release.set()
    await after_write.drain()
    await _mutations_done(ch, org)
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


# Episodic and profile rows are not embedded: their write takes the plain insert path.
@pytest.mark.parametrize("memory_type", ["semantic", "episodic", "profile"])
async def test_a_first_write_paused_past_a_conditional_revision_leaves_no_fact(
    ch, org, monkeypatch, memory_type
) -> None:
    # London's first write stops before its edges; a conditional revision to jazz
    # completes; then London resumes. Its edges and its stated relation must not walk.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, projections
    from server.memory.app.write import handle_memory_set

    write_edges, release, held = projections.write_entity_edges, asyncio.Event(), []

    async def _london_held(input: Any, *args: Any, **kw: Any) -> Any:
        if "London" in str(input.entries[0].value):
            held.append(True)
            await release.wait()
        return await write_edges(input, *args, **kw)

    monkeypatch.setattr(projections, "write_entity_edges", _london_held)

    def _set(value: str, relations: list[Any], **extra: Any) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                memory_type=memory_type,
                value=value,
                relations=relations,
                **extra,
            )
        )

    london = asyncio.create_task(
        _set(
            "Alice lives in London", [("alice", "live_in", "london")], entities=["alice", "london"]
        )
    )
    for _ in range(100):
        if held:
            break
        await asyncio.sleep(0.05)
    assert held
    (row,) = await ch.query_all(
        "SELECT toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} AND memory_key = 'alice'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    jazz = await _set(
        "Alice likes jazz", [], entities=["alice", "jazz"], if_match_sha256=row["sha"]
    )
    release.set()
    await london
    await after_write.drain()
    await _mutations_done(ch, org)
    (stored,) = await ch.query_all(
        "SELECT toString(content_sha256) AS sha FROM loom.memory FINAL"
        " WHERE org = {org:String} AND memory_key = 'alice'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert stored["sha"] == jazz.content_sha256
    assert await _walked(org, [], labeled=True, seed="alice") == []
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


# A profile row is not embedded: its write takes the plain insert path.
@pytest.mark.parametrize("memory_type", ["semantic", "profile"])
async def test_a_revisions_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch, memory_type
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, projections
    from server.memory.app.write import handle_memory_set

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                memory_type=memory_type,
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in Paris", [("alice", "live_in", "paris")])
    await after_write.drain()
    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        projections,
        lambda: _set("Alice lives in London", [("alice", "live_in", "london")]),
        lambda: _set("Alice likes jazz", []),
    )


async def test_an_update_by_ids_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch
) -> None:
    # As above, through memory.update_by_id.
    from server.memory.api.models_crud import MemorySetInput, MemoryUpdateByIdInput
    from server.memory.app import projections
    from server.memory.app.update import handle_memory_update_by_id
    from server.memory.app.write import handle_memory_set

    first = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice lives in Paris",
            relations=[("alice", "live_in", "paris")],
        )
    )

    def _update(value: str, relations: list[Any]) -> Any:
        return handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=org, memory_id=str(first.memory_id), value=value, relations=relations
            )
        )

    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        projections,
        lambda: _update("Alice lives in London", [("alice", "live_in", "london")]),
        lambda: _update("Alice likes jazz", []),
    )


async def test_a_new_key_written_twice_in_a_bulk_keeps_one_row_and_one_edge(ch, org) -> None:
    # A key with no row yet, written twice in one bulk: one row, revised in the batch,
    # and the first value's edge ends with it.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set

    def _entry(city: str) -> dict[str, Any]:
        return {
            "memory_key": "alice",
            "value": f"Alice likes {city}",
            "entities": ["alice", city.lower()],
        }

    out = await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            entries=[_entry("London"), _entry("Rome")],
        )
    )
    await after_write.drain()
    assert len({r.memory_id for r in out.written if r.version}) == 1
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second the edge ended in
    walked = await _walked(org, ["rome"], labeled=False, seed="alice", until=lambda w: "rome" in w)
    assert "rome" in walked and "london" not in walked


async def test_an_overtaken_version_ends_its_edge_though_its_row_holds_another(ch, org) -> None:
    # A bulk can revise one row twice: asked about both versions while the row holds
    # the second, the check must still end the first's edge (it asks by value, not by row).
    from server.memory import store as M
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.edge_repair import end_overtaken_edges
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice likes London",
            entities=["alice", "london"],
        )
    )
    await after_write.drain()
    london = await M.get_by_id(
        str(out.memory_id), org=org, ch=ch, scope=WHOLE_NAMESPACE, namespace=NS
    )
    rome = M.build_memory_version_row(
        london,
        version=M.next_version(),
        value=json.dumps("Alice likes Rome"),
        content_sha256=hashlib.sha256(json.dumps("Alice likes Rome").encode()).hexdigest(),
        updated_at=naive_utc_now(),
    )
    await M.insert_memory_versions([rome], ch=ch)
    assert "london" in await _walked(org, ["london"], labeled=False, seed="alice")
    scope = london.model_copy()
    versions = [
        (str(london.memory_id), "alice", london.content_sha256),
        (str(london.memory_id), "alice", rome["content_sha256"]),
    ]
    await end_overtaken_edges(scope, versions, op="memory.bulk_set", ch=ch)
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # past the second the edge ended in
    assert "london" not in await _walked(org, [], labeled=False, seed="alice")


async def test_a_bulk_revisions_edges_landing_after_the_next_revision_end_with_it(
    ch, org, monkeypatch
) -> None:
    # As above, through memory.bulk_set.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import bulk_set

    def _set(value: str, city: str) -> Any:
        return bulk_set.handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                entries=[
                    {
                        "memory_key": "alice",
                        "value": value,
                        "entities": ["alice", city],
                        "relations": [("alice", "likes", city)],
                    }
                ],
            )
        )

    await _set("Alice likes Paris", "paris")
    await _london_ends_with_the_next_revision(
        ch,
        org,
        monkeypatch,
        bulk_set,
        lambda: _set("Alice likes London", "london"),
        lambda: _set("Alice likes jazz", "jazz"),
    )


async def test_a_late_projection_whose_cleanup_failed_is_closed_by_the_repair(
    ch, org, monkeypatch
) -> None:
    # As above with the edges landing late, but the cleanup that ends them fails
    # once: it is recorded, and the projection repair ends them.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, edge_repair, projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        retry_projection_failures,
    )
    from server.memory.app.write import handle_memory_set

    write_edges, release = projections.write_entity_edges, asyncio.Event()
    end_edges_of, failures = edge_repair._end_edges_of, ["once"]
    calls: list[str] = []

    async def _held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    monkeypatch.setattr(projections, "write_entity_edges", _held)
    monkeypatch.setattr(edge_repair, "_end_edges_of", _fails_once)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    await _set("Alice likes jazz", [])
    release.set()
    await after_write.drain()
    (failure,) = await list_projection_failures(
        org=org, projection="entity_edges_close", status="active", ch=ch
    )
    await _mutations_done(ch, org)
    assert "london" in await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "london" in w
    )
    await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked
    assert not await list_projection_failures(
        org=org, projection="entity_edges_close", status="active", ch=ch
    )


@pytest.mark.parametrize("cleanup", ["beside the write", "by the repair"])
async def test_a_late_projections_edge_ends_when_the_revision_ended_its_value(
    ch, org, monkeypatch, cleanup
) -> None:
    # London's edges land two seconds after the revision to jazz. Their cleanup, or
    # its replay, must end them at the revision, not when it runs: a walk as of a
    # moment in between must not reach London.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write, edge_repair, projections
    from server.memory.app.projection_failures import retry_projection_failures
    from server.memory.app.write import handle_memory_set

    write_edges, release = projections.write_entity_edges, asyncio.Event()
    end_edges_of = edge_repair._end_edges_of
    failures = ["once"] if cleanup == "by the repair" else []
    calls: list[str] = []

    async def _held(*args: Any, **kw: Any) -> Any:
        calls.append("edges")
        if len(calls) == 1:
            await release.wait()
        return await write_edges(*args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    monkeypatch.setattr(projections, "write_entity_edges", _held)
    monkeypatch.setattr(edge_repair, "_end_edges_of", _fails_once)

    def _set(value: str, relations: list[Any]) -> Any:
        return handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                memory_key="alice",
                value=value,
                relations=relations,
            )
        )

    await _set("Alice lives in London", [("alice", "live_in", "london")])
    await _set("Alice likes jazz", [])
    revised = naive_utc_now()
    await asyncio.sleep(2.5)
    release.set()
    await after_write.drain()
    if cleanup == "by the repair":
        await asyncio.sleep(1)
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    between = revised + timedelta(seconds=1.2)
    walked = await _walked(
        org,
        [],
        labeled=False,
        as_of=between,
        seed="alice",
        until=lambda w: "jazz" in w and "london" not in w,
    )
    assert "jazz" in walked and "london" not in walked
    # Jazz states no relation: the labeled walk reaches nothing then.
    assert await _walked(org, [], labeled=True, as_of=between, seed="alice") == []
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "london" not in w
    )
    assert "jazz" in walked and "london" not in walked


@pytest.mark.parametrize("cleanup", ["beside the write", "by the repair"])
async def test_a_late_projections_edge_ends_when_a_bulk_retired_its_row(
    ch, org, monkeypatch, cleanup
) -> None:
    # The retirement-history case of the test above: London's edges land after a
    # later bulk write under another session retired its row. A retirement keeps the
    # row's updated_at, so the edges end at the retirement, not at London's write: a
    # walk between the two still reaches London, a walk after it does not.
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import (
        after_write,
        bulk_set,
        conflicts,
        edge_repair,
        projections,
    )
    from server.memory.app.projection_failures import (
        list_projection_failures,
        retry_projection_failures,
    )
    from server.memory.app.write import handle_memory_set

    write_edges, release, held = projections.write_entity_edges, asyncio.Event(), []
    end_edges_of = edge_repair._end_edges_of
    failures = ["once"] if cleanup == "by the repair" else []

    async def _london_held(input: Any, *args: Any, **kw: Any) -> Any:
        if "London" in str(input.entries[0].value):
            held.append(True)
            await release.wait()
        return await write_edges(input, *args, **kw)

    async def _fails_once(*args: Any, **kw: Any) -> Any:
        if failures:
            failures.pop()
            raise RuntimeError("transient")
        return await end_edges_of(*args, **kw)

    async def _newest_wins(org: str, namespace: str, ch: Any = None) -> str:
        return "newest_wins"  # a later write to the key retires the older rows

    monkeypatch.setattr(projections, "write_entity_edges", _london_held)
    monkeypatch.setattr(edge_repair, "_end_edges_of", _fails_once)
    monkeypatch.setattr(conflicts, "namespace_conflict_policy", _newest_wins)
    written = naive_utc_now()
    london = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            memory_key="alice",
            value="Alice lives in London",
            entities=["alice", "london"],
        )
    )
    await asyncio.sleep(2.5)  # the retirement well after London's write
    await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s2",
            entries=[
                {"memory_key": "alice", "value": "Alice likes jazz", "entities": ["alice", "jazz"]}
            ],
        )
    )
    (retired,) = await ch.query_all(
        "SELECT invalid_at FROM loom.memory FINAL WHERE org = {org:String}"
        " AND memory_id = {id:UUID} AND status = 'superseded'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(london.memory_id)},
    )
    await asyncio.sleep(2.5)  # London's edges land after the retirement
    assert held
    release.set()
    await after_write.drain()
    if cleanup == "by the repair":
        (failure,) = await list_projection_failures(
            org=org, projection="entity_edges_close", status="active", ch=ch
        )
        assert failure.payload["ended_at"] == retired["invalid_at"].isoformat()
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    await _mutations_done(ch, org)
    before = written + timedelta(seconds=1.2)
    assert "london" in await _walked(
        org, [], labeled=False, as_of=before, seed="alice", until=lambda w: "london" in w
    )
    after = retired["invalid_at"] + timedelta(seconds=1.2)
    walked = await _walked(
        org, [], labeled=False, as_of=after, seed="alice", until=lambda w: "london" not in w
    )
    assert "london" not in walked


async def test_a_late_cleanup_keeps_the_end_another_row_gave_the_pair(ch, org) -> None:
    # Alice and London have two edge rows: one another row closed ten minutes after
    # the revision, and the late projection's own open one. The late cleanup ends
    # the open one at the revision and leaves the closed one's end alone.
    from server.memory.app.edge_repair import close_late_edges
    from server.memory.app.projections import entity_edge_row

    revised = naive_utc_now().replace(microsecond=0) - timedelta(hours=1)
    started = revised - timedelta(hours=1)
    other_end = revised + timedelta(minutes=10)
    closed = entity_edge_row(
        org, NS, "alice", "london", last_seen=started, valid_from=started, valid_to=other_end
    )
    late = entity_edge_row(org, NS, "alice", "london", last_seen=started, valid_from=started)
    await ch.insert("loom.entity_edges", [closed, late], settings={"wait_for_async_insert": 1})
    memory_id, sha = str(uuid.uuid4()), "a" * 64
    link = {
        "org": org,
        "namespace": NS,
        "memory_id": memory_id,
        "user_id": USER,
        "content_sha256": sha,
        "entity_a": "alice",
        "entity_b": "london",
    }
    await ch.insert("loom.entity_edge_rows", [link], settings={"wait_for_async_insert": 1})
    await close_late_edges(org, NS, memory_id, sha, ended_at=revised, ch=ch)
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " ORDER BY valid_to SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [revised, other_end]


@pytest.mark.parametrize(
    "values",
    [
        ["Alice lives in London", "Alice likes jazz"],
        ["Alice lives in London", "Alice likes jazz", "Alice lives in London", "Alice likes blues"],
    ],
    ids=["once", "held-again"],
)
async def test_a_value_ends_when_the_row_last_stopped_holding_it(ch, org, values) -> None:
    # A row held London, then other values, maybe London again in between: its
    # London ended with the last revision away from it, not the first.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.edge_repair import value_ended_at
    from server.memory.app.write import handle_memory_set

    for i, value in enumerate(values):
        if i:
            await asyncio.sleep(1.1)  # one version per second of updated_at
        out = await handle_memory_set(
            MemorySetInput(
                org=org, namespace=NS, agent="bot", user_id=USER, memory_key="alice", value=value
            )
        )
    await after_write.drain()
    memory_id = str(out.memory_id)
    versions = await ch.query_all(
        "SELECT content_sha256, updated_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND memory_id = {id:UUID} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": memory_id},
    )
    assert len(versions) == len(values)
    london = versions[0]["content_sha256"].decode()
    ended = await value_ended_at(org, NS, memory_id, london, ch=ch)
    assert ended == versions[-1]["updated_at"]


async def test_a_value_a_retirement_ended_ends_at_the_retirement(ch, org) -> None:
    # A retirement keeps the row's updated_at and stamps invalid_at: the value ended
    # when it was retired, not when it was written.
    from server.memory import store as M
    from server.memory.app.edge_repair import value_ended_at

    row = _row(org, "s1", "User lives in London", None, **_written_earlier())
    await M.insert_memory_versions([row], ch=ch)
    assert await M.supersede_duplicates(
        org, [{**row, "superseded_by": "winner"}], ch=ch, strong=True, namespace=NS
    )
    versions = await ch.query_all(
        "SELECT updated_at, invalid_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND memory_id = {id:UUID} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(row["memory_id"])},
    )
    written, retired = versions[0]["updated_at"], versions[-1]["invalid_at"]
    assert retired is not None and retired > written
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == retired


async def test_a_value_an_undated_value_update_ended_ends_at_the_retirement(ch, org) -> None:
    # A value update whose newer row has no valid_at retires the older row without an
    # invalid_at, keeping its updated_at: the value ended when it was retired.
    from server.memory import store as M
    from server.memory.app.edge_repair import value_ended_at
    from server.memory.app.value_updates import _supersession_update

    row = _row(org, "s1", "price is $10", None, **_written_earlier())
    await M.insert_memory_versions([row], ch=ch)
    retiring = naive_utc_now().replace(microsecond=0)
    update = _supersession_update("newer", None)
    assert await M.write_versions_by_ids(
        org, [str(row["memory_id"])], update, ch=ch, strong=True, namespace=NS
    )
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended >= retiring > row["updated_at"]


async def test_a_value_a_back_dated_value_update_ended_ends_at_its_date(ch, org) -> None:
    # A value update retires the older row as of the newer row's valid_at, which is
    # usually before the retirement was written: that date is the value's end.
    from server.memory import store as M
    from server.memory.app.edge_repair import value_ended_at
    from server.memory.app.value_updates import _supersession_update

    row = _row(org, "s1", "price is $10", datetime(2026, 1, 1))
    await M.insert_memory_versions([row], ch=ch)
    newer_valid_at = datetime(2026, 3, 1)
    update = _supersession_update("newer", newer_valid_at)
    assert await M.write_versions_by_ids(
        org, [str(row["memory_id"])], update, ch=ch, strong=True, namespace=NS
    )
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == newer_valid_at


async def test_a_value_a_by_id_tombstone_ended_ends_at_the_tombstone(ch, org) -> None:
    # A by-id tombstone (a conflict's losing side, a blacklist sweep) keeps the row's
    # updated_at and stamps only tombstoned_at.
    from server.memory import store as M
    from server.memory.app.edge_repair import value_ended_at

    row = _row(org, "s1", "User lives in London", None, **_written_earlier())
    await M.insert_memory_versions([row], ch=ch)
    when = naive_utc_now().replace(microsecond=0)
    assert await M.tombstone_cascade_by_ids(
        org, [str(row["memory_id"])], when=when, ch=ch, namespace=NS
    )
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == when


async def test_a_restored_tombstone_is_not_left_retired(ch, org) -> None:
    from server.memory import store as M

    row = await _restored_dependent(ch, org)
    head = await M.get_by_id(
        str(row["memory_id"]), org=org, ch=ch, strong=True, scope=WHOLE_NAMESPACE, namespace=NS
    )
    assert head.tombstoned_at is None and head.invalid_at is None


async def test_a_value_revised_after_a_restore_ends_at_the_revision(ch, org) -> None:
    # The tombstone's invalid_at must not date the end of a value the row held again.
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app.edge_repair import value_ended_at
    from server.memory.app.update import handle_memory_update_by_id

    row = await _restored_dependent(ch, org)
    await asyncio.sleep(1.1)  # the revision a second after the tombstone
    revised = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(org=org, memory_id=str(row["memory_id"]), value="User lives in Rome")
    )
    assert revised.updated
    (version,) = await ch.query_all(
        "SELECT updated_at FROM loom.memory_versions"
        " WHERE org = {org:String} AND version = {v:UInt64}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "v": revised.new_version},
    )
    ended = await value_ended_at(org, NS, str(row["memory_id"]), row["content_sha256"], ch=ch)
    assert ended == version["updated_at"]


@pytest.mark.parametrize(
    "extra",
    [
        {},
        {"proposed": True},
        {"valid_at": datetime(2019, 1, 1), "valid_to": datetime(2020, 1, 1)},
    ],
    ids=["plain", "proposed", "closed-in-the-past"],
)
async def test_a_first_write_still_holding_its_value_projects_its_edge(ch, org, extra) -> None:
    # The fence asks only whether the row still holds this value: a proposal
    # or a fact dated in the past gets its edge, as an active row does.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set

    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            value="Alice met Bob in Paris",
            **extra,
        )
    )
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edge_rows "
        "WHERE org = {org:String} AND memory_id = {id:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(out.memory_id)},
    )
    assert int(rows[0]["n"]) > 0


async def test_a_plain_first_write_projects_its_edge_fenced_on_its_value(
    ch, org, monkeypatch
) -> None:
    # A profile row takes the plain insert path. Its first write's edges land beside
    # the response, flushed, and fenced as a semantic row's are: written only once the
    # row is asked whether it still holds the value.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import (
        after_write,
        graph_holders,
    )
    from server.memory.app.write import handle_memory_set

    rows_holding, asked = graph_holders.rows_holding, []

    async def _counted(*a: Any, **k: Any) -> Any:
        asked.append(True)
        return await rows_holding(*a, **k)

    monkeypatch.setattr(graph_holders, "rows_holding", _counted)
    out = await handle_memory_set(
        MemorySetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            memory_key="alice",
            memory_type="profile",
            value="Alice met Bob in Paris",
        )
    )
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_edge_rows "
        "WHERE org = {org:String} AND memory_id = {id:UUID} "
        "SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": str(out.memory_id)},
    )
    assert int(rows[0]["n"]) > 0
    assert asked


async def test_a_replayed_repair_never_closes_a_newer_value(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Rome's repair fails; Paris then closes London and Rome. The replay of Rome's
    # repair is dated at Rome's write: it closes nothing Paris stated, nor retires it.

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_repair_contradictory_triples",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    # Paris arrives after Rome's write, in its second or a later one.
    paris = await _stated(ch, org, "s3", "I moved to Paris", (*live, "paris"), memory_key="k3")
    await _mutations_done(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _status(ch, org, paris) == "active"
    assert await _status(ch, org, london) == "superseded"


@pytest.mark.parametrize(
    "step",
    [
        "close_triples",
        "_undo_raced_closes",
        "_repair_contradictory_edges",
        "_retire_memories_behind_repairs",
    ],
)
async def test_a_replay_finishes_a_repair_that_failed_after_its_close(
    ch, org, monkeypatch, newest_wins, step
) -> None:
    # Rome's close of London was issued, a later step did not run: the replay closes
    # London if that close never landed, and retires the row behind it.

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch, org, monkeypatch, step, "I moved to Rome", (*live, "rome"), "k2"
    )
    await _mutations_done(ch, org)
    assert await _status(ch, org, london) == "active"
    assert (await _retry_one(failure, ch)).resolved
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "superseded"


async def test_a_replay_that_fails_after_its_close_is_finished_by_the_next(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Rome's repair fails before it closes anything. Its replay closes London and then
    # fails to retire: the next replay, finding London closed, still retires its row.
    from server.memory.app import projections
    from server.memory.app.projection_failures import (
        list_projection_failures,
        record_projection_failure,
    )

    live = ("user", "live_in")
    london = await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_repair_contradictory_triples",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    await record_projection_failure(
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        memory_keys=["k2"],
        error=failure.error_message,
        payload=failure.payload,
        ch=ch,
    )

    async def _stored() -> Any:
        for _ in range(100):
            stored = await list_projection_failures(org=org, projection="entity_triples", ch=ch)
            if stored:
                return stored[0]
            await asyncio.sleep(0.1)
        pytest.fail("the failure log never flushed")

    async def _fails(*a: Any, **k: Any) -> Any:
        raise RuntimeError("retire down")

    with monkeypatch.context() as patched:
        patched.setattr(projections, "_retire_memories_behind_repairs", _fails)
        assert not (await _retry_one(await _stored(), ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "active"
    assert (await _retry_one(await _stored(), ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"rome"}) == {"rome"}
    assert await _status(ch, org, london) == "superseded"


async def test_a_replay_leaves_a_value_stated_again_since(
    ch, org, monkeypatch, newest_wins
) -> None:
    # Back to London after Rome's retirement failed: London is current again, and
    # finishing Rome's repair would retire the row that just stated it.

    live = ("user", "live_in")
    await _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1")
    _, failure = await _failed_repair(
        ch,
        org,
        monkeypatch,
        "_retire_memories_behind_repairs",
        "I moved to Rome",
        (*live, "rome"),
        "k2",
    )
    await _mutations_done(ch, org)
    # Back to London arrives after Rome, in its second or a later one.
    back = await _stated(
        ch, org, "s3", "I moved back to London", (*live, "london"), memory_key="k3"
    )
    await _mutations_done(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _status(ch, org, back) == "active"


@pytest.mark.parametrize("dated", [False, True])
async def test_a_replay_reopens_a_value_stated_again_while_its_close_landed(
    ch, org, monkeypatch, newest_wins, dated
) -> None:
    # The replay opens London again instead of finishing Rome's close, also when London
    # is dated by its session day, before Rome's write.

    back_from = _today() if dated else None
    _, back, failure = await _rome_raced_by_back_to_london(ch, org, monkeypatch, back_from)
    assert await _open_cities(ch, org, set()) == set()
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    assert await _status(ch, org, back) == "active"


async def test_a_reopened_value_meets_the_value_stated_after_it(
    ch, org, monkeypatch, newest_wins
) -> None:
    # As above, and Paris is stated before the replay: it found no open London to
    # close. Opened again, London meets Paris as written in turn: Paris stays current
    # and the London rows retire behind it, as if Paris's write had closed London.

    london, back, failure = await _rome_raced_by_back_to_london(ch, org, monkeypatch)
    live = ("user", "live_in")
    paris = await _stated(ch, org, "s4", "I moved to Paris", (*live, "paris"), memory_key="k4")
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _status(ch, org, paris) == "active"
    assert await _status(ch, org, back) == "superseded"
    assert await _status(ch, org, london) == "superseded"


async def test_a_users_row_links_are_erased_after_their_memory_row_is_gone(ch, org) -> None:
    # A deleted row dropped by its TTL leaves no `loom.memory` row to find its links by.
    import uuid

    from server.erasure.store.census import residue_census
    from server.erasure.store.steps_graph import delete_graph_links_by_user

    link = {
        "org": org,
        "namespace": NS,
        "memory_id": str(uuid.uuid4()),
        "user_id": USER,
        "content_sha256": "0" * 64,
        "subject": "user",
        "relation": "live_in",
        "object": "paris",
        "inferred": False,
    }
    theirs = {**link, "memory_id": str(uuid.uuid4()), "user_id": "u2"}
    await ch.insert("loom.entity_triple_rows", [link, theirs])
    # The census counts by the same predicate the step deletes by.
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_triple_rows"] == 1
    await delete_graph_links_by_user(org, USER, ch=ch)
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_triple_rows"] == 0
    rows = await ch.query_all(
        "SELECT user_id FROM loom.entity_triple_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"user_id": "u2"}]


async def test_a_users_edge_row_links_are_erased(ch, org) -> None:
    # As the triple links: found by the writer they carry, not by their row.
    import uuid

    from server.erasure.store.census import residue_census
    from server.erasure.store.steps_graph import delete_graph_links_by_user

    link = {
        "org": org,
        "namespace": NS,
        "memory_id": str(uuid.uuid4()),
        "user_id": USER,
        "content_sha256": "0" * 64,
        "entity_a": "alice",
        "entity_b": "paris",
    }
    theirs = {**link, "memory_id": str(uuid.uuid4()), "user_id": "u2"}
    await ch.insert("loom.entity_edge_rows", [link, theirs])
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_edge_rows"] == 1
    await delete_graph_links_by_user(org, USER, ch=ch)
    assert (await residue_census(org, "user_id", USER, ch=ch))["loom.entity_edge_rows"] == 0
    rows = await ch.query_all(
        "SELECT user_id FROM loom.entity_edge_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert rows == [{"user_id": "u2"}]


async def test_a_replay_projects_nothing_for_a_value_revised_since(ch, org) -> None:
    # London's projection failed; the row was then revised to Berlin. The replay
    # finds the row no longer holds London and leaves Berlin current.
    import uuid

    from server.memory.app.projection_errors import bulk_projection_payload
    from server.memory.app.projection_failures import ProjectionFailureRow

    live = ("user", "live_in")
    london, write, results = await _row_and_write(
        ch, org, "s1", "I live in London", [(*live, "london")]
    )
    payload = bulk_projection_payload(write, results, error=RuntimeError("triples down"))
    await _revise("memory.update_by_id", org, london, "I live in Berlin", ((*live, "berlin"),))
    failure = ProjectionFailureRow(
        failure_id=str(uuid.uuid4()),
        org=org,
        namespace=NS,
        agent="bot",
        projection="entity_triples",
        source_operation="memory.bulk_set",
        source_resource_id="memory.bulk_set:s1:-",
        memory_keys=[KEY],
        error_type="RuntimeError",
        error_message="triples down",
        payload=json.loads(json.dumps(payload)),
    )
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"berlin"}) == {"berlin"}


async def test_a_fallback_asks_only_the_rows_holding_the_value(ch, org, monkeypatch) -> None:
    # A jazz row was linked to Paris for a value it no longer has. London's row drops
    # London: Paris is stated again through the row that says it, not the jazz row.
    from server.memory import store as M
    from server.memory.app import graph_undo
    from server.memory.app.projections import triple_row_link

    london = await _london_undone_over_paris(ch, org, "live_in")
    paris = await _row_saying(ch, org, "I live in Paris")
    jazz = _row(org, "s3", "I like jazz", datetime(2026, 5, 1), memory_key="k3")
    await M.insert_memory_versions([jazz], ch=ch)
    old = triple_row_link(
        org, NS, str(jazz["memory_id"]), "0" * 64, ("user", "live_in", "paris"), user_id=USER
    )
    await ch.insert("loom.entity_triple_rows", [old])
    reopen, asked = graph_undo.reopen_held_facts, []

    async def _asked(input: Any, ids: list[str], **kw: Any) -> None:
        if kw.get("adapter_name") == "row-fallback":
            asked.append(ids)
        await reopen(input, ids, **kw)

    monkeypatch.setattr(graph_undo, "reopen_held_facts", _asked)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    assert asked == [[paris["memory_id"]]]
    assert await _open_cities(ch, org, {"paris", "rome"}) == {"paris", "rome"}


async def test_a_fallback_leaves_out_a_row_revised_before_its_markers_land(
    ch, org, monkeypatch
) -> None:
    # London's row is revised away and Paris is picked to be current again; before its
    # markers land, the Paris row is revised to jazz. No live row says Paris.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    paris = await _row_saying(ch, org, "I live in Paris")
    restate = graph_undo.restate_facts

    async def _paris_revised_first(*a: Any, **k: Any) -> Any:
        monkeypatch.setattr(graph_undo, "restate_facts", restate)
        await _revise("memory.update_by_id", org, paris, "I like jazz", (("user", "like", "jazz"),))
        return await restate(*a, **k)

    monkeypatch.setattr(graph_undo, "restate_facts", _paris_revised_first)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("like", "rome"), ("like", "jazz")}
    assert await _open_facts(ch, org, want) == want


async def test_a_replayed_fallback_dates_its_triple_as_its_edge(ch, org, monkeypatch) -> None:
    # The fallback's edge marker lands and its triple marker fails. Replayed later, the
    # triple starts when the edge does, so both walks agree in between.
    from server.core import ch as ch_mod
    from server.memory.app import edge_repair, graph_holders, triple_projections

    london = await _london_undone_over_paris(ch, org, "live_in")
    await _edges_like_triples(ch, org)
    client = ch_mod.get_client()
    insert = client.insert

    async def _triples_fail(table: str, rows: Any, **kw: Any) -> Any:
        if table == "loom.entity_triples" and rows[0]["adapter_name"] == "row-fallback":
            raise ConnectionError("transient")
        return await insert(table, rows, **kw)

    monkeypatch.setattr(client, "insert", _triples_fail)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(client, "insert", insert)
    failure = await _recorded_reconcile(ch, org)
    later = naive_utc_now() + timedelta(seconds=30)
    monkeypatch.setattr(graph_holders, "naive_utc_now", lambda: later)
    monkeypatch.setattr(edge_repair, "naive_utc_now", lambda: later)
    monkeypatch.setattr(triple_projections, "naive_utc_now", lambda: later)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    starts = await ch.query_all(
        "SELECT 'triple' AS plane, valid_from FROM loom.entity_triples"
        " WHERE org = {org:String} AND adapter_name = 'row-fallback'"
        " UNION ALL SELECT 'edge', valid_from FROM loom.entity_edges"
        " WHERE org = {org:String} AND restore_id IN (SELECT restore_id"
        " FROM loom.entity_triples WHERE org = {org:String} AND adapter_name = 'row-fallback')"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert sorted(r["plane"] for r in starts) == ["edge", "triple"]
    assert len({r["valid_from"] for r in starts}) == 1


async def test_a_replayed_fallback_finishes_the_repair_its_markers_missed(
    ch, org, monkeypatch
) -> None:
    # The move to Berlin is ahead. London's row is revised away and Paris is stated
    # again; both its markers land but its interval repair fails. The replay finds the
    # markers and must still repair: Paris ends when Berlin starts.
    from server.memory.app import graph_undo

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)
    repair = graph_undo.repair_intervals

    async def _repair_fails(*a: Any, **k: Any) -> Any:
        raise ConnectionError("transient")

    monkeypatch.setattr(graph_undo, "repair_intervals", _repair_fails)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(graph_undo, "repair_intervals", repair)
    failure = await _recorded_reconcile(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    want = ["berlin", "rome"]
    assert await _walked(org, want, labeled=True, as_of=after) == want
    # Rome came after the edges were copied: it has no edge.
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_replayed_fallback_closes_the_edge_its_repair_missed(ch, org, monkeypatch) -> None:
    # As above, but the repair closes Paris's triple at Berlin's start and then fails
    # to close its edge. The replay closes the edge too: after Dec 1 neither walk
    # reaches Paris.
    from server.core import ch as ch_mod

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)
    client = ch_mod.get_client()
    execute, failed = client.execute, []

    async def _edge_close_fails(sql: str, *a: Any, **kw: Any) -> Any:
        # The repair's edge close names each edge by its key (char 31 joins its parts).
        if not failed and "ALTER TABLE loom.entity_edges" in sql and "char(31)" in sql:
            failed.append(sql)
            raise ConnectionError("transient")
        return await execute(sql, *a, **kw)

    monkeypatch.setattr(client, "execute", _edge_close_fails)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    monkeypatch.setattr(client, "execute", execute)
    assert failed
    failure = await _recorded_reconcile(ch, org)
    assert (await _retry_one(failure, ch)).resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    want = ["berlin", "rome"]
    assert await _walked(org, want, labeled=True, as_of=after) == want
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_second_replay_closes_the_edge_the_first_replays_repair_missed(
    ch, org, monkeypatch
) -> None:
    # The revision's reconcile fails before its fallback runs, so the failure records no
    # markers. Its first replay states Paris again and fails to close Paris's edge: the
    # closes it made must ride the failure, or the second replay finds the markers,
    # finds Paris's triple already closed, repairs nothing and leaves the edge open.
    from server.core import ch as ch_mod
    from server.memory.app import triple_projections

    live = ("user", "live_in")
    london = await _london_undone_over_paris(ch, org, "live_in")
    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=45)
    await _stated(
        ch, org, "s3", "I am moving to Berlin", (*live, "berlin"), memory_key="move", valid_at=ahead
    )
    await _edges_like_triples(ch, org)

    async def _fallback_fails(*a: Any, **k: Any) -> None:
        raise ConnectionError("transient")

    with monkeypatch.context() as patched:
        patched.setattr(triple_projections, "_keep_one_current", _fallback_fails)
        await _revise(
            "memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),)
        )
    client = ch_mod.get_client()
    execute, failed = client.execute, []

    async def _edge_close_fails(sql: str, *a: Any, **kw: Any) -> Any:
        # The repair's edge close names each edge by its key (char 31 joins its parts).
        if not failed and "ALTER TABLE loom.entity_edges" in sql and "char(31)" in sql:
            failed.append(sql)
            raise ConnectionError("transient")
        return await execute(sql, *a, **kw)

    with monkeypatch.context() as patched:
        patched.setattr(client, "execute", _edge_close_fails)
        first = await _retry_one(await _recorded_reconcile(ch, org), ch)
    assert failed and not first.resolved
    second = await _retry_one(await _recorded_reconcile(ch, org), ch)
    assert second.resolved
    await _mutations_done(ch, org)
    after = ahead + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=False, as_of=after) == ["berlin"]


async def test_a_retired_rows_edge_ends_with_its_triple(ch, org) -> None:
    # London's row retires behind Paris's: both walks reach Paris only. A favourite
    # city may have many values, so Paris's own write does not close London.
    london = await _stated(ch, org, "s1", "I like London")
    paris = await _stated(ch, org, "s2", "I like Paris")
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, london, paris)
    assert await _walked(org, ["paris"], labeled=True) == ["paris"]
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]


async def test_a_replayed_follow_ends_the_edge_its_failure_left_open(ch, org, monkeypatch) -> None:
    # London's close lands and its edge end fails: the replay plans no close, as
    # London is closed, and still ends the edge.
    from server.memory.app import triple_projections

    london = await _stated(ch, org, "s1", "I like London")
    paris = await _stated(ch, org, "s2", "I like Paris")
    await _edges_like_triples(ch, org)

    async def _fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("edges down")

    with monkeypatch.context() as patched:
        patched.setattr(triple_projections, "end_edges", _fails)
        with pytest.raises(RuntimeError):
            await _follow_retirement(ch, org, london, paris)
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"paris"}) == {"paris"}
    assert await _walked(org, ["london", "paris"], labeled=False) == ["london", "paris"]
    await _replay_follow(ch, org, london, paris)
    assert await _walked(org, ["paris"], labeled=False) == ["paris"]


async def test_a_replayed_follow_keeps_one_value_current(ch, org, monkeypatch) -> None:
    # London undone over Paris, then retired behind Rome: the follow closes London and
    # fails to state Paris again. The replay finds London closed and still does.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    rome = await _stated(ch, org, "s3", "I like Rome", ("user", "like", "rome"))

    async def _fails(*a: Any, **k: Any) -> None:
        raise RuntimeError("restate down")

    with monkeypatch.context() as patched:
        patched.setattr(graph_undo, "restate_facts", _fails)
        with pytest.raises(RuntimeError):
            await _follow_retirement(ch, org, london, rome)
    await _mutations_done(ch, org)
    assert await _open_facts(ch, org, {("like", "rome")}) == {("like", "rome")}
    await _replay_follow(ch, org, london, rome)
    want = {("live_in", "paris"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want


async def test_a_retired_rows_edge_ends_when_a_winner_dated_ahead_starts(ch, org) -> None:
    # Paris's row retires behind Berlin's, dated next month: Paris holds until then on
    # both walks, and only Berlin after.
    from server.memory import store as M

    ahead = naive_utc_now().replace(microsecond=0) + timedelta(days=30)
    paris = await _stated(ch, org, "s1", "I like Paris")
    berlin = await _stated(ch, org, "s2", "I like Berlin", valid_at=ahead)
    berlin = {**berlin, "valid_at": ahead, "version": M.next_version()}
    await M.insert_memory_versions([berlin], ch=ch)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, paris, berlin)
    both = ["berlin", "paris"]
    assert await _walked(org, both, labeled=True) == both
    assert await _walked(org, both, labeled=False) == both
    later = ahead + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=later) == ["berlin"]
    assert await _walked(org, ["berlin"], labeled=False, as_of=later) == ["berlin"]


async def test_a_retirement_ends_now_a_fact_its_row_stated_until_later(ch, org) -> None:
    # "I work at Acme" until next month, retired behind "I like Rome": Acme ends now on
    # both walks, as a revision away ends it.
    until = naive_utc_now() + timedelta(days=20)
    job = await _stated(
        ch, org, "s1", "I work at Acme", ("user", "work_at", "acme"), fact_valid_to=until
    )
    rome = await _stated(ch, org, "s2", "I like Rome")
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, job, rome)
    assert await _walked(org, ["rome"], labeled=True) == ["rome"]
    assert await _walked(org, ["rome"], labeled=False) == ["rome"]


async def test_a_retirement_ends_a_fact_stated_until_later_when_a_dated_winner_starts(
    ch, org
) -> None:
    # Paris until next month, retired behind Berlin from ten days on: Paris holds
    # until Berlin starts, as an open Paris would.
    from server.memory import store as M

    now = naive_utc_now().replace(microsecond=0)
    live, starts = ("user", "live_in"), now + timedelta(days=10)
    paris = await _stated(
        ch, org, "s1", "I live in Paris", (*live, "paris"), fact_valid_to=now + timedelta(days=30)
    )
    berlin = await _stated(ch, org, "s2", "I live in Berlin", (*live, "berlin"), valid_at=starts)
    berlin = {**berlin, "valid_at": starts, "version": M.next_version()}
    await M.insert_memory_versions([berlin], ch=ch)
    await _edges_like_triples(ch, org)
    await _follow_retirement(ch, org, paris, berlin)
    await asyncio.sleep(1.1)  # the walk counts a fact ended this second as current
    between = now + timedelta(days=5)
    assert await _walked(org, ["paris"], labeled=True, as_of=between) == ["paris"]
    assert await _walked(org, ["paris"], labeled=False, as_of=between) == ["paris"]
    later = starts + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=later) == ["berlin"]


async def test_a_revision_ends_now_a_value_still_valid_until_a_later_move(ch, org) -> None:
    # After the Undo, a move to Berlin dated ahead ends London then. London's row is then
    # revised away: London ends now, and Paris holds until Berlin starts.
    london = await _london_undone_over_paris(ch, org, "live_in")
    moving = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    moving += timedelta(days=45)
    await _stated(
        ch,
        org,
        "s4",
        "I am moving to Berlin",
        ("user", "live_in", "berlin"),
        memory_key="k4",
        valid_at=moving,
    )
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    soon = naive_utc_now() + timedelta(seconds=2)
    assert await _walked(org, ["paris"], labeled=True, as_of=soon) == ["paris"]
    after = moving + timedelta(days=1)
    assert await _walked(org, ["berlin"], labeled=True, as_of=after) == ["berlin"]


async def test_a_value_written_while_a_revision_restates_another_stays_current(
    ch, org, monkeypatch
) -> None:
    # The revision drops London and finds no current value; a move to Berlin dated
    # ahead lands before Paris is stated again. Paris then ends when Berlin starts.
    from server.memory.app import graph_undo

    london = await _london_undone_over_paris(ch, org, "live_in")
    restate = graph_undo.restate_facts
    moving = naive_utc_now().replace(hour=0, minute=0, second=0, microsecond=0)
    moving += timedelta(days=45)

    async def _berlin_first(*a: Any, **k: Any) -> Any:
        monkeypatch.setattr(graph_undo, "restate_facts", restate)
        await _stated(
            ch,
            org,
            "s4",
            "I am moving to Berlin",
            ("user", "live_in", "berlin"),
            memory_key="k4",
            valid_at=moving,
        )
        return await restate(*a, **k)

    monkeypatch.setattr(graph_undo, "restate_facts", _berlin_first)
    await _revise("memory.update_by_id", org, london, "I like Rome", (("user", "like", "rome"),))
    await _mutations_done(ch, org)
    want = {("live_in", "berlin"), ("like", "rome")}
    assert await _open_facts(ch, org, want) == want
    soon = naive_utc_now() + timedelta(seconds=2)
    assert await _walked(org, ["paris", "rome"], labeled=True, as_of=soon) == ["paris", "rome"]


async def test_a_batch_and_a_racing_writer_leave_one_current_value(ch, org, monkeypatch) -> None:
    # One write: London from Oct 15, then Berlin from Oct 14. Rome from Oct 14 arrives
    # later from another writer. Both repairs read all three rows before any close.
    from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetResult
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
    batch = MemoryBulkSetInput(
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        entries=[
            {
                "memory_key": f"k-{city}",
                "value": f"I live in {city}",
                "relations": [(*live, city)],
                "valid_at": day,
            }
            for city, day in (
                ("london", datetime(2026, 10, 15)),
                ("berlin", datetime(2026, 10, 14)),
            )
        ],
    )
    results = [
        _BulkSetResult(memory_key=e.memory_key, version=1, memory_id="") for e in batch.entries
    ]
    await asyncio.gather(
        projections.write_entity_triples(batch, results, ch=ch, entity_map={}),
        _stated(
            ch,
            org,
            "s3",
            "I live in Rome",
            (*live, "rome"),
            memory_key="k3",
            valid_at=datetime(2026, 10, 14),
        ),
    )
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, {"london"}) == {"london"}


async def test_two_undated_writers_racing_in_one_second_leave_one_current_value(
    ch, org, monkeypatch
) -> None:
    # Both writes insert before either repairs, so each repair reads the other's row.
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
    await asyncio.gather(
        _stated(ch, org, "s1", "I live in London", (*live, "london"), memory_key="k1"),
        _stated(ch, org, "s2", "I live in Paris", (*live, "paris"), memory_key="k2"),
    )
    await _mutations_done(ch, org)
    # The later arrival, to the microsecond, is the one current value.
    later = await ch.query_one(
        "SELECT argMax(object, seen_at) AS city FROM loom.entity_triples"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert await _open_cities(ch, org, {later["city"]}) == {later["city"]}


async def test_every_start_of_a_closed_value_closes_its_edge(ch, org) -> None:
    # London from January and from March, Paris from June: both London edges close.
    from types import SimpleNamespace

    from server.memory.app.projections import entity_triple_row, repair_intervals

    rows = [
        entity_triple_row(
            org,
            NS,
            "user",
            "live_in",
            "london",
            provenance="t",
            confidence=1.0,
            adapter_name="t",
            last_seen=datetime(2026, 9, 1),
            seen_at=datetime(2026, 9, 1),
            valid_from=start,
        )
        for start in (datetime(2026, 1, 1), datetime(2026, 3, 1))
    ]
    await ch.insert("loom.entity_triples", rows, settings={"wait_for_async_insert": 1})
    await _edges_like_triples(ch, org)
    paris = {
        "subject": "user",
        "relation": "live_in",
        "object": "paris",
        "valid_from": datetime(2026, 6, 1),
        "seen_at": datetime(2026, 9, 2),
    }
    scope = SimpleNamespace(org=org, namespace=NS)
    repairs = await repair_intervals(scope, [paris], ch=ch, at=datetime(2026, 9, 2))
    assert sorted(r.valid_from for r in repairs) == [datetime(2026, 1, 1), datetime(2026, 3, 1)]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()
    assert await _open_edges(ch, org, set()) == []


async def test_a_dropped_facts_edge_stays_open_while_another_row_names_both(ch, org) -> None:
    # A trip row names Alice and London and states no relation. London's row is
    # revised away: its fact ends, the trip row's edge does not.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, trip, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=True, seed="alice") == []
    assert await _walks_to(org, "london")


async def test_a_dropped_facts_edge_stays_open_while_a_previous_build_row_names_both(
    ch, org
) -> None:
    # Mid-rollout the previous build writes the trip row's edge with a key link and no
    # row link (it links no rows). London's row is revised away: the trip row's edge stays.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, {**trip, "memory_id": ""}, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


async def test_a_dropped_facts_edge_stays_open_while_the_same_key_in_another_session_names_both(
    ch, org
) -> None:
    # Two sessions keep one key: s1 likes London, s2 is a trip there. s1's row is
    # revised away; s2's row still names Alice and London, so the edge stays open.
    likes = ("alice", "like", "london")
    london = await _stated(ch, org, "s1", "Alice likes London", likes)
    trip = await _stated(ch, org, "s2", "Alice's trip to London", extracted=False)
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, trip, "alice", "london")
    await _revise("memory.update_by_id", org, london, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


async def test_a_key_that_no_longer_names_both_keeps_no_edge_open(ch, org) -> None:
    # Two keys say Alice lives in Paris. The second is revised to jazz, then the first
    # to blues: the second key's old link names the pair, but its value no longer does.
    lives = ("alice", "live_in", "paris")
    first = await _stated(ch, org, "s1", "Alice lives in Paris", lives, memory_key="k1")
    second = await _stated(ch, org, "s2", "Alice lives in Paris", lives, memory_key="k2")
    await _mentions(ch, org, first, "alice", "paris")
    await _mentions(ch, org, second, "alice", "paris")
    await _revise("memory.update_by_id", org, second, "Alice likes jazz")
    await _mutations_done(ch, org)
    await _revise("memory.update_by_id", org, first, "Alice likes blues")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=True, seed="alice") == []
    # The revisions' own edges (jazz, blues) are walked; Paris is not.
    walked = await _walked(
        org, [], labeled=False, seed="alice", until=lambda w: "jazz" in w and "paris" not in w
    )
    assert "jazz" in walked and "paris" not in walked


@pytest.mark.parametrize(
    ("city", "trip_value"),
    [("new york city", "Alice's trip to NYC"), ("北京市", "Alice去北京市旅行")],
)
async def test_a_dropped_facts_edge_stays_open_while_a_row_states_the_pair_in_other_words(
    ch, org, city, trip_value
) -> None:
    # The trip row's entities are Alice and the city, though its text spells the city
    # another way, or with no space around it. The city's row is revised away: the
    # trip row still states the pair, so the edge stays open.
    liked = await _stated(ch, org, "s1", f"Alice likes {city}", ("alice", "like", city))
    trip = await _stated(ch, org, "s2", trip_value, extracted=False, memory_key="trip")
    await _mentions(ch, org, liked, "alice", city)
    await _mentions(ch, org, trip, "alice", city)
    await _revise("memory.update_by_id", org, liked, "Alice likes Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, city)


@pytest.mark.parametrize("path", ["memory.update_by_id", "memory.set"])
async def test_a_revision_that_still_names_the_pair_keeps_its_edge(
    ch, org, path, monkeypatch
) -> None:
    # No other row names Alice and London. The revision drops the fact but its new
    # value names both: its own edge stays open, with the weight of both mentions.
    from server.memory.app import edge_links

    project = edge_links.project_memory_set_entities

    async def slow_projection(*a: Any, **kw: Any) -> Any:
        # memory.set's first-write projection runs beside the response; slowed here.
        await asyncio.sleep(1.5)
        return await project(*a, **kw)

    monkeypatch.setattr(edge_links, "project_memory_set_entities", slow_projection)
    london = await _stated(ch, org, "s1", "Alice likes London", ("alice", "like", "london"))
    await _mentions(ch, org, london, "alice", "london")
    await _mentions(ch, org, london, "alice", "london")
    await _revise(path, org, london, "Alice took a trip to London")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")
    rows = await ch.query_all(
        "SELECT co_occur, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'london'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [(r["co_occur"], r["valid_to"]) for r in rows] == [(1, None)] * 3


async def test_a_retired_rows_revision_projects_no_edges(ch, org) -> None:
    # London's row retired behind Paris's and is then revised; not embedded, it is not
    # judged again and stays retired. It holds no graph facts, so its new value's edges
    # are not written.
    london = await _stated(
        ch, org, "s1", "Alice likes London", ("alice", "like", "london"), memory_type="episodic"
    )
    paris = await _stated(ch, org, "s2", "Alice likes Paris", ("alice", "like", "paris"))
    await _mentions(ch, org, paris, "alice", "paris")  # what the walk does reach
    await _follow_retirement(ch, org, london, paris)
    await _revise("memory.update_by_id", org, london, "Alice works at Umbrella")
    await _mutations_done(ch, org)
    walked = await _walked(org, [], labeled=False, seed="alice", until=lambda w: "paris" in w)
    assert "paris" in walked and "umbrella" not in walked


async def test_a_row_naming_the_pair_while_its_edge_ends_keeps_it_open(ch, org) -> None:
    # The trip row's edge lands after the close checked the pair and before its UPDATE:
    # the check after the UPDATE finds the row's link and opens the edge again.
    from server.memory.app.projections import end_edges, entity_edge_row

    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    edge = entity_edge_row(org, NS, "alice", "london", last_seen=naive_utc_now())
    await ch.insert("loom.entity_edges", [edge], settings={"wait_for_async_insert": 1})

    class _Racing:
        """`ch`, with the trip row written right after the first read."""

        def __init__(self) -> None:
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(ch, name)

        async def query_all(self, *a: Any, **k: Any) -> Any:
            found = await ch.query_all(*a, **k)
            self.reads += 1
            if self.reads == 1:
                await _mentions(ch, org, trip, "alice", "london")
            return found

    await end_edges(_Racing(), org=org, namespace=NS, ends=[("alice", "london", naive_utc_now())])
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walks_to(org, "london")


@pytest.mark.parametrize("racing", [True, False])
async def test_a_row_naming_the_pair_while_its_edge_ends_keeps_its_stated_end(
    ch, org, racing: bool
) -> None:
    # The edge is stated to hold until next year. The trip row names the pair while the
    # close runs: the edge keeps that end, it is neither cut short nor opened forever.
    # With no row naming it, the close ends it now.
    from server.memory.app.projections import end_edges, entity_edge_row

    trip = await _stated(
        ch, org, "s2", "Alice's trip to London", extracted=False, memory_key="trip"
    )
    until = naive_utc_now().replace(microsecond=0) + timedelta(days=365)
    edge = entity_edge_row(org, NS, "alice", "london", last_seen=naive_utc_now())
    edge["valid_to"] = until
    await ch.insert("loom.entity_edges", [edge], settings={"wait_for_async_insert": 1})

    class _Racing:
        """`ch`, with the trip row written right after the first read."""

        def __init__(self) -> None:
            self.reads = 0

        def __getattr__(self, name: str) -> Any:
            return getattr(ch, name)

        async def query_all(self, *a: Any, **k: Any) -> Any:
            found = await ch.query_all(*a, **k)
            self.reads += 1
            if self.reads == 1 and racing:
                await _mentions(ch, org, trip, "alice", "london")
            return found

    now = naive_utc_now().replace(microsecond=0)
    await end_edges(_Racing(), org=org, namespace=NS, ends=[("alice", "london", now)])
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice' AND entity_b = 'london' AND valid_to IS NOT NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert [r["valid_to"] for r in rows] == [until if racing else now]


async def test_a_fact_that_ended_keeps_no_edge_of_a_dropped_one_open(ch, org) -> None:
    # A live row says London was home until 2020. London's liked row is revised
    # away: the pair's only fact valid now is gone, so the edge ends.
    lived = ("user", "live_in", "london")
    await _stated(
        ch, org, "s1", "I lived in London", lived, memory_key="home", fact_valid_to=_past()
    )
    london = await _stated(ch, org, "s2", "I like London")
    await _edges_like_triples(ch, org)
    await _revise("memory.update_by_id", org, london, "I like Rome")
    await _mutations_done(ch, org)
    await asyncio.sleep(1.1)  # the walk counts an edge ended this second as current
    assert await _walked(org, [], labeled=False) == []


@pytest.mark.parametrize("with_open_edge", [False, True], ids=["finite-only", "open-too"])
async def test_a_racing_holder_reopens_only_the_edges_the_close_ended(
    ch, org, with_open_edge
) -> None:
    # An old London edge already ended in the very second this close ends at. A row
    # naming the pair lands during the close, so what the close ended opens again; the
    # old edge was not ended by it and keeps its end.
    from server.memory import store as M
    from server.memory.app.projections import end_edges

    end = datetime(2026, 6, 1, 12, 0, 0)
    edge = {"org": org, "namespace": NS, "entity_a": "london", "entity_b": "user"}
    edges = [
        {**edge, "valid_from": datetime(2026, 1, 1), "valid_to": end},
        {**edge, "valid_from": datetime(2026, 5, 1), "valid_to": end + timedelta(days=30)},
    ]
    if with_open_edge:
        edges.append({**edge, "valid_from": datetime(2026, 5, 15), "valid_to": None})
    await ch.insert("loom.entity_edges", edges)
    holder = _row(org, "s9", "I live in London", datetime(2026, 5, 20), memory_key="home")

    async def lands() -> None:
        await M.insert_memory_versions([holder], ch=ch)
        link = {k: holder[k] for k in ("org", "namespace", "memory_id", "user_id")}
        sha = holder["content_sha256"]
        await ch.insert("loom.entity_edge_rows", [{**link, "content_sha256": sha, **edge}])

    racing = _HolderLandsAfterTheRead(ch, lands)
    await end_edges(racing, org=org, namespace=NS, ends=[("london", "user", end)])
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_from, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " ORDER BY valid_from SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    got = [(r["valid_from"].replace(tzinfo=None), r["valid_to"]) for r in rows]
    assert got[0] == (datetime(2026, 1, 1), end)
    assert got[1] == (datetime(2026, 5, 1), end + timedelta(days=30))
    if with_open_edge:
        assert got[2] == (datetime(2026, 5, 15), None)


async def test_a_racing_holder_leaves_ended_an_identical_edge_the_close_did_not_end(
    ch, org
) -> None:
    # Two London observations alike in every column the edge has, seen the same second:
    # one already ended at the very second this close ends at, one open. A row naming the
    # pair lands during the close: only the open one, which the close ended, opens again.
    from server.memory import store as M
    from server.memory.app.projections import end_edges

    end = datetime(2026, 6, 1, 12, 0, 0)
    seen = datetime(2026, 5, 1, 9, 0, 0)
    edge = {"org": org, "namespace": NS, "entity_a": "london", "entity_b": "user"}
    alike = {**edge, "valid_from": datetime(2026, 1, 1), "last_seen": seen, "co_occur": 1}
    await ch.insert("loom.entity_edges", [{**alike, "valid_to": end}, {**alike, "valid_to": None}])
    holder = _row(org, "s9", "I live in London", datetime(2026, 5, 20), memory_key="home")

    async def lands() -> None:
        await M.insert_memory_versions([holder], ch=ch)
        link = {k: holder[k] for k in ("org", "namespace", "memory_id", "user_id")}
        sha = holder["content_sha256"]
        await ch.insert("loom.entity_edge_rows", [{**link, "content_sha256": sha, **edge}])

    racing = _HolderLandsAfterTheRead(ch, lands)
    await end_edges(racing, org=org, namespace=NS, ends=[("london", "user", end)])
    await _mutations_done(ch, org)
    rows = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert sorted(str(r["valid_to"]) for r in rows) == sorted([str(end), "None"])
    assert (await _open_weights(ch, org, "london"))["edges"] == 1


@pytest.mark.parametrize("racing", [False, True], ids=["no-holder", "racing-holder"])
@pytest.mark.parametrize("pair", [("london", "user"), ("user", _LONG_JAPANESE_NAME)])
async def test_end_edges_closes_a_pair_seen_thousands_of_times(ch, org, racing, pair) -> None:
    # One edge row per observation: a pair seen 3000 times has 3000 open rows, more than
    # one query parameter could list, the more so under a long non-ASCII name. The close
    # ends all of them, and a row naming the pair that lands during the close opens all
    # of them again.
    from server.memory import store as M
    from server.memory.app.projections import end_edges

    end = datetime(2026, 6, 1, 12, 0, 0)
    edge = {"org": org, "namespace": NS, "entity_a": pair[0], "entity_b": pair[1]}
    start = datetime(2026, 1, 1)
    await ch.insert(
        "loom.entity_edges",
        [{**edge, "valid_from": start + timedelta(minutes=i)} for i in range(3000)],
    )
    holder = _row(org, "s9", "I live in London", datetime(2026, 5, 20), memory_key="home")

    async def lands() -> None:
        await M.insert_memory_versions([holder], ch=ch)
        link = {k: holder[k] for k in ("org", "namespace", "memory_id", "user_id")}
        sha = holder["content_sha256"]
        await ch.insert("loom.entity_edge_rows", [{**link, "content_sha256": sha, **edge}])

    closing = _HolderLandsAfterTheRead(ch, lands) if racing else ch
    await end_edges(closing, org=org, namespace=NS, ends=[(*pair, end)])
    await _mutations_done(ch, org)
    (row,) = await ch.query_all(
        "SELECT countIf(valid_to IS NULL) AS open, countIf(valid_to = {end:DateTime}) AS ended"
        " FROM loom.entity_edges WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org, "end": end},
    )
    assert (row["open"], row["ended"]) == ((3000, 0) if racing else (0, 3000))


async def test_the_skip_log_counts_an_entry_a_landed_pin_refused(ch, org, caplog) -> None:
    # The first entry lands pinned, so the second is refused: the skip log, written once
    # the verdicts settle the batch, names it as pinned.
    import logging

    caplog.set_level(logging.INFO, logger="server.memory.app.bulk_set")
    out = await _bulk_beside_a_pin(
        org,
        [
            {"memory_key": "plan", "value": "Ship the release notes on Monday.", "pinned": True},
            {"memory_key": "plan", "value": "Ship the release notes on Wednesday."},
        ],
    )
    assert out.written[1].conflict_verdict == "pinned"
    assert "skipped 1 entry targeting pinned key(s): ['plan']" in caplog.text


async def test_names_that_look_like_grammar_are_reachable_from_a_point_write(ch, org) -> None:
    # Saitama is written in hiragana; Uganda and Rwanda end like a Korean verb. Each is a
    # name, so the write links it to the others and a walk from Uganda reaches them.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_facts import FactsTraverseInput
    from server.memory.app import after_write
    from server.memory.app.facts import handle_facts_traverse
    from server.memory.app.write import handle_memory_set

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    await handle_memory_set(
        MemorySetInput(**ids, memory_key="trip", value="さいたま 우간다 르완다 캐나다")
    )
    await after_write.drain()
    walk = FactsTraverseInput(org=org, namespace=NS, seed_entity="우간다", max_hops=1)
    nodes = {n.entity for n in (await handle_facts_traverse(walk)).nodes}
    assert {"さいたま", "르완다", "캐나다"} <= nodes, nodes


async def test_triple_closes_of_long_names_go_a_parameter_at_a_time(ch, org) -> None:
    """200 open triples of ~470-character names: one bound list is past the server's
    field cap, so the closes, their undo, the later-end check and the holder read go a
    slice at a time, and each row still gets its own end."""
    from server.memory.app import graph_holders, projections

    name = "Committee for the Coordination of Freight " * 11
    seen = naive_utc_now().replace(microsecond=0)
    triples = [(f"{name}{i}", "member_of", f"{name}x{i}") for i in range(200)]
    rows = [
        projections.entity_triple_row(
            org,
            NS,
            s,
            r,
            o,
            provenance="stated",
            confidence=1.0,
            adapter_name="test",
            last_seen=seen,
            seen_at=seen,
        )
        for s, r, o in triples
    ]
    await ch.insert("loom.entity_triples", rows, settings={"async_insert": 0})
    closed_at = datetime(2026, 1, 2)
    closes = [
        (s, r, o, datetime(2026, 1, 1) + timedelta(minutes=i), closed_at)
        for i, (s, r, o) in enumerate(triples)
    ]
    assert await projections.close_triples(ch, org=org, namespace=NS, closes=closes) > 1
    await _mutations_done(ch, org)
    ended = await ch.query_all(
        "SELECT subject, valid_to FROM loom.entity_triples WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["subject"]: r["valid_to"] for r in ended} == {s: vt for s, _, _, vt, _ in closes}
    await projections.reopen_triples(
        ch, org=org, namespace=NS, closes=[c[:4] for c in closes], closed_at=closed_at
    )
    await projections.end_triples_now(ch, org=org, namespace=NS, triples=triples)
    assert await graph_holders.live_holders(org, NS, triples, ch=ch) == {}
    await _mutations_done(ch, org)
    reopened = await ch.query_all(
        "SELECT count() AS n FROM loom.entity_triples WHERE org = {org:String}"
        " AND valid_to IS NULL SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert int(reopened[0]["n"]) == 200


async def test_an_edge_undo_opens_only_what_its_own_close_ended(ch, org) -> None:
    """Two open edges ended by two closes: undoing the first close opens its edge with
    its weight and start as they were, and leaves the other close's edge ended."""
    from server.memory.app import projections

    seen = naive_utc_now().replace(microsecond=0)
    start = datetime(2026, 1, 1)
    rows = [
        projections.entity_edge_row(org, NS, a, b, last_seen=seen, valid_from=start, co_occur=3)
        for a, b in (("alice", "bob"), ("alice", "carol"))
    ]
    await ch.insert("loom.entity_edges", rows, settings={"async_insert": 0})
    first = await projections.end_edges(
        ch, org=org, namespace=NS, ends=[("alice", "bob", seen)], open_only=True
    )
    second = await projections.end_edges(
        ch, org=org, namespace=NS, ends=[("alice", "carol", seen)], open_only=True
    )
    assert len(first) == len(second) == 1 and first != second
    await projections.reopen_closed_by(
        ch, org=org, namespace=NS, close_ids=first, pairs=[("alice", "bob"), ("alice", "carol")]
    )
    await _mutations_done(ch, org)
    edges = await ch.query_all(
        "SELECT entity_b, valid_from, valid_to, co_occur, closed_by FROM loom.entity_edges"
        " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    by_b = {r["entity_b"]: r for r in edges}
    assert by_b["bob"]["valid_to"] is None and by_b["bob"]["closed_by"] == ""
    assert (by_b["bob"]["valid_from"], by_b["bob"]["co_occur"]) == (start, 3)
    assert by_b["carol"]["valid_to"] is not None and by_b["carol"]["closed_by"] == second[0]
