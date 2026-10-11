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


async def test_the_migration_moves_stored_entities_to_the_current_form(ch_client) -> None:
    """Rows an earlier normaliser stored (`api respons`, `payload siz`) and lowercased
    rows (`kits`): once migrated, the plural filters and graph paths find them, and a
    second run has nothing left to do."""
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_facts import FactsPathInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.entity_migration import migrate_entities
    from server.memory.app.facts import handle_facts_path
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
            memory_key="api",
            value="api responses page",
            entities=["x"],
        )
    )
    p = {"org": ORG, "ns": ns}
    where = "WHERE org = {org:String} AND namespace = {ns:String}"
    for table in ("loom.memory", "loom.memory_embeddings"):
        ch_client.command(
            f"INSERT INTO {table} SELECT * REPLACE (['api respons', 'kits'] AS entities, "
            f"version + 1 AS version) FROM (SELECT * FROM {table} FINAL {where})",
            parameters=p,
        )
    ch_client.command(
        "INSERT INTO loom.entity_edges (org, namespace, entity_a, entity_b, co_occur) VALUES "
        "({org:String}, {ns:String}, 'latency', 'payload siz', 2), "
        "({org:String}, {ns:String}, 'api respons', 'latency', 1)",
        parameters=p,
    )

    def stored() -> list[tuple]:
        return [
            ch_client.query(
                f"SELECT entities FROM {t} FINAL {where} AND tombstoned_at IS NULL", parameters=p
            ).result_rows
            for t in ("loom.memory", "loom.memory_embeddings")
        ]

    planned = await migrate_entities(apply=False)
    assert planned["loom.memory"] >= 1 and planned["loom.entity_edges"] >= 2
    assert stored() == [[(["api respons", "kits"],)]] * 2  # a dry run writes nothing
    await migrate_entities(apply=True)
    assert stored() == [[(["api response", "kit"],)]] * 2
    degrees = ch_client.query(
        f"SELECT entity, sum(degree) FROM loom.entity_degree {where} GROUP BY entity ORDER BY entity",
        parameters=p,
    ).result_rows
    seeded = {"api respons", "api response", "latency", "payload siz", "payload size"}
    assert [d for d in degrees if d[0] in seeded] == [
        ("api response", 1),
        ("latency", 3),
        ("payload size", 2),
    ]
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
    assert all(n == 0 for n in (await migrate_entities(apply=False)).values())
