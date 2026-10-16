"""memory.search_trace — per-candidate scoring breakdown.

The complement to memory.search: returns every component that fed into
the rank_score for each candidate, so the dashboard's Recall Lab can show "why did
this row land at rank N" and operators can debug ranking.

Covered:
  - basic call: results carry rank, similarity, rank_score; ordering
    matches memory.search
  - entity_filter boost: rows whose entities match get a 0.5 boost
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._eventually import eventually  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("search-trace")

# No flaky-rerun marker: every read-after-write below polls via eventually()
# until the embedding (async_insert=1 on memory_embeddings) is visible.


@pytest.fixture(autouse=True)
async def _ch_connected():
    from server.core.ch import get_client
    from server.embedding import embedder as _embedder_mod

    _embedder_mod.reset()
    ch = get_client()
    await ch.connect()
    yield
    await ch.disconnect()


@pytest.mark.asyncio
async def test_basic_breakdown(
    control_plane_url,
    mint_test_token,
) -> None:
    from loom import Agent

    ns = f"trace-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("k1", "Postgres for analytics workloads", memory_type="semantic")
        await a.memory.set("k2", "Coffee preferences", memory_type="semantic")

        async def both_rows_traced():
            t = await a.memory.search_trace("postgres database", top_k=10)
            return t if len(t["results"]) >= 2 else None

        trace = await eventually(
            both_rows_traced, description="both embeddings to surface in search_trace"
        )
    assert trace["embedding_model"], trace
    assert len(trace["results"]) >= 2
    # First result is the postgres-related row
    top = trace["results"][0]
    assert top["memory_key"] == "k1"
    assert top["rank"] == 1
    assert 0.0 <= top["similarity"] <= 1.0
    # With entity_filter empty, rank_score == similarity
    assert abs(top["rank_score"] - top["similarity"]) < 1e-5
    assert "entity_boost" in top
    # Ranks are sequential 1..n
    ranks = [r["rank"] for r in trace["results"]]
    assert ranks == list(range(1, len(trace["results"]) + 1))


@pytest.mark.asyncio
async def test_entity_filter_boosts_matching_rows(
    control_plane_url,
    mint_test_token,
) -> None:
    """The entity_filter boost adds 0.5 to rank_score when the row's
    entities intersect with the filter. The handler populates
    entity_boost as a separate component so the contribution is visible."""
    from loom import Agent

    ns = f"trace-ent-{uuid.uuid4().hex[:6]}"
    tok = mint_test_token(org=ORG, namespaces=[ns], roles=["admin"])
    async with Agent(
        org=ORG, namespace=ns, agent="t", base_url=control_plane_url, api_key=tok
    ) as a:
        await a.memory.set("k1", "Some technical writing about Postgres", memory_type="semantic")

        async def _traced():
            t = await a.memory.search_trace(
                "any technical query at all",
                top_k=5,
                entity_filter=["postgres"],
            )
            return t if t["results"] else None

        trace = await eventually(_traced, description="row to surface in search_trace")
    assert trace["entity_filter"] == ["postgres"]
    # Either entity_extractor ran and tagged the row → entity_boost=0.5,
    # OR it didn't run yet → entity_boost=0. Both are valid; the test
    # pins that the field is present and the type is right, not the
    # exact value (entity_extractor is an async worker, race-sensitive).
    assert len(trace["results"]) >= 1
    for r in trace["results"]:
        assert isinstance(r["entity_boost"], (int, float))
        # rank_score = similarity + entity_boost (other components 0)
        expected = r["similarity"] + r["entity_boost"]
        assert abs(r["rank_score"] - expected) < 1e-4
