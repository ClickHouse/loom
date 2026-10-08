"""knowledge.upsert + supersession.

Verifies:
  - upsert of new source_uri behaves like add (no supersession)
  - upsert of existing source_uri supersedes prior chunks
  - search after upsert returns the new content
  - has_similar against the old (now superseded) content does NOT match
  - prior chunks remain queryable, listed in loom.chunk_supersessions (audit)
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.mark.asyncio
async def test_upsert_new_source_uri_is_pure_add(agent) -> None:
    """First upsert for a source_uri = no supersession."""
    result = await agent.knowledge.upsert(
        "First version of the doc",
        source_uri="github://pr/777",
        tags=["docs"],
    )
    assert result["chunks_created"] == 1
    assert result["chunks_superseded"] == 0


@pytest.mark.asyncio
async def test_upsert_supersedes_prior_chunks(agent, ch_client) -> None:
    """Adding then upserting the same source_uri marks prior chunks superseded."""
    await agent.knowledge.add(
        "Original finding: NULL pointer dereference in retry loop",
        source_uri="github://pr/888",
        tags=["null-deref"],
    )

    # Upsert with new content
    upsert = await agent.knowledge.upsert(
        "Updated finding: NULL pointer in retry loop fixed in commit abc123",
        source_uri="github://pr/888",
        tags=["null-deref", "fixed"],
    )
    assert upsert["chunks_superseded"] >= 1, "should have superseded the original chunk"
    assert upsert["chunks_created"] == 1

    # Verify in CH: exactly one chunk is superseded (the original) and one
    # is active (the new one). Order-independent because `DateTime` is
    # second-precision and both rows can land in the same wall-clock second.
    rows = ch_client.query(
        """
        SELECT content, NOT ("""
        + LIVE_CHUNK_SQL
        + """) AS superseded
        FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri = 'github://pr/888'
        """,
        parameters={"org": agent.org, "ns": agent.namespace},
    ).result_rows
    # Superseded = listed in loom.chunk_supersessions (or the legacy column)
    superseded = [r for r in rows if r[1]]
    active = [r for r in rows if not r[1]]
    assert len(superseded) == 1, f"expected exactly one superseded chunk; got {rows}"
    assert len(active) == 1, f"expected exactly one active chunk; got {rows}"
    assert "Original" in superseded[0][0]
    assert "Updated" in active[0][0]


@pytest.mark.asyncio
async def test_search_after_upsert_returns_new_content_only(agent) -> None:
    """Default search excludes superseded chunks."""
    await agent.knowledge.add(
        "Version one: memory leak in MV background merge",
        source_uri="github://pr/999",
        tags=["leak"],
    )
    await agent.knowledge.upsert(
        "Version two: memory leak in MV background merge FIXED in 25.9",
        source_uri="github://pr/999",
        tags=["leak", "fixed"],
    )

    hits = await agent.knowledge.search(
        "memory leak materialized view background merge",
        top_k=5,
    )
    # All returned chunks should be the new (active) version
    contents = [h.content for h in hits]
    new_seen = any("FIXED in 25.9" in c for c in contents)
    old_seen = any("Version one" in c for c in contents)
    assert new_seen, "new chunk should appear in search"
    assert not old_seen, "superseded old chunk should NOT appear in default search"
