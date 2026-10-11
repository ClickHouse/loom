"""`memory.set(entities=["Billing Services"])` then `memory.search(entity_filter=["billing
service"])` boosts the row: both sides meet in the canonical entity form. In-process
against a live ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import os

import pytest

from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("entcanon")


async def test_a_plural_entity_meets_its_singular_filter(ch_client) -> None:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=ns,
            agent="a",
            memory_key="billing-owner",
            value="the invoices team owns the billing services rollout",
            entities=["Billing Services"],
        )
    )
    out = await handle_memory_search(
        MemorySearchInput(
            org=ORG,
            namespace=ns,
            agent="*",
            query="who owns the rollout",
            entity_filter=["billing service"],
        )
    )
    (hit,) = [h for h in out.results if h.memory_key == "billing-owner"]
    assert hit.score_components["entity_boost_sql"] == 0.5  # every filter entity matched
    stored = ch_client.query(
        "SELECT entities FROM loom.memory FINAL WHERE org = {org:String} "
        "AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert stored == [(["billing service"],)]


async def test_a_timeline_entity_matches_canonical_and_older_lowercase_rows(ch_client) -> None:
    import server.memory.store as memory_repo
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_temporal import MemoryTimelineInput
    from server.memory.app.curation import handle_memory_timeline
    from server.memory.app.write import handle_memory_set

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    for key in ("shipped", "older"):
        await handle_memory_set(
            MemorySetInput(
                org=ORG,
                namespace=ns,
                agent="a",
                memory_key=key,
                value=f"the widgets {key}",
                entities=["widgets"],
            )
        )
    # `older` as rows written before entities were canonical: its mirror holds "widgets"
    rows = await memory_repo.live_embedding_rows_for_key(
        get_client(),
        org=ORG,
        namespace=ns,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="older",
    )
    legacy = [{**r, "entities": ["widgets"], "version": int(r["version"]) + 1} for r in rows]
    await get_client().insert("loom.memory_embeddings", legacy)
    found = await handle_memory_timeline(
        MemoryTimelineInput(org=ORG, namespace=ns, agent="*", entity="widgets")
    )
    assert sorted(e.memory_key for e in found.events) == ["older", "shipped"]


async def test_a_touch_keeps_entities_and_their_boost(ch_client) -> None:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput, MemoryTouchInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.handlers import handle_memory_touch
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    ident = {"org": ORG, "namespace": ns, "agent": "a", "memory_key": "api-shape"}
    await handle_memory_set(
        MemorySetInput(
            **ident,
            value="api responses carry a next cursor",
            entities=["API Responses", "sql databases"],
        )
    )
    await handle_memory_touch(MemoryTouchInput(**ident, ttl_seconds=3600))
    for table in ("loom.memory", "loom.memory_embeddings"):
        stored = ch_client.query(
            f"SELECT entities, expires_at IS NOT NULL FROM {table} FINAL "
            "WHERE org = {org:String} AND namespace = {ns:String}",
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        assert stored == [(["api response", "sql database"], True)], table
    out = await handle_memory_search(
        MemorySearchInput(
            org=ORG, namespace=ns, agent="*", query="cursor", entity_filter=["api responses"]
        )
    )
    (hit,) = [h for h in out.results if h.memory_key == "api-shape"]
    assert hit.score_components["entity_boost_sql"] == 0.5


async def test_healing_a_legacy_pair_converges(ch_client) -> None:
    """A row stored before entities were canonical, with a stale mirror: the heal copies the
    source's entities as stored, so the pair agrees and the next pass writes nothing."""
    import server.memory.store as memory_repo
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await handle_memory_set(
        MemorySetInput(
            org=ORG, namespace=ns, agent="a", memory_key="legacy", value="billing", entities=["x"]
        )
    )
    p = {"org": ORG, "ns": ns}
    where = "WHERE org = {org:String} AND namespace = {ns:String}"
    for table, ents in (("loom.memory", "['billing services']"), ("loom.memory_embeddings", "[]")):
        ch_client.command(
            f"INSERT INTO {table} SELECT * REPLACE ({ents} AS entities, version + 1 AS version) "
            f"FROM {table} FINAL {where}",
            parameters=p,
        )

    def mirror() -> list[tuple]:
        return ch_client.query(
            f"SELECT max(version), argMax(entities, version) FROM loom.memory_embeddings {where}",
            parameters=p,
        ).result_rows

    await memory_repo.heal_divergent_entities(limit=10_000)
    ((versions, entities),) = mirror()
    assert entities == ["billing services"]
    for _ in range(2):
        await memory_repo.heal_divergent_entities(limit=10_000)
        assert mirror() == [(versions, entities)]


async def _legacy(ch_client, ns: str, key: str, value: str, entities: list[str]) -> None:
    """A memory as an earlier normaliser left it: `entities` stored as given."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    await handle_memory_set(
        MemorySetInput(
            org=ORG, namespace=ns, agent="a", memory_key=key, value=value, entities=["x"]
        )
    )
    for table in ("loom.memory", "loom.memory_embeddings"):
        ch_client.command(
            f"INSERT INTO {table} SELECT * REPLACE ({{ents:Array(String)}} AS entities, "
            f"version + 1 AS version) FROM (SELECT * FROM {table} FINAL WHERE org = {{org:String}} "
            "AND namespace = {ns:String} AND memory_key = {key:String})",
            parameters={"org": ORG, "ns": ns, "key": key, "ents": entities},
        )


def _edges(ch_client, ns: str, *pairs: tuple[str, str, int]) -> None:
    for a, b, n in pairs:
        ch_client.command(
            "INSERT INTO loom.entity_edges (org, namespace, entity_a, entity_b, co_occur) "
            "VALUES ({org:String}, {ns:String}, {a:String}, {b:String}, {n:UInt32})",
            parameters={"org": ORG, "ns": ns, "a": a, "b": b, "n": n},
        )
        ch_client.command(
            "INSERT INTO loom.entity_edge_memories (org, namespace, entity_a, entity_b, memory_key) "
            "VALUES ({org:String}, {ns:String}, {a:String}, {b:String}, 'k')",
            parameters={"org": ORG, "ns": ns, "a": a, "b": b},
        )


def _rows(ch_client, sql: str, ns: str) -> list[tuple]:
    return ch_client.query(sql, parameters={"org": ORG, "ns": ns}).result_rows


_WHERE = "WHERE org = {org:String} AND namespace = {ns:String}"


def _stored(ch_client, ns: str) -> list[list[tuple]]:
    return [
        _rows(
            ch_client,
            f"SELECT memory_key, entities FROM {t} FINAL {_WHERE} AND tombstoned_at IS NULL "
            "ORDER BY memory_key",
            ns,
        )
        for t in ("loom.memory", "loom.memory_embeddings")
    ]


# what memory.set projects from a value of its own is not what these tests seeded
_SEEDED = {"api respons", "api response", "latency", "payload siz", "payload size", "dns", "tls"}
_SEEDED |= {"cas", "case", "closed"}


def _graph(ch_client, ns: str) -> list[tuple]:
    rows = _rows(
        ch_client,
        f"SELECT entity_a, entity_b, sum(co_occur) FROM loom.entity_edges {_WHERE} "
        "GROUP BY entity_a, entity_b ORDER BY entity_a, entity_b",
        ns,
    )
    return [r for r in rows if {r[0], r[1]} <= _SEEDED]


def _degrees(ch_client, ns: str) -> dict[str, int]:
    rows = _rows(
        ch_client,
        f"SELECT entity, sum(degree) FROM loom.entity_degree {_WHERE} GROUP BY entity",
        ns,
    )
    return {e: int(n) for e, n in rows if e in _SEEDED}


async def _connected() -> None:
    from server.core.ch import get_client

    try:
        await get_client().connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"clickhouse unavailable: {e}")


async def test_the_migration_moves_stored_entities_to_the_current_form(ch_client, tmp_path) -> None:
    """Old stems a memory's text says the plural of (`api respons`, `payload siz`), and a
    lowercased plural (`kits`), move; names with no such evidence (`dns`, `tls`) stay."""
    from server.memory.api.models_facts import FactsPathInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.entity_migration import migrate_entities
    from server.memory.app.facts import handle_facts_path
    from server.memory.app.search.handler import handle_memory_search

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await _legacy(ch_client, ns, "api", "api responses page the kits", ["api respons", "kits"])
    await _legacy(
        ch_client, ns, "pay", "payload sizes grow with latency", ["payload siz", "latency"]
    )
    _edges(
        ch_client,
        ns,
        ("latency", "payload siz", 2),
        ("api respons", "latency", 1),
        ("dns", "tls", 1),
    )
    state = tmp_path / "state.json"
    planned = await migrate_entities(apply=False, state=state)
    assert planned["loom.memory"] >= 2 and planned["loom.entity_edges"] >= 2
    assert not state.exists()  # a dry run writes nothing
    assert _stored(ch_client, ns)[0] == [
        ("api", ["api respons", "kits"]),
        ("pay", ["payload siz", "latency"]),
    ]
    await migrate_entities(apply=True, state=state)
    assert not state.exists()
    assert (
        _stored(ch_client, ns)
        == [[("api", ["api response", "kit"]), ("pay", ["payload size", "latency"])]] * 2
    )
    assert _graph(ch_client, ns) == [
        ("api response", "latency", 1),
        ("dns", "tls", 1),
        ("latency", "payload size", 2),
    ]
    degrees = _degrees(ch_client, ns)
    assert {e: degrees.get(e) for e in ("api response", "payload size", "dns", "tls")} == {
        "api response": 1,
        "payload size": 2,
        "dns": 1,
        "tls": 1,
    }
    assert not {"api respons", "payload siz"} & set(degrees)
    out = await handle_memory_search(
        MemorySearchInput(
            org=ORG, namespace=ns, agent="*", query="page", entity_filter=["api responses"]
        )
    )
    (hit,) = [h for h in out.results if h.memory_key == "api"]
    assert hit.score_components["entity_boost_sql"] == 0.5
    path = await handle_facts_path(
        FactsPathInput(org=ORG, namespace=ns, entity_a="payload sizes", entity_b="latency")
    )
    assert path.found
    after = await migrate_entities(apply=False, state=state)
    assert all(n == 0 for n in after.values())


async def test_an_interrupted_migration_resumes_without_duplicates(
    ch_client, tmp_path, monkeypatch
) -> None:
    """A failure after a graph copy, before the old rows go, or before the memory rows
    move: the retry adds no observation twice and keeps the saved mapping."""
    from server.memory.app import entity_migration as app
    from server.memory.store import entity_migration as store

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await _legacy(ch_client, ns, "api", "api responses page", ["api respons"])
    _edges(ch_client, ns, ("api respons", "latency", 2))
    state = tmp_path / "state.json"
    real_rename, real_reemit = store.rename_graph, store.reemit_memory
    calls = {"rename": 0}

    async def rename_then_fail(table, cols, pair, scope, mapping, *, ch=None):
        await real_rename(table, cols, pair, scope, mapping, ch=ch)
        if table == "loom.entity_edges" and scope[1] == ns and not calls["rename"]:
            calls["rename"] += 1
            # the copy is in; put the old rows back as if the delete had failed
            _edges(ch_client, ns, ("api respons", "latency", 2))
            raise RuntimeError("delete failed")

    async def reemit_fails(*args, **kw):
        raise RuntimeError("memory step failed")

    monkeypatch.setattr(store, "rename_graph", rename_then_fail)
    with pytest.raises(RuntimeError):
        await app.migrate_entities(apply=True, state=state)
    assert state.exists()
    monkeypatch.setattr(store, "reemit_memory", reemit_fails)
    with pytest.raises(RuntimeError):
        await app.migrate_entities(apply=True, state=state)
    monkeypatch.setattr(store, "reemit_memory", real_reemit)
    await app.migrate_entities(apply=True, state=state)
    assert _graph(ch_client, ns) == [("api response", "latency", 2)]  # not 4
    # the memory step ran after the graph lost its old text: the saved mapping decides
    assert _stored(ch_client, ns)[0] == [("api", ["api response"])]


async def test_the_mapping_stays_in_its_namespace(ch_client, tmp_path) -> None:
    from server.memory.app.entity_migration import migrate_entities

    await _connected()
    old = f"agent-entcanon-{os.urandom(3).hex()}"
    acronym = f"agent-entcanon-{os.urandom(3).hex()}"
    await _legacy(ch_client, old, "c", "two cases closed", ["cas"])
    _edges(ch_client, old, ("cas", "closed", 1))
    await _legacy(ch_client, acronym, "c", "CAS swaps values atomically", ["cas"])
    await migrate_entities(apply=True, state=tmp_path / "state.json")
    assert _stored(ch_client, old)[0] == [("c", ["case"])]
    assert _stored(ch_client, acronym)[0] == [("c", ["cas"])]


async def test_a_pair_that_becomes_one_entity_is_dropped(ch_client, tmp_path) -> None:
    from server.memory.app.entity_migration import migrate_entities

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await _legacy(ch_client, ns, "c", "the cases and the case", ["cas", "case"])
    _edges(ch_client, ns, ("cas", "case", 1), ("case", "latency", 1))
    await migrate_entities(apply=True, state=tmp_path / "state.json")
    assert _graph(ch_client, ns) == [("case", "latency", 1)]
    pairs = _rows(
        ch_client,
        f"SELECT entity_a, entity_b FROM loom.entity_edge_memories FINAL {_WHERE} ORDER BY entity_a",
        ns,
    )
    assert pairs == [("case", "latency")]
    degrees = _degrees(ch_client, ns)
    assert degrees == {"case": 1, "latency": 1}


async def test_an_interrupted_degree_rebuild_is_repaired_by_the_next_run(
    ch_client, tmp_path, monkeypatch
) -> None:
    from server.memory.app import entity_migration as app
    from server.memory.store import entity_migration as store

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await _legacy(ch_client, ns, "api", "api responses page", ["api respons"])
    _edges(ch_client, ns, ("api respons", "latency", 2))
    real = store.rebuild_degrees

    async def wipe_only(scope, *, ch=None):
        if scope[1] != ns:
            return await real(scope, ch=ch)
        ch_client.command(
            f"DELETE FROM loom.entity_degree {_WHERE}", parameters={"org": ORG, "ns": ns}
        )
        raise RuntimeError("insert failed")

    monkeypatch.setattr(store, "rebuild_degrees", wipe_only)
    with pytest.raises(RuntimeError):
        await app.migrate_entities(apply=True, state=tmp_path / "state.json")
    monkeypatch.setattr(store, "rebuild_degrees", real)
    await app.migrate_entities(apply=True, state=tmp_path / "state.json")
    degrees = _degrees(ch_client, ns)
    assert degrees == {"api response": 2, "latency": 2}


async def test_a_dotted_name_given_in_canonical_form_keeps_its_boost(ch_client) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.graph_quality import canonical_entity_text

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=ns,
            agent="a",
            memory_key="flags",
            value="re.S lets the dot match newlines",
            entities=[canonical_entity_text("re.S")],
        )
    )
    out = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=ns, agent="*", query="dot", entity_filter=["re.S"])
    )
    (hit,) = [h for h in out.results if h.memory_key == "flags"]
    assert hit.score_components["entity_boost_sql"] == 0.5
