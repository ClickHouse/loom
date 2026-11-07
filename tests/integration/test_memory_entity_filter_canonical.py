"""`memory.set(entities=["Billing Services"])` then `memory.search(entity_filter=["billing
service"])` boosts the row: both sides meet in the canonical entity form. In-process
against a live ClickHouse; skips without LOOM_CH_HOST."""

from __future__ import annotations

import os
from typing import Any

import pytest

from tests.integration._eventually import eventually
from tests.integration._resources import unique_org

pytestmark = pytest.mark.asyncio

ORG = unique_org("entcanon")


async def test_a_plural_entity_meets_its_singular_filter(ch_client) -> None:
    from server.core.ch import get_client
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    await get_client().connect()
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

    await get_client().connect()
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

    await get_client().connect()
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

    await get_client().connect()
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


async def _connected() -> None:
    from server.core.ch import get_client

    await get_client().connect()


async def test_distinct_acronyms_stay_distinct(ch_client) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import handle_memory_set

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=ns,
            agent="a",
            memory_key="proto",
            value="the proxy speaks HTTP to the app and HTTPS to clients",
            entities=["HTTP", "HTTPS"],
        )
    )
    for table in ("loom.memory", "loom.memory_embeddings"):
        stored = ch_client.query(
            f"SELECT entities FROM {table} FINAL WHERE org = {{org:String}} "
            "AND namespace = {ns:String}",
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        assert stored == [(["http", "https"],)], table


@pytest.mark.parametrize("raw", ["re.S", "re.S.S.S.S.S"])
async def test_a_dotted_name_given_in_canonical_form_keeps_its_boost(ch_client, raw: str) -> None:
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
            entities=[canonical_entity_text(raw)],
        )
    )
    out = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=ns, agent="*", query="dot", entity_filter=[raw])
    )
    (hit,) = [h for h in out.results if h.memory_key == "flags"]
    assert hit.score_components["entity_boost_sql"] == 0.5


async def test_a_huge_entity_neither_slows_a_write_nor_a_search(ch_client) -> None:
    import time

    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    huge = "re" + ".S" * 16000
    started = time.perf_counter()
    await handle_memory_set(
        MemorySetInput(
            org=ORG, namespace=ns, agent="a", memory_key="huge", value="flags", entities=[huge]
        )
    )
    await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=ns, agent="*", query="flags", entity_filter=[huge])
    )
    assert time.perf_counter() - started < 3  # was ~14 s, the worker blocked throughout


@pytest.mark.parametrize(
    "names",
    [
        [
            "https://example.test/" + "x" * 179 + "/alpha",
            "https://example.test/" + "x" * 179 + "/beta",
        ],
        ["İstanbul/" + "a" * 191],
    ],
)
async def test_long_and_unicode_names_keep_their_identity_and_boost(ch_client, names) -> None:
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
            memory_key="names",
            value="two long names",
            entities=[canonical_entity_text(n) for n in names],
        )
    )
    stored = ch_client.query(
        "SELECT entities FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert stored == [([canonical_entity_text(n) for n in names],)]
    out = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=ns, agent="*", query="names", entity_filter=names)
    )
    (hit,) = [h for h in out.results if h.memory_key == "names"]
    assert hit.score_components["entity_boost_sql"] == 0.5


@pytest.mark.parametrize("raw", ["The .NET Services", ". the . a .NET services"])
async def test_a_name_whose_article_hides_a_dot_keeps_its_boost_and_graph_path(
    ch_client, raw: str
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_facts import FactsPathInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.facts import handle_facts_path
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set
    from server.memory.domain.graph_quality import canonical_entity_text

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    canonical = canonical_entity_text(raw)
    assert canonical == "net service"
    for key, entities in (("raw", [raw, "Latency"]), ("canonical", [canonical, "latency"])):
        await handle_memory_set(
            MemorySetInput(
                org=ORG,
                namespace=ns,
                agent="a",
                memory_key=key,
                value=".NET services add latency under load",
                entities=entities,
            )
        )
    out = await handle_memory_search(
        MemorySearchInput(org=ORG, namespace=ns, agent="*", query="latency", entity_filter=[raw])
    )
    boosts = {h.memory_key: h.score_components["entity_boost_sql"] for h in out.results}
    assert boosts == {"raw": 0.5, "canonical": 0.5}
    for seed in (raw, canonical):
        # The edges land through an async insert that the write does not wait for.
        async def _path(seed: str = seed) -> Any:
            got = await handle_facts_path(
                FactsPathInput(org=ORG, namespace=ns, entity_a=seed, entity_b="Latency")
            )
            return got if got.found else None

        path = await eventually(_path, description=f"a graph path from {seed!r}")
        assert path.path == ["net service", "latency"], seed


async def test_a_name_too_long_to_bind_is_dropped_on_write_and_search(ch_client) -> None:
    # Over 128 KiB, a name cannot be sent as a query parameter: neither side keeps it.
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.handler import handle_memory_search
    from server.memory.app.write import handle_memory_set

    await _connected()
    ns = f"agent-entcanon-{os.urandom(3).hex()}"
    huge = "Zed" + "x" * 200_000
    await handle_memory_set(
        MemorySetInput(
            org=ORG,
            namespace=ns,
            agent="a",
            memory_key="huge",
            value=f"{huge} met Bob Lane",
            entities=[huge, "Bob Lane"],
        )
    )
    stored = ch_client.query(
        "SELECT entities FROM loom.memory FINAL WHERE org = {org:String} AND namespace = {ns:String}",
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert stored == [(["bob lane"],)]
    out = await handle_memory_search(
        MemorySearchInput(
            org=ORG, namespace=ns, agent="*", query="Bob Lane", entity_filter=[huge, "Bob Lane"]
        )
    )
    assert "huge" in {h.memory_key for h in out.results}
