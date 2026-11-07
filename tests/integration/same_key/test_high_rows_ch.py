"""'high' rows in the graph against ClickHouse: only their declared edges stay,
the drop of the rest, and the edge closes recorded without a value."""

from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, datetime
from typing import Any

import pytest

from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.store.sensitivity_sweep_sql import SORT_KEY
from tests.integration.same_key._rows import (
    KEY,
    NS,
    USER,
    _drop_intents,
    _edge_links,
    _high_set,
    _long_names,
    _made_high_with_the_drop_lost,
    _mutations_done,
    _open_cities,
    _open_edge_pairs,
    _open_facts,
    _recorded_closes,
    _stated,
)


async def test_a_high_bulk_entry_puts_only_its_declared_edge_in_the_shared_graph(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[
                {
                    "memory_key": "k1",
                    "value": "My vault code is 4417",
                    "sensitivity": "high",
                    "entities": ["vault", "4417"],
                    "relations": [("user", "vault_code", "4417")],
                },
                {
                    "memory_key": "k2",
                    "value": "I like Oslo and Bergen",
                    "entities": ["oslo", "bergen"],
                    "relations": [("user", "likes", "oslo")],
                },
            ],
        )
    )

    async def _graphed() -> set[tuple[str, str]]:
        got = await ch.query_all(
            "SELECT 'triples' AS t, object AS text FROM loom.entity_triples WHERE org = {org:String}"
            " UNION ALL SELECT 'rows', object FROM loom.entity_triple_rows WHERE org = {org:String}"
            " UNION ALL SELECT 'edges', concat(entity_a, ' ', entity_b) FROM loom.entity_edges"
            " WHERE org = {org:String}"
            " UNION ALL SELECT 'vectors', entity_text FROM loom.entity_vectors"
            " WHERE org = {org:String}"
            " SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return {(str(r["t"]), str(r["text"])) for r in got}

    # The batch's other entry is the sentinel: once its rows are in, all of them are.
    want = {("triples", "oslo"), ("rows", "oslo"), ("edges", "bergen oslo"), ("vectors", "oslo")}
    for _ in range(100):
        graphed = await _graphed()
        if want <= graphed:
            break
        await asyncio.sleep(0.1)
    assert want <= graphed
    # Only the edge of the entities its writer declared, as memory.set projects a high
    # row: no triple, triple row or entity vector of its value.
    leaked = [g for g in graphed if "4417" in g[1] or "vault" in g[1]]
    assert leaked == [("edges", "4417 vault")], leaked


async def test_a_high_point_write_puts_no_entity_edge_in_the_shared_graph(ch, org) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    for key, value, sensitivity in (
        ("k1", "Alice Smith met Bob Jones in Secretville", "high"),
        ("k2", "Carol White met Dave Brown in Oslo", ""),
    ):
        await handle_memory_set(
            MemorySetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key=key,
                value=value,
                sensitivity=sensitivity,
            )
        )

    async def _edges() -> list[str]:
        got = await ch.query_all(
            "SELECT concat(entity_a, ' ', entity_b) AS pair FROM loom.entity_edges"
            " WHERE org = {org:String} SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return [str(r["pair"]) for r in got]

    # The ordinary write is the sentinel: once its edges are in, the high one's would be.
    for _ in range(100):
        edges = await _edges()
        if any("oslo" in e for e in edges):
            break
        await asyncio.sleep(0.1)
    assert any("oslo" in e for e in edges)
    assert not [e for e in edges if "secretville" in e or "alice" in e or "bob" in e]


async def test_a_bulk_revision_to_high_closes_what_its_row_held(ch, org) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import handle_memory_bulk_set

    london = await _stated(ch, org, "s1", "I currently live in London")
    assert await _open_cities(ch, org, {"london"}) == {"london"}
    entry = {
        "memory_key": KEY,
        "value": "I currently live in London, flat 4B",
        "sensitivity": "high",
    }
    out = await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )
    )
    assert [r.memory_id for r in out.written] == [str(london["memory_id"])]
    await _mutations_done(ch, org)
    assert await _open_cities(ch, org, set()) == set()


async def test_a_revision_of_a_high_row_keeps_its_declared_edges(ch, org) -> None:
    """A high row's value is never mined, but the entities its writer declared project:
    a revision naming the same entities leaves their edge open."""
    await _high_set(
        org,
        "s1",
        "vault",
        "Orchid Falcon rotate the Vault Key",
        sensitivity="high",
        entities=["Billing", "Stripe"],
    )
    edges = await _open_edge_pairs(ch, org)
    assert len(edges) == 1 and all("orchid" not in a + b for a, b in edges), edges
    await _high_set(
        org,
        "s1",
        "vault",
        "Quartz Meridian rotate the Vault Key",
        sensitivity="high",
        entities=["Billing", "Stripe"],
    )
    assert await _open_edge_pairs(ch, org) == edges


@pytest.mark.parametrize("how", ["same value", "new value", "bulk"])
async def test_a_high_row_re_set_with_fewer_entities_ends_the_dropped_ones(ch, org, how) -> None:
    """A high row keeps the edges of the entities it names and no other: re-set high
    naming one entity fewer, the edges of the one it dropped end."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    value = "Alice Smith met Bob Jones at Acme Corp"
    three = ["Alice Smith", "Bob Jones", "Acme Corp"]
    await _high_set(org, "s1", "contact", value, sensitivity="high", entities=three)
    assert len(await _open_edge_pairs(ch, org)) == 3
    narrowed = {"sensitivity": "high", "entities": ["Alice Smith", "Bob Jones"]}
    if how == "bulk":
        # A new value: bulk skips a restatement whose labels are unchanged.
        entry = {"memory_key": "contact", "value": value + ".", "memory_type": "semantic"}
        entry |= narrowed
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                entries=[entry],
            )
        )
        await after_write.drain()
    else:
        new = value if how == "same value" else value + "."
        await _high_set(org, "s1", "contact", new, **narrowed)
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob jone")}


async def test_a_relabel_to_high_closes_mined_facts_and_keeps_declared_edges(ch, org) -> None:
    """The sensitivity sweep relabels a row high: the edges its text gave the graph and
    its triples close; the edge of the entities its writer declared stays."""
    from server.memory.app import sensitivity_sweep

    out = await _high_set(
        org,
        "s1",
        "door",
        "Ana keeps the vault key in Porto",
        entities=["Ana", "Porto"],
        relations=[("ana", "keeps_key_in", "porto")],
    )
    declared = await _open_edge_pairs(ch, org)
    assert declared, "the declared pair projects"
    assert await _open_facts(ch, org, {("keep_key_in", "porto")}) == {("keep_key_in", "porto")}
    # A mined edge linked to the same row (as a plain row's text would give it).
    await ch.insert(
        "loom.entity_edge_rows",
        [
            {
                "org": org,
                "namespace": NS,
                "memory_id": out.memory_id,
                "user_id": USER,
                "content_sha256": out.content_sha256,
                "entity_a": "vault key",
                "entity_b": "zzz",
            }
        ],
    )
    await ch.insert(
        "loom.entity_edges",
        [{"org": org, "namespace": NS, "entity_a": "vault key", "entity_b": "zzz"}],
    )
    assert ("vault key", "zzz") in await _open_edge_pairs(ch, org)
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=out.memory_id,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key="door",
        level="high",
        shapes=("credential",),
        sort_key=dict(zip(SORT_KEY, (org, NS, "bot", USER, "s1", "door"), strict=True)),
        version=out.version,
    )
    result = await sensitivity_sweep.apply([candidate], ch=ch)
    assert result.high == 1, result
    assert await _open_edge_pairs(ch, org) == declared
    assert await _open_facts(ch, org, set()) == set()


async def test_the_sweep_leaves_a_head_written_since_its_scan(ch, org) -> None:
    # Scanned unlabelled, the key is then written again and labelled high by its writer: the
    # sweep's candidate from the scan must not relabel that head medium.
    from server.memory import store as M
    from server.memory.app import sensitivity_sweep

    scanned = await _high_set(org, "s1", "door", "write to ana@example.com about the door")
    newer = await _high_set(org, "s1", "door", "the door code is in the vault", sensitivity="high")
    assert newer.memory_id == scanned.memory_id  # moved, not gone
    candidate = sensitivity_sweep.SweepCandidate(
        memory_id=scanned.memory_id,
        org=org,
        namespace=NS,
        agent="bot",
        user_id=USER,
        session_id="s1",
        memory_key="door",
        level="medium",
        shapes=("email",),
        sort_key=dict(zip(SORT_KEY, (org, NS, "bot", USER, "s1", "door"), strict=True)),
        version=scanned.version,
    )
    result = await sensitivity_sweep.apply([candidate], ch=ch)
    assert (result.relabelled, len(result.left)) == (0, 1) and not result.failed
    identity = M.Identity(org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1")
    head = await M.get_by_key(identity, "door", scope=WHOLE_NAMESPACE, ch=ch, strong=True)
    assert head is not None and (head.version, head.sensitivity) == (newer.version, "high")


async def test_a_late_mined_projection_writes_nothing_on_a_row_made_high(
    ch, org, monkeypatch
) -> None:
    """A plain write's mined edges still projecting beside its response when the same
    value is re-set as high: the late projection's fence no longer counts the row, so
    no mined edge lands on it."""
    from server.memory.app import graph_holders

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin about Project Falcon"
    gate = asyncio.Event()
    real = graph_holders.rows_holding

    async def slow(*a: Any, **kw: Any) -> Any:
        await gate.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(graph_holders, "rows_holding", slow)
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    first = await handle_memory_set(MemorySetInput(**ids, memory_key="contact", value=value))
    monkeypatch.setattr(graph_holders, "rows_holding", real)
    await handle_memory_set(
        MemorySetInput(**ids, memory_key="contact", value=value, sensitivity="high")
    )
    gate.set()
    await after_write.drain()
    links = await ch.query_all(
        "SELECT entity_a FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND memory_id = {id:UUID} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": first.memory_id},
    )
    assert links == []


async def test_a_bulk_re_set_to_high_drops_the_mined_edges(ch, org) -> None:
    """A plain row's text gave the graph edges; a bulk write of the same value as high
    drops them after its response, as memory.set does."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin"
    await _high_set(org, "s1", "contact", value)
    assert await _edge_links(ch, org) > 0 and await _open_edge_pairs(ch, org)
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=[{"memory_key": "contact", "value": value, "sensitivity": "high"}],
        )
    )
    await after_write.drain()
    assert await _edge_links(ch, org) == 0
    assert await _open_edge_pairs(ch, org) == set()


async def test_a_failed_drop_of_mined_edges_is_left_to_the_repair(ch, org, monkeypatch) -> None:
    """The drop after a re-set to high fails: it is recorded, and the repair drops them."""
    from server.memory.app import edge_links
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin"
    await _high_set(org, "s1", "contact", value)
    real = edge_links.drop_undeclared_links

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("DELETE refused")

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", down)
        await _high_set(org, "s1", "contact", value, sensitivity="high")
    assert await _edge_links(ch, org) > 0
    monkeypatch.setattr(edge_links, "drop_undeclared_links", real)
    results = await retry_projection_failures(
        org=org, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    assert await _edge_links(ch, org) == 0


async def test_a_by_id_update_that_makes_a_row_high_drops_its_mined_edges(ch, org) -> None:
    """The revised value holds a credential, so the update labels the row high: the edges
    the old value's text gave the graph go after the response."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import after_write
    from server.memory.app.update import handle_memory_update_by_id

    out = await _high_set(org, "s1", "contact", "Alice Smith met Bob Jones at Acme Corp in Berlin")
    assert await _edge_links(ch, org) > 0
    updated = await handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org=org,
            memory_id=out.memory_id,
            # A credential shape with a nonce body (never a real secret).
            value=f"Alice Smith keeps sk-proj-{uuid.uuid4().hex} for Acme Corp in Berlin",
        )
    )
    await after_write.drain()
    head = await ch.query_all(
        "SELECT sensitivity FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert updated.updated and head[0]["sensitivity"] == "high"
    assert await _edge_links(ch, org) == 0


async def test_late_declared_edges_write_nothing_on_a_high_row_without_them(
    ch, org, monkeypatch
) -> None:
    """A write's declared entities still projecting when the same value is re-set high
    with no entities: the row no longer names them, so the late edges do not land."""
    from server.memory.app import graph_holders

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin about Project Falcon"
    gate = asyncio.Event()
    real = graph_holders.rows_holding

    async def slow(*a: Any, **kw: Any) -> Any:
        await gate.wait()
        return await real(*a, **kw)

    monkeypatch.setattr(graph_holders, "rows_holding", slow)
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import after_write
    from server.memory.app.write import handle_memory_set

    ids = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
    first = await handle_memory_set(
        MemorySetInput(
            **ids, memory_key="contact", value=value, entities=["Alice Smith", "Bob Jones"]
        )
    )
    monkeypatch.setattr(graph_holders, "rows_holding", real)
    await handle_memory_set(
        MemorySetInput(**ids, memory_key="contact", value=value, sensitivity="high")
    )
    gate.set()
    await after_write.drain()
    links = await ch.query_all(
        "SELECT entity_a FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND memory_id = {id:UUID} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "id": first.memory_id},
    )
    assert links == []


@pytest.mark.parametrize("path", ["bulk", "update_by_id"])
async def test_a_high_row_keeps_its_declared_edge_through_a_revision(ch, org, path) -> None:
    """A row declared Billing and Stripe, re-written high by a bulk write or a by-id
    update that keeps those entities: the pair's edge stays open."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.update import handle_memory_update_by_id

    out = await _high_set(
        org, "s1", "vault", "Rotate the vault key weekly", entities=["Billing", "Stripe"]
    )
    declared = await _open_edge_pairs(ch, org)
    assert len(declared) == 1, declared
    if path == "bulk":
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                entries=[
                    {
                        "memory_key": "vault",
                        "value": "Rotate the vault key daily",
                        "sensitivity": "high",
                        "entities": ["Billing", "Stripe"],
                    }
                ],
            )
        )
    else:
        await handle_memory_update_by_id(
            MemoryUpdateByIdInput(
                org=org,
                memory_id=out.memory_id,
                value=f"Rotate sk-proj-{uuid.uuid4().hex} daily",
            )
        )
    await after_write.drain()
    assert await _open_edge_pairs(ch, org) == declared


async def test_a_user_erasure_deletes_every_kind_of_projection_failure_of_that_user(
    ch, org, monkeypatch
) -> None:
    """Each failure kind a write can leave names its writer's user, so a user erasure
    deletes them all and leaves another user's."""
    from types import SimpleNamespace

    from server.erasure.store import steps_ledgers
    from server.memory.app import edge_links
    from server.memory.app.projection_errors import record_input_projection_failure
    from server.memory.app.projection_failures import (
        EDGES_CLOSE,
        FOLLOW_ROWS,
        TRIPLES_RECONCILE,
        TRIPLES_REOPEN,
    )

    async def boom(*a: Any, **kw: Any) -> None:
        raise RuntimeError("CH down")

    for user in ("alice", "bob"):
        scope = SimpleNamespace(org=org, namespace=NS, agent="bot", user_id=user)
        with monkeypatch.context() as m:
            m.setattr(edge_links, "drop_undeclared_links", boom)
            memory_id = str(uuid.uuid4())
            await edge_links.drop_undeclared_edges(scope, {memory_id: 1}, ch=ch, op="memory.set")
        for kind in (TRIPLES_REOPEN, TRIPLES_RECONCILE, EDGES_CLOSE, FOLLOW_ROWS):
            await record_input_projection_failure(
                scope, projection=kind, error="x", payload={"closes": [["alice", "lives_in", "x"]]}
            )
    await asyncio.sleep(1.0)
    await steps_ledgers.delete_projection_failures_by_user(org, "alice", ch=ch)
    rows = await ch.query_all(
        "SELECT projection, JSONExtractString(payload, 'user_id') AS u"
        " FROM loom.memory_projection_failures WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {r["u"] for r in rows} == {"bob"} and len(rows) == 5, rows


async def test_a_drop_of_mined_edges_is_owed_first_and_resolved_once_it_lands(ch, org) -> None:
    """Re-set high, the drop's intent goes in before it is scheduled and is resolved by
    the next flush once the drop landed; a later write of the row, high already, owes
    no drop."""
    from server.memory.app import intent_marks

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin"
    await _high_set(org, "s1", "contact", value)
    await _high_set(org, "s1", "contact", value, sensitivity="high")
    await intent_marks.drain()
    assert await _edge_links(ch, org) == 0
    for _ in range(50):
        if await _drop_intents(ch, org) == ["resolved"]:
            break
        await asyncio.sleep(0.2)
    assert await _drop_intents(ch, org) == ["resolved"]
    await _high_set(org, "s1", "contact", value + ", again", sensitivity="high")
    assert await _drop_intents(ch, org) == ["resolved"], "no second intent"


async def test_a_bulk_write_making_many_rows_high_owes_one_drop(ch, org, monkeypatch) -> None:
    """Rows re-set high in one bulk call: one intent before the response (not a committed
    insert per row, which made 200 rows take 14 s), one drop after it; a failed drop
    leaves that intent to the repair, which drops every row's links."""

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, edge_links, intent_marks
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.projection_failures import retry_projection_failures

    def write(sensitivity: str) -> MemoryBulkSetInput:
        entries = [
            {
                "memory_key": f"k{i}",
                "value": f"Alice Smith met Bob Jones at Acme Corp {i}",
                "memory_type": "semantic",
                "sensitivity": sensitivity,
                # Declared on the plain write only: the high one names none.
                "entities": [] if sensitivity else ["Alice Smith", "Bob Jones", "Acme Corp"],
            }
            for i in range(5)
        ]
        return MemoryBulkSetInput(
            org=org,
            namespace=NS,
            agent="bot",
            user_id=USER,
            session_id="s1",
            entries=entries,
        )

    await handle_memory_bulk_set(write(""))
    await after_write.drain()
    assert await _edge_links(ch, org) > 0
    out = await handle_memory_bulk_set(write("high"))
    # Each row got a new version and owes one drop: a failure below is then the drop's.
    assert [r.version > 0 for r in out.written] == [True] * 5, out.written
    assert await _drop_intents(ch, org) in (["active"], ["resolved"])
    await after_write.drain()
    await intent_marks.drain()
    assert await _edge_links(ch, org) == 0
    for _ in range(50):  # the mark is a mutation
        if await _drop_intents(ch, org) == ["resolved"]:
            break
        await asyncio.sleep(0.2)
    assert await _drop_intents(ch, org) == ["resolved"]

    # The same with the drop failing: the one intent stays and the repair drops them.
    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("DELETE refused")

    other = f"{org}-failed"
    monkeypatch.setattr(edge_links, "drop_undeclared_links", down)
    await handle_memory_bulk_set(write("").model_copy(update={"org": other}))
    await after_write.drain()
    await handle_memory_bulk_set(write("high").model_copy(update={"org": other}))
    await after_write.drain()
    assert await _drop_intents(ch, other) == ["active"]
    monkeypatch.undo()
    results = await retry_projection_failures(
        org=other, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    assert await _edge_links(ch, other) == 0


async def test_a_bulk_revision_asks_once_whether_its_rows_hold_their_edges(
    ch, org, monkeypatch
) -> None:
    """A bulk revision of rows pairing different entities reads the rows' holders once,
    not once per set of entities, and every edge a current row names stays open."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set, graph_holders
    from server.memory.app.bulk_set import handle_memory_bulk_set

    def write(text: str) -> MemoryBulkSetInput:
        entries = [
            {
                "memory_key": f"k{i}",
                "value": f"{text} {i}",
                "memory_type": "semantic",
                "entities": [f"Person{i}", f"Place{i}"],
            }
            for i in range(40)
        ]
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=entries
        )

    await handle_memory_bulk_set(write("first value"))
    await after_write.drain()
    reads = {"edge_holders": 0, "versions_held": 0}
    patched = (
        (bulk_set, "edge_holders"),
        (graph_holders, "versions_held"),
    )
    for module, name in patched:
        real = getattr(module, name)

        async def counted(*a: Any, _real: Any = real, _name: str = name, **k: Any) -> Any:
            reads[_name] += 1
            return await _real(*a, **k)

        monkeypatch.setattr(module, name, counted)
    await handle_memory_bulk_set(write("second value"))
    await after_write.drain()
    assert reads == {"edge_holders": 1, "versions_held": 0}, reads
    assert len(await _open_edge_pairs(ch, org)) == 40


async def test_a_row_written_high_by_bulk_holds_its_declared_edges(ch, org) -> None:
    """A bulk write as 'high' projects its writer's entities, as memory.set does: when
    another row naming the pair stops naming it, the high row still holds the edge."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write
    from server.memory.app.bulk_set import handle_memory_bulk_set

    entry = {
        "memory_key": "vault",
        "value": "the vault code",
        "memory_type": "semantic",
        "entities": ["Gina", "Hank"],
        "sensitivity": "high",
    }
    await handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )
    )
    await after_write.drain()
    await _high_set(org, "s1", "plain", "Gina met Hank", entities=["Gina", "Hank"])
    await _high_set(org, "s1", "plain", "now secret", sensitivity="high", entities=["Ivy", "Zed"])
    assert ("gina", "hank") in await _open_edge_pairs(ch, org)


async def test_a_failed_bulk_edges_leg_of_a_high_entry_records_no_value(
    ch, org, monkeypatch
) -> None:
    """The edges leg fails for a batch holding a high entry: the failure it records names
    only the plain entries, so no payload holds the high value."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set

    secret = "sk-proj-" + uuid.uuid4().hex

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("edges down")

    monkeypatch.setattr(bulk_set, "write_entity_edges", down)
    entries = [
        {
            "memory_key": "vault",
            "value": f"token {secret}",
            "sensitivity": "high",
            "entities": ["Gina", "Hank"],
        },
        {"memory_key": "plain", "value": "Ivy met Zed", "entities": ["Ivy", "Zed"]},
    ]
    await bulk_set.handle_memory_bulk_set(
        MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=entries
        )
    )
    await after_write.drain()
    rows = await ch.query_all(
        "SELECT projection, payload FROM loom.memory_projection_failures WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert any(r["projection"] == "entity_edges" for r in rows), rows
    assert not [r for r in rows if secret in r["payload"]]


async def test_a_failed_holder_read_after_a_bulk_revision_is_left_to_the_repair(
    ch, org, monkeypatch
) -> None:
    """The holder read after a bulk revision fails: the write still answers, each revised
    row's edge close is recorded, and the repair runs it."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set
    from server.memory.app.projection_failures import retry_projection_failures

    def write(text: str) -> MemoryBulkSetInput:
        entry = {"memory_key": "k", "value": text, "entities": ["Gina", "Hank"]}
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )

    await bulk_set.handle_memory_bulk_set(write("Gina met Hank"))
    await after_write.drain()

    async def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("read timed out")

    monkeypatch.setattr(bulk_set, "edge_holders", down)
    out = await bulk_set.handle_memory_bulk_set(write("Gina left Hank"))
    assert out.written[0].version
    monkeypatch.undo()
    await after_write.drain()
    results = await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert results and all(r.resolved for r in results), results


async def test_a_bulk_write_answers_when_its_holder_read_and_its_record_both_fail(
    ch, org, monkeypatch
) -> None:
    """ClickHouse fails both the holder read and the edge-close record after a bulk
    revision committed: the write still answers."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set, edge_repair

    def write(text: str) -> MemoryBulkSetInput:
        entry = {"memory_key": "k", "value": text, "entities": ["Gina", "Hank"]}
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )

    await bulk_set.handle_memory_bulk_set(write("Gina met Hank"))
    await after_write.drain()

    async def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("clickhouse down")

    monkeypatch.setattr(bulk_set, "edge_holders", down)
    monkeypatch.setattr(edge_repair, "insert_projection_failures", down)
    out = await bulk_set.handle_memory_bulk_set(write("Gina left Hank"))
    assert out.written[0].version


async def test_a_failed_holder_read_records_every_close_in_one_insert_and_no_high_value(
    ch, org, monkeypatch
) -> None:
    """The holder read after a bulk revision of plain and high rows fails: one insert
    records every close, and the high row's names no value."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set, edge_repair

    def write(text: str) -> MemoryBulkSetInput:
        entries = [
            {"memory_key": f"p{i}", "value": f"{text} {i}", "entities": ["Gina", "Hank"]}
            for i in range(3)
        ]
        entries.append(
            {
                "memory_key": "h",
                "value": f"{text} secret",
                "entities": ["Gina", "Hank"],
                "sensitivity": "high",
            }
        )
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=entries
        )

    await bulk_set.handle_memory_bulk_set(write("first"))
    await after_write.drain()

    async def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("read timed out")

    inserts: list[list[dict[str, Any]]] = []
    real_insert = edge_repair.insert_projection_failures

    async def counted(rows: list[dict[str, Any]], **kw: Any) -> None:
        inserts.append(rows)
        await real_insert(rows, **kw)

    monkeypatch.setattr(bulk_set, "edge_holders", down)
    monkeypatch.setattr(edge_repair, "insert_projection_failures", counted)
    await bulk_set.handle_memory_bulk_set(write("second"))
    monkeypatch.undo()
    high_id = (
        await ch.query_one(
            "SELECT toString(memory_id) AS id FROM loom.memory FINAL"
            " WHERE org = {org:String} AND memory_key = 'h'",
            params={"org": org},
        )
    )["id"]
    assert len(inserts) == 1 and len(inserts[0]) == 4
    payloads = [json.loads(r["payload"]) for r in inserts[0]]
    (high_close,) = [p for p in payloads if p["memory_id"] == high_id]
    assert "content_sha256" not in high_close


async def test_a_high_rows_edge_close_record_after_memory_set_names_no_value(
    ch, org, monkeypatch
) -> None:
    """memory.set of a high row, its overtaken-edge read fails: the close is recorded
    without the row's content_sha256."""
    from server.memory.app import graph_holders

    await _high_set(
        org, "s1", "vault", "Orchid one", sensitivity="high", entities=["Billing", "Stripe"]
    )
    real_held, calls = graph_holders.versions_held, 0

    async def second_down(*a: Any, **kw: Any) -> Any:
        nonlocal calls
        calls += 1
        if calls == 1:  # the fence before the edges are written
            return await real_held(*a, **kw)
        raise RuntimeError("read timed out")  # end_overtaken_edges' read

    monkeypatch.setattr(graph_holders, "versions_held", second_down)
    captured = await _recorded_closes(monkeypatch)
    await _high_set(
        org, "s1", "vault", "Orchid two", sensitivity="high", entities=["Billing", "Stripe"]
    )
    monkeypatch.undo()
    closes = [r for r in captured if r["projection"] == "entity_edges_close"]
    assert closes and not [r for r in closes if "content_sha256" in r["payload"]]


async def test_a_high_rows_edge_close_record_after_a_bulk_names_no_value(
    ch, org, monkeypatch
) -> None:
    """A bulk revision of a high row whose overtaken-edge close fails, and one whose
    batch writes the key plain then high with the holder read failing: neither record
    names the high version's content_sha256."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, bulk_set, graph_holders

    def write(*entries: dict[str, Any]) -> MemoryBulkSetInput:
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=list(entries)
        )

    high = {"memory_key": "vault", "entities": ["Billing", "Stripe"], "sensitivity": "high"}
    await bulk_set.handle_memory_bulk_set(write({**high, "value": "Orchid one"}))
    await after_write.drain()

    async def nobody_holds(*a: Any, **kw: Any) -> Any:
        return {}

    async def down(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("read timed out")

    monkeypatch.setattr(bulk_set, "edge_holders", nobody_holds)
    monkeypatch.setattr(graph_holders, "versions_held", down)
    captured = await _recorded_closes(monkeypatch)
    await bulk_set.handle_memory_bulk_set(write({**high, "value": "Orchid two"}))
    await after_write.drain()
    # The same key plain then high in one batch, the holder read failing.
    monkeypatch.setattr(bulk_set, "edge_holders", down)
    plain = {"memory_key": "k", "value": "plain Gina Hank", "entities": ["Gina", "Hank"]}
    secret = {**plain, "value": "secret Gina Hank", "sensitivity": "high"}
    await bulk_set.handle_memory_bulk_set(write(plain, secret))
    monkeypatch.undo()
    from server.core.hashing import sha256_value

    high_shas = {sha256_value(v) for v in ("Orchid two", "secret Gina Hank")}
    recorded = [json.loads(r["payload"]).get("content_sha256") for r in captured]
    assert recorded and not high_shas & set(recorded), recorded


@pytest.mark.parametrize("then", ["revised", "deleted"])
async def test_the_repair_of_a_high_rows_close_ends_the_values_it_no_longer_holds(
    ch, org, monkeypatch, then
) -> None:
    """A close recorded for a high row names no value: the repair ends the edges of each
    value the row no longer holds, and keeps its current one's; a deleted row holds
    none, its last value included."""
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    await _high_set(org, "s1", "vault", "Alice met Carol", entities=["Alice", "Carol"])

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(edge_links, "drop_undeclared_links", nothing)  # its links stay behind
    out = await _high_set(
        org, "s1", "vault", "Alice met Bob", sensitivity="high", entities=["Alice", "Bob"]
    )
    if then == "deleted":
        # The delete's own edge close is lost.
        monkeypatch.setattr(projections, "end_edges", nothing)
        monkeypatch.setattr(triple_projections, "end_edges", nothing)
        await handle_memory_delete(
            MemoryDeleteInput(
                org=org,
                namespace=NS,
                agent="bot",
                user_id=USER,
                session_id="s1",
                memory_key="vault",
            )
        )
    monkeypatch.undo()
    assert ("alice", "carol") in await _open_edge_pairs(ch, org)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", ["alice", "bob"], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    results = await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert results and all(r.resolved for r in results), results
    want = {("alice", "bob")} if then == "revised" else set()
    assert await _open_edge_pairs(ch, org) == want


async def test_a_value_less_close_dates_each_high_value_by_when_the_row_left_it(
    ch, org, monkeypatch
) -> None:
    """A high row revised twice while every drop and close was lost: the repair of its
    value-less closes ends each older value's edge when the next value was written,
    asking each value with its own entities (not the close's), so no end is the
    repair's clock, and keeps the current value's edge open."""
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(edge_links, "drop_undeclared_links", nothing)
    monkeypatch.setattr(projections, "end_edges", nothing)
    monkeypatch.setattr(triple_projections, "end_edges", nothing)
    for i, value in enumerate(("Alice met Carol", "Alice met Dan", "Alice met Bob")):
        if i:
            # Each value's end is read from the next version's write time, in seconds.
            await asyncio.sleep(1.1)
        await _high_set(
            org, "s1", "vault", value, sensitivity="high", entities=value.split(" met ")
        )
    monkeypatch.undo()
    row = await ch.query_one(
        "SELECT toString(memory_id) AS id FROM loom.memory FINAL WHERE org = {org:String}",
        params={"org": org},
    )
    versions = await ch.query_all(
        "SELECT updated_at FROM loom.memory_versions WHERE org = {org:String} ORDER BY version"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    for ents in (["alice", "carol"], ["alice", "dan"]):
        close = edge_repair.EdgeClose(row["id"], "vault", "x" * 64, ents, high=True)
        await edge_repair.record_edges_closes(
            scope, [close], RuntimeError("x"), op="memory.set", ch=ch
        )
    results = await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert results and all(r.resolved for r in results), results
    await _mutations_done(ch, org)
    edges = await ch.query_all(
        "SELECT entity_b, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    ends = {str(e["entity_b"]): e["valid_to"] for e in edges}
    assert set(ends) == {"carol", "dan", "bob"} and ends["bob"] is None, ends
    for name, left in (("carol", versions[1]), ("dan", versions[2])):
        gap = (ends[name] - left["updated_at"]).total_seconds()
        assert abs(gap) < 1, (name, ends, versions)


async def test_a_replayed_drop_ends_the_edges_when_the_row_was_made_high(
    ch, org, monkeypatch
) -> None:
    """The drop after a re-set to high fails and the repair runs it later: the dropped
    edges end when the row was made high, not at the repair's clock."""
    from server.memory.app import edge_links
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Jones at Acme Corp in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("DELETE refused")

    made_high = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", down)
        await _high_set(org, "s1", "contact", value, sensitivity="high")
    await asyncio.sleep(3)
    results = await retry_projection_failures(
        org=org, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    await _mutations_done(ch, org)
    ends = await ch.query_all(
        "SELECT valid_to FROM loom.entity_edges WHERE org = {org:String} AND valid_to IS NOT NULL"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert ends and all(abs((r["valid_to"] - made_high).total_seconds()) < 2 for r in ends), (
        ends,
        made_high,
    )


async def test_a_bulk_making_hundreds_of_rows_high_drops_every_mined_link(ch, org) -> None:
    """300 rows of ten entities each re-set high in one bulk write: the drop's links
    exceed what one query parameter carries, so it reads, deletes and ends them in
    chunks, and resolves its intent."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, intent_marks
    from server.memory.app.bulk_set import handle_memory_bulk_set

    names = [f"{first} Vance" for first in ("Arlo", "Bryn", "Cato", "Dara", "Eris")]
    names += [f"{first} Calder" for first in ("Fenn", "Gale", "Hale", "Ivo", "Juno")]

    def write(high: bool) -> MemoryBulkSetInput:
        entries = [
            {
                "memory_key": f"m{i}",
                "value": f"Meeting {i}: " + ", ".join(names),
                "memory_type": "semantic",
                **({"sensitivity": "high"} if high else {"entities": names}),
            }
            for i in range(300)
        ]
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=entries
        )

    await handle_memory_bulk_set(write(high=False))
    await after_write.drain()
    assert await _edge_links(ch, org) == 300 * 45
    await handle_memory_bulk_set(write(high=True))
    await after_write.drain()
    await intent_marks.drain()
    assert await _edge_links(ch, org) == 0
    assert await _open_edge_pairs(ch, org) == set()
    for _ in range(50):  # the mark is a mutation
        if await _drop_intents(ch, org) == ["resolved"]:
            break
        await asyncio.sleep(0.2)
    assert await _drop_intents(ch, org) == ["resolved"]


@pytest.mark.parametrize(
    "later",
    ["Alice Smith met Carol King", "Alice Smith met Bob Jones and Carol King in Berlin"],
)
async def test_a_drop_replayed_after_a_later_plain_write_changes_nothing(
    ch, org, monkeypatch, later
) -> None:
    """A row made high whose drop failed is written plain again before the repair runs,
    with a new value or with the value it held before: the replay leaves the links and
    open edges of that later write as they are."""
    from server.memory.app.projection_failures import retry_projection_failures

    first = "Alice Smith met Bob Jones and Carol King in Berlin"
    await _high_set(org, "s1", "contact", first)
    await _made_high_with_the_drop_lost(org, first, monkeypatch)
    await _high_set(org, "s1", "contact", later, sensitivity="low")
    open_before = await _open_edge_pairs(ch, org)
    links_before = await _edge_links(ch, org)
    assert ("alice smith", "carol king") in open_before, open_before
    results = await retry_projection_failures(
        org=org, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    assert await _open_edge_pairs(ch, org) == open_before
    assert await _edge_links(ch, org) == links_before


async def test_a_value_less_close_keeps_the_edges_a_high_row_still_names(ch, org) -> None:
    """A row revised by id into a 'high' value keeps the entities it named, whose edges
    only its earlier value links: the repair of a value-less close for it ends none of
    them."""
    from server.memory.api.models_crud import MemoryUpdateByIdInput
    from server.memory.app import after_write, edge_repair
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures
    from server.memory.app.update import handle_memory_update_by_id

    out = await _high_set(
        org,
        "s1",
        "contact",
        "Alice Smith met Bob Lane and Carol King",
        entities=["Alice Smith", "Bob Lane", "Carol King"],
    )
    named = {("alice smith", "bob lane"), ("alice smith", "carol king"), ("bob lane", "carol king")}
    assert await _open_edge_pairs(ch, org) == named
    await handle_memory_update_by_id(
        MemoryUpdateByIdInput(
            org=org,
            memory_id=out.memory_id,
            # A credential shape with a nonce body (never a real secret).
            value=f"Alice Smith keeps sk-proj-{uuid.uuid4().hex} for Bob Lane and Carol King",
        )
    )
    await after_write.drain()
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "contact", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    results = await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert results and all(r.resolved for r in results), results
    assert await _open_edge_pairs(ch, org) == named


@pytest.mark.parametrize("later", ["pin", "unpin", "verify"])
async def test_a_drop_replayed_after_a_write_that_kept_the_row_high_still_drops(
    ch, org, monkeypatch, later
) -> None:
    """A row made high whose drop failed is written again before the repair runs and
    stays high (pinned, then maybe unpinned, or attested): the replay still drops its
    mined links, and only the declared pair's edge stays open."""
    from server.memory.api.models_crud import MemoryUnpinInput, MemoryVerifyInput
    from server.memory.app.handlers import handle_memory_unpin
    from server.memory.app.projection_failures import retry_projection_failures
    from server.memory.app.verify import handle_memory_verify

    first = "Alice Smith met Bob Lane and Carol King in Berlin"
    out = await _high_set(org, "s1", "contact", first)
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)
    await _made_high_with_the_drop_lost(org, first, monkeypatch, ["Alice Smith", "Bob Lane"])
    if later in ("pin", "unpin"):
        await _high_set(
            org,
            "s1",
            "contact",
            first,
            sensitivity="high",
            entities=["Alice Smith", "Bob Lane"],
            pinned=True,
        )
    if later == "unpin":
        key = {"org": org, "namespace": NS, "agent": "bot", "user_id": USER, "session_id": "s1"}
        assert (await handle_memory_unpin(MemoryUnpinInput(**key, memory_key="contact"))).unpinned
    if later == "verify":
        verified = await handle_memory_verify(
            MemoryVerifyInput(org=org, memory_id=str(out.memory_id), actor="human:itest")
        )
        assert verified.changed
    results = await retry_projection_failures(
        org=org, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_a_bulk_making_rows_with_long_entity_names_high_drops_every_link(ch, org) -> None:
    """Ten rows of sixteen long names re-set high: their 1,000 links pass one parameter's
    size, so the drop cuts them by size, not by count."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, intent_marks
    from server.memory.app.bulk_set import handle_memory_bulk_set

    names = _long_names(16, 4)

    def write(high: bool) -> MemoryBulkSetInput:
        entries = [
            {
                "memory_key": f"m{i}",
                "value": f"Meeting {i}",
                "memory_type": "semantic",
                **({"sensitivity": "high"} if high else {"entities": names}),
            }
            for i in range(10)
        ]
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=entries
        )

    await handle_memory_bulk_set(write(high=False))
    await after_write.drain()
    assert await _edge_links(ch, org) == 10 * 100  # a row links at most 100 pairs
    await handle_memory_bulk_set(write(high=True))
    await after_write.drain()
    await intent_marks.drain()
    assert await _edge_links(ch, org) == 0
    assert await _open_edge_pairs(ch, org) == set()


async def test_a_drop_whose_edge_end_fails_leaves_its_links_for_the_replay(
    ch, org, monkeypatch
) -> None:
    """The drop after a re-set to high fails to end its edges: its links are still
    there, so the repair finds the edges by them, ends them and deletes the links."""
    from server.memory.app import projections
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def down(*a: Any, **kw: Any) -> None:
        raise RuntimeError("edges down")

    with monkeypatch.context() as m:
        m.setattr(projections, "end_edges", down)
        await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)
    results = await retry_projection_failures(
        org=org, projection="entity_edges_drop_undeclared", ch=ch
    )
    assert results and all(r.resolved for r in results), results
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_a_bulk_made_high_with_a_longest_declared_entity_drops_and_ends_its_other_pairs(
    ch, org
) -> None:
    """A row naming an entity as long as a name may be, re-set high declaring it and one
    other: the pairs with the undeclared name end, and the declared pair stays."""
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, intent_marks
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.domain.graph_quality import MAX_ENTITY_CHARS, canonical_entity_text

    huge = ("Rome" + " District" * 40)[:MAX_ENTITY_CHARS]
    big = canonical_entity_text(huge)

    def write(sensitivity: str, entities: list[str]) -> MemoryBulkSetInput:
        entry = {"memory_key": "k", "value": "Meeting", "entities": entities}
        if sensitivity:
            entry["sensitivity"] = sensitivity
        return MemoryBulkSetInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
        )

    await handle_memory_bulk_set(write("", [huge, "Bob Lane", "Carol King"]))
    await after_write.drain()
    assert len(await _open_edge_pairs(ch, org)) == 3
    await handle_memory_bulk_set(write("high", [huge, "Bob Lane"]))
    await after_write.drain()
    await intent_marks.drain()
    assert await _open_edge_pairs(ch, org) == {tuple(sorted((big, "bob lane")))}
    linked = await ch.query_all(
        "SELECT DISTINCT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert {(r["entity_a"], r["entity_b"]) for r in linked} == {tuple(sorted((big, "bob lane")))}


@pytest.mark.parametrize(
    "later", ["Alice Smith met Carol King", "Alice Smith met Bob Lane and Carol King in Berlin"]
)
async def test_a_link_written_inside_a_drop_keeps_its_edge_and_survives(
    ch, org, monkeypatch, later
) -> None:
    """A row made high; its drop reads its links, then a plain write of the row (a new
    value, or the same one again) links a pair the drop is ending, before the drop ends
    and deletes: the plain row's links stay and hold its pair's edge open."""
    from server.core.hashing import sha256_value
    from server.memory.app import edge_links, projections
    from server.memory.app.projection_errors import ProjectionScope

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = projections.end_edges

    async def a_later_link_first(*a: Any, **kw: Any) -> list[str]:
        monkeypatch.setattr(projections, "end_edges", real)
        # The writer re-sets the row plain, naming Carol King: its own links land.
        await _high_set(org, "s1", "contact", later, sensitivity="low")
        return await real(*a, **kw)

    monkeypatch.setattr(projections, "end_edges", a_later_link_first)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    await edge_links.drop_undeclared_links(
        scope, {str(out.memory_id): int(head["version"])}, ch=ch, op="memory.set"
    )
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)
    linked = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND content_sha256 = {sha:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "sha": sha256_value(later)},
    )
    assert ("alice smith", "carol king") in {(r["entity_a"], r["entity_b"]) for r in linked}


async def test_a_high_re_set_naming_more_inside_a_drop_keeps_those_links(
    ch, org, monkeypatch
) -> None:
    """A row made high naming Alice and Bob; its drop reads its links, then the row is
    re-set high naming Carol King too, before the drop deletes: the DELETE goes by the
    entities the row names then, so the Alice-Carol link stays."""
    from server.memory.app import after_write, edge_links, projections
    from server.memory.app.projection_errors import ProjectionScope

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = projections.end_edges

    async def named_more_first(*a: Any, **kw: Any) -> list[str]:
        monkeypatch.setattr(projections, "end_edges", real)
        await _high_set(
            org,
            "s1",
            "contact",
            value,
            sensitivity="high",
            entities=["Alice Smith", "Bob Lane", "Carol King"],
        )
        return await real(*a, **kw)

    monkeypatch.setattr(projections, "end_edges", named_more_first)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    await edge_links.drop_undeclared_links(
        scope, {str(out.memory_id): int(head["version"])}, ch=ch, op="memory.set"
    )
    await after_write.drain()
    linked = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    assert ("alice smith", "carol king") in {(r["entity_a"], r["entity_b"]) for r in linked}
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)


async def test_a_restored_link_keeps_its_edges_stated_end_and_weight(ch, org, monkeypatch) -> None:
    """A value stated to hold until 2030 links Alice and Carol; the row is made high and,
    inside its drop, re-set plain with the same value: the link comes back and the edge
    rows are as they were, end and weight, not a fresh open row."""
    from datetime import datetime

    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app import after_write, edge_links, projections
    from server.memory.app.bulk_set import handle_memory_bulk_set
    from server.memory.app.projection_errors import ProjectionScope

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    until = datetime(2030, 12, 31)

    async def stated(**kw: Any) -> None:
        entry = {
            "memory_key": "contact",
            "value": value,
            "valid_to": until,
            "entities": ["Alice Smith", "Bob Lane", "Carol King"],
            **kw,
        }
        await handle_memory_bulk_set(
            MemoryBulkSetInput(
                org=org, namespace=NS, agent="bot", user_id=USER, session_id="s1", entries=[entry]
            )
        )
        await after_write.drain()

    async def edge_rows() -> list[tuple[Any, Any, int]]:
        await _mutations_done(ch, org)
        rows = await ch.query_all(
            "SELECT valid_from, valid_to, co_occur FROM loom.entity_edges"
            " WHERE org = {org:String} AND entity_a = 'alice smith' AND entity_b = 'carol king'"
            " AND invalid_at IS NULL SETTINGS select_sequential_consistency = 1",
            params={"org": org},
        )
        return sorted((r["valid_from"], r["valid_to"], int(r["co_occur"])) for r in rows)

    await stated()
    before = await edge_rows()
    assert before and all(valid_to is not None for _, valid_to, _ in before)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = projections.end_edges

    async def plain_before_the_delete(*a: Any, **kw: Any) -> list[str]:
        monkeypatch.setattr(projections, "end_edges", real)
        close_ids = await real(*a, **kw)
        await stated(sensitivity="low")
        return close_ids

    monkeypatch.setattr(projections, "end_edges", plain_before_the_delete)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    await edge_links.drop_undeclared_links(
        scope, {str(out.memory_id): int(head["version"])}, ch=ch, op="memory.set"
    )
    after = await edge_rows()
    assert all(valid_to is not None for _, valid_to, _ in after), after
    assert sum(w for *_, w in after) == sum(w for *_, w in before) + 1  # the re-set's own


async def test_a_plain_re_set_inside_a_drop_keeps_its_links_when_the_restore_fails(
    ch, org, monkeypatch
) -> None:
    """A row made high, re-set plain with the same value inside its drop, and the
    restore after the DELETE fails: the read before the DELETE spared the plain row's
    links, so after the repair the pair is linked and open."""
    from server.core.hashing import sha256_value
    from server.memory.app import edge_links, projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = projections.end_edges

    async def restore_lost(*a: Any, **kw: Any) -> None:
        raise RuntimeError("restore lost")

    with monkeypatch.context() as patched:

        async def plain_before_the_delete(*a: Any, **kw: Any) -> list[str]:
            patched.setattr(projections, "end_edges", real)
            close_ids = await real(*a, **kw)
            await _high_set(org, "s1", "contact", value, sensitivity="low")
            return close_ids

        patched.setattr(projections, "end_edges", plain_before_the_delete)
        patched.setattr(edge_links, "_restore_kept_links", restore_lost)
        scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
        versions = {str(out.memory_id): int(head["version"])}
        await edge_links.drop_undeclared_edges(scope, versions, ch=ch, op="memory.set")
    await retry_projection_failures(org=org, ch=ch)
    linked = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND content_sha256 = {sha:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "sha": sha256_value(value)},
    )
    assert ("alice smith", "carol king") in {(r["entity_a"], r["entity_b"]) for r in linked}
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)


async def test_a_plain_re_set_after_the_drops_last_read_gets_its_links_back_from_the_repair(
    ch, org, monkeypatch
) -> None:
    """A row made high is re-set plain with the same value between the drop's read before
    the DELETE and the DELETE, and the restore fails: the repair writes its links back, so
    when the row is made high again its drop finds them and ends the undeclared edges."""
    from server.core.hashing import sha256_value
    from server.memory.app import edge_links
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = edge_links._rows_still_high
    reads = []

    async def restore_lost(*a: Any, **kw: Any) -> None:
        raise RuntimeError("restore lost")

    with monkeypatch.context() as patched:

        async def plain_after_the_last_read(*a: Any, **kw: Any) -> dict[str, set[str]]:
            high = await real(*a, **kw)
            reads.append(high)
            if len(reads) == 2:  # the read right before the DELETE
                await _high_set(org, "s1", "contact", value, sensitivity="low")
            return high

        patched.setattr(edge_links, "_rows_still_high", plain_after_the_last_read)
        patched.setattr(edge_links, "_restore_kept_links", restore_lost)
        scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
        versions = {str(out.memory_id): int(head["version"])}
        await edge_links.drop_undeclared_edges(scope, versions, ch=ch, op="memory.set")
    assert len(reads) == 2
    await retry_projection_failures(org=org, ch=ch)
    linked = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND content_sha256 = {sha:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "sha": sha256_value(value)},
    )
    assert ("alice smith", "carol king") in {(r["entity_a"], r["entity_b"]) for r in linked}

    await _high_set(
        org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
    )
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_a_row_made_high_again_before_the_repair_ends_its_undeclared_edges(
    ch, org, monkeypatch
) -> None:
    """As above, but the row is made high again before the repair runs: that drop finds no
    links to end the mined edges by, so the repair writes them back and drops them."""
    from server.memory.app import edge_links
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = edge_links._rows_still_high
    reads = []

    async def restore_lost(*a: Any, **kw: Any) -> None:
        raise RuntimeError("restore lost")

    with monkeypatch.context() as patched:

        async def plain_after_the_last_read(*a: Any, **kw: Any) -> dict[str, set[str]]:
            high = await real(*a, **kw)
            reads.append(high)
            if len(reads) == 2:  # the read right before the DELETE
                await _high_set(org, "s1", "contact", value, sensitivity="low")
            return high

        patched.setattr(edge_links, "_rows_still_high", plain_after_the_last_read)
        patched.setattr(edge_links, "_restore_kept_links", restore_lost)
        scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
        versions = {str(out.memory_id): int(head["version"])}
        await edge_links.drop_undeclared_edges(scope, versions, ch=ch, op="memory.set")
    await _high_set(
        org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
    )
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)

    await retry_projection_failures(org=org, ch=ch)
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_a_same_value_plain_re_set_before_the_drops_delete_keeps_its_links(
    ch, org, monkeypatch
) -> None:
    """A row made high; its drop ends the edges, and right after, before the DELETE, the
    row is re-set plain with the same value: its links come back and its edges open."""
    from server.core.hashing import sha256_value
    from server.memory.app import edge_links, projections
    from server.memory.app.projection_errors import ProjectionScope

    value = "Alice Smith met Bob Lane and Carol King in Berlin"
    await _high_set(org, "s1", "contact", value)

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:
        m.setattr(edge_links, "drop_undeclared_links", nothing)  # the drop runs below
        out = await _high_set(
            org, "s1", "contact", value, sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    head = await ch.query_one(
        "SELECT version FROM loom.memory FINAL WHERE org = {org:String}"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    real = projections.end_edges

    async def plain_before_the_delete(*a: Any, **kw: Any) -> list[str]:
        monkeypatch.setattr(projections, "end_edges", real)
        close_ids = await real(*a, **kw)
        await _high_set(org, "s1", "contact", value, sensitivity="low")
        return close_ids

    monkeypatch.setattr(projections, "end_edges", plain_before_the_delete)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    await edge_links.drop_undeclared_links(
        scope, {str(out.memory_id): int(head["version"])}, ch=ch, op="memory.set"
    )
    linked = await ch.query_all(
        "SELECT entity_a, entity_b FROM loom.entity_edge_rows WHERE org = {org:String}"
        " AND content_sha256 = {sha:String} SETTINGS select_sequential_consistency = 1",
        params={"org": org, "sha": sha256_value(value)},
    )
    assert ("alice smith", "carol king") in {(r["entity_a"], r["entity_b"]) for r in linked}
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)


async def test_values_whose_edges_stay_open_do_not_block_a_value_less_close(
    ch, org, monkeypatch
) -> None:
    """A high row whose older values link only the pair it still names, and one that
    links a pair it no longer names: the repair reads only what can close, so the
    closable value is reached however few values a pass reads (one, here) and however
    many stay open ahead of it by hash."""
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    with monkeypatch.context() as m:  # every close and drop lost: all edges stay open
        m.setattr(edge_links, "drop_undeclared_links", nothing)
        m.setattr(projections, "end_edges", nothing)
        m.setattr(triple_projections, "end_edges", nothing)
        # Their hashes sort ahead of the closable value's.
        for value in ("Alice and Bob note 9", "Alice and Bob note 22", "Alice and Bob note 23"):
            await _high_set(org, "s1", "vault", value, entities=["Alice Smith", "Bob Lane"])
        await _high_set(
            org,
            "s1",
            "vault",
            "Alice met Carol about the plan",
            entities=["Alice Smith", "Carol King"],
        )
        out = await _high_set(
            org, "s1", "vault", "Orchid", sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGE", 1)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGES", 1)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    for _ in range(2):
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_values_another_row_holds_open_do_not_block_a_value_less_close(
    ch, org, monkeypatch
) -> None:
    """Older values linking only a pair another live row holds open are not read, so the
    closable value behind them by hash is reached with one value a pass."""
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    # Another live row holds the Alice-Dave edge open.
    await _high_set(org, "s9", "other", "Alice and Dave met", entities=["Alice Smith", "Dave Ray"])
    with monkeypatch.context() as m:  # every close and drop lost: all edges stay open
        m.setattr(edge_links, "drop_undeclared_links", nothing)
        m.setattr(projections, "end_edges", nothing)
        m.setattr(triple_projections, "end_edges", nothing)
        # Their hashes sort ahead of the closable value's.
        for value in ("Alice and Dave note 6", "Alice and Dave note 22", "Alice and Dave note 37"):
            await _high_set(org, "s1", "vault", value, entities=["Alice Smith", "Dave Ray"])
        await _high_set(
            org,
            "s1",
            "vault",
            "Alice met Carol about the plan",
            entities=["Alice Smith", "Carol King"],
        )
        out = await _high_set(
            org, "s1", "vault", "Orchid", sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    assert ("alice smith", "carol king") in await _open_edge_pairs(ch, org)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGE", 1)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGES", 1)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    # The first pass reads the closable value and runs out of pages; the second resolves.
    for _ in range(2):
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert ("alice smith", "carol king") not in await _open_edge_pairs(ch, org)
    assert ("alice smith", "dave ray") in await _open_edge_pairs(ch, org)


async def test_a_value_less_close_ends_a_page_of_values_with_one_close(
    ch, org, monkeypatch
) -> None:
    """Thirty older values, each linking a pair the high row no longer names: one pass
    ends them all with one end_edges (one page), each pair no later than the write that
    replaced its value."""
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    names = [f"Person{i} Zed{i}" for i in range(30)]
    with monkeypatch.context() as m:  # every close and drop lost: all edges stay open
        m.setattr(edge_links, "drop_undeclared_links", nothing)
        m.setattr(projections, "end_edges", nothing)
        m.setattr(triple_projections, "end_edges", nothing)
        for name in names:
            await _high_set(org, "s1", "vault", f"Alice met {name}", entities=["Alice Smith", name])
        out = await _high_set(
            org, "s1", "vault", "Orchid", sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    real = projections.end_edges
    calls: list[int] = []

    async def counted(*a: Any, **kw: Any) -> list[str]:
        calls.append(len(kw["ends"]))
        return await real(*a, **kw)

    monkeypatch.setattr(projections, "end_edges", counted)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert calls == [30]
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}
    ended = await ch.query_all(
        "SELECT entity_b, valid_to FROM loom.entity_edges WHERE org = {org:String}"
        " AND entity_a = 'alice smith' AND entity_b != 'bob lane'"
        " SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    versions = await ch.query_all(
        "SELECT value, updated_at FROM loom.memory_versions WHERE org = {org:String}"
        " ORDER BY version SETTINGS select_sequential_consistency = 1",
        params={"org": org},
    )
    # Each value ended with the write after it: its pair's end is that write's time.
    replaced = {
        str(json.loads(v["value"])).removeprefix("Alice met ").lower(): nxt["updated_at"]
        for v, nxt in zip(versions, versions[1:], strict=False)
    }
    assert {str(r["entity_b"]): r["valid_to"] for r in ended} == {
        name.lower(): replaced[name.lower()] for name in names
    }


async def test_a_value_less_close_counts_its_own_queued_closes_as_applied(
    ch, org, monkeypatch
) -> None:
    """Eight older values, each linking a pair the high row no longer names; two pages
    of two a pass, with the edge closes queued behind stopped merges: each pass reads
    its own closes as applied, so later passes reach every value."""
    from server.memory.app import edge_links, edge_repair, projections, triple_projections
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    names = ["Carol King", "Dave Ray", "Erin Holt", "Frank Moss"]
    names += ["Gina Park", "Hank Bell", "Ivy Chen", "Jack Ford"]
    with monkeypatch.context() as m:  # every close and drop lost: all edges stay open
        m.setattr(edge_links, "drop_undeclared_links", nothing)
        m.setattr(projections, "end_edges", nothing)
        m.setattr(triple_projections, "end_edges", nothing)
        for name in names:
            await _high_set(org, "s1", "vault", f"Alice met {name}", entities=["Alice Smith", name])
        out = await _high_set(
            org, "s1", "vault", "Orchid", sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    monkeypatch.setattr(edge_repair, "_VALUES_PAGE", 2)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGES", 2)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    # A busy mutation pool: the first pass's closes queue but do not apply.
    await ch.execute("SYSTEM STOP MERGES loom.entity_edges")
    try:
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    finally:
        await ch.execute("SYSTEM START MERGES loom.entity_edges")
    await _mutations_done(ch, org)
    for _ in range(4):
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert await _open_edge_pairs(ch, org) == {("alice smith", "bob lane")}


async def test_a_value_less_close_reaches_a_value_after_a_held_pair_lets_go(
    ch, org, monkeypatch
) -> None:
    """Values linking a pair another row holds, and one that can close; after a first
    pass the other row lets go of its pair: the closable value is still reached."""
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app import (
        after_write,
        edge_links,
        edge_repair,
        projections,
        triple_projections,
    )
    from server.memory.app.handlers import handle_memory_delete
    from server.memory.app.projection_errors import ProjectionScope
    from server.memory.app.projection_failures import retry_projection_failures

    async def nothing(*a: Any, **kw: Any) -> None:
        return None

    await _high_set(org, "s9", "other", "Alice and Dave met", entities=["Alice Smith", "Dave Ray"])
    with monkeypatch.context() as m:  # every close and drop lost: all edges stay open
        m.setattr(edge_links, "drop_undeclared_links", nothing)
        m.setattr(projections, "end_edges", nothing)
        m.setattr(triple_projections, "end_edges", nothing)
        for value in ("Alice and Dave note 6", "Alice and Dave note 22", "Alice and Dave note 37"):
            await _high_set(org, "s1", "vault", value, entities=["Alice Smith", "Dave Ray"])
        await _high_set(
            org,
            "s1",
            "vault",
            "Alice met Carol about the plan",
            entities=["Alice Smith", "Carol King"],
        )
        out = await _high_set(
            org, "s1", "vault", "Orchid", sensitivity="high", entities=["Alice Smith", "Bob Lane"]
        )
    monkeypatch.setattr(edge_repair, "_VALUES_PAGE", 1)
    monkeypatch.setattr(edge_repair, "_VALUES_PAGES", 1)
    scope = ProjectionScope(org=org, namespace=NS, agent="bot", user_id=USER)
    close = edge_repair.EdgeClose(str(out.memory_id), "vault", "-", [], high=True)
    await edge_repair.record_edges_closes(scope, [close], RuntimeError("x"), op="memory.set", ch=ch)
    await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    # The other row lets go of Alice-Dave, and the edge ends: the values that linked it
    # leave the read set.
    await handle_memory_delete(
        MemoryDeleteInput(
            org=org, namespace=NS, agent="bot", user_id=USER, session_id="s9", memory_key="other"
        )
    )
    await after_write.drain()
    await ch.execute(
        "ALTER TABLE loom.entity_edges UPDATE valid_to = now() WHERE org = {org:String}"
        " AND entity_a = 'alice smith' AND entity_b = 'dave ray' SETTINGS mutations_sync = 2",
        params={"org": org},
    )
    for _ in range(3):
        await retry_projection_failures(org=org, projection="entity_edges_close", ch=ch)
    assert ("alice smith", "carol king") not in await _open_edge_pairs(ch, org)
