"""knowledge.bulk_delete — batched tombstoning by source_uri."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kbdel")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


def _live_count(ch_client, ns: str) -> int:
    rows = ch_client.query(
        """
        SELECT count() FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    return int(rows[0][0])


@pytest.mark.asyncio
async def test_bulk_delete_tombstones_all_listed(
    control_plane_url,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"kbd-{uuid.uuid4().hex[:6]}"
    uris = [f"doc://{ns}/{i}" for i in range(4)]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for u in uris:
            await a.knowledge.add(content=f"body {u}", source_uri=u, title="t")
        assert _live_count(ch_client, ns) == 4

        result = await a.knowledge.bulk_delete(uris[:3])
    assert result["total_chunks_tombstoned"] == 3
    assert {r["source_uri"] for r in result["deleted"]} == set(uris[:3])
    assert _live_count(ch_client, ns) == 1  # only uris[3] survives


@pytest.mark.asyncio
async def test_bulk_delete_search_hides_them(control_plane_url: str) -> None:
    from loom import Agent

    ns = f"kbd-s-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        keep = f"doc://{ns}/keep"
        gone = [f"doc://{ns}/gone-{i}" for i in range(3)]
        await a.knowledge.add(content="unique search needle", source_uri=keep, title="keep")
        for u in gone:
            await a.knowledge.add(content="unique search needle", source_uri=u, title="x")

        await a.knowledge.bulk_delete(gone)
        hits = await a.knowledge.search("unique search needle", top_k=10)
    uris = {h.source_uri for h in hits}
    assert keep in uris
    assert not (uris & set(gone))


@pytest.mark.asyncio
async def test_bulk_delete_idempotent_on_missing(control_plane_url: str) -> None:
    """Source_uris that don't exist → chunks_tombstoned=0, total=0."""
    from loom import Agent

    ns = f"kbd-miss-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.knowledge.bulk_delete(
            [f"doc://ghost-{uuid.uuid4().hex[:6]}" for _ in range(3)]
        )
    assert result["total_chunks_tombstoned"] == 0
    for r in result["deleted"]:
        assert r["chunks_tombstoned"] == 0


@pytest.mark.asyncio
async def test_bulk_delete_mixed_existing_and_missing(
    control_plane_url,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"kbd-mix-{uuid.uuid4().hex[:6]}"
    real = f"doc://{ns}/real"
    ghost = f"doc://{ns}/ghost"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri=real, title="real")
        result = await a.knowledge.bulk_delete([real, ghost])
    by_uri = {r["source_uri"]: r for r in result["deleted"]}
    assert by_uri[real]["chunks_tombstoned"] >= 1
    assert by_uri[ghost]["chunks_tombstoned"] == 0
