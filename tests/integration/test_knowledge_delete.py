"""knowledge.delete — per-source-uri soft-delete for day-to-day curation.

Covers: tombstone behavior, search invisibility, idempotency, isolation,
audit row emission.
"""

from __future__ import annotations

import asyncio
import sys
import uuid
from pathlib import Path

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kdel")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


def _live_chunk_count(ch_client, ns: str, source_uri: str) -> int:
    rows = ch_client.query(
        """
        SELECT count() FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri = {su:String} AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        parameters={"org": ORG, "ns": ns, "su": source_uri},
    ).result_rows
    return int(rows[0][0])


@pytest.mark.asyncio
async def test_delete_tombstones_chunks(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"kd-{uuid.uuid4().hex[:6]}"
    src = f"doc://x-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="Loom is a memory + knowledge platform.",
            source_uri=src,
            title="t",
        )
        assert _live_chunk_count(ch_client, ns, src) == 1

        result = await a.knowledge.delete(src)
    assert result["deleted"] is True
    assert result["chunks_tombstoned"] == 1
    assert _live_chunk_count(ch_client, ns, src) == 0


@pytest.mark.asyncio
async def test_deleted_doc_is_not_returned_by_search(
    control_plane_url: str,
) -> None:
    from loom import Agent

    ns = f"kd-srch-{uuid.uuid4().hex[:6]}"
    src_del = f"doc://del-{uuid.uuid4().hex[:6]}"
    src_keep = f"doc://keep-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="purple bananas are unusual",
            source_uri=src_del,
            title="del",
        )
        await a.knowledge.add(
            content="purple bananas are unusual",
            source_uri=src_keep,
            title="keep",
        )
        await a.knowledge.delete(src_del)

        hits = await a.knowledge.search("purple bananas", top_k=10)

    uris = {h.source_uri for h in hits}
    assert src_keep in uris
    assert src_del not in uris, "deleted doc must not surface in search"


@pytest.mark.asyncio
async def test_delete_idempotent_on_missing(control_plane_url: str) -> None:
    """Deleting a doc that was never added returns deleted=False, count=0."""
    from loom import Agent

    ns = f"kd-miss-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.knowledge.delete(f"doc://ghost-{uuid.uuid4().hex[:6]}")
    assert result["deleted"] is False
    assert result["chunks_tombstoned"] == 0


@pytest.mark.asyncio
async def test_second_delete_is_noop(control_plane_url: str, ch_client) -> None:
    """Deleting the same doc twice: the second call is a clean no-op."""
    from loom import Agent

    ns = f"kd-2x-{uuid.uuid4().hex[:6]}"
    src = f"doc://x-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri=src, title="t")
        first = await a.knowledge.delete(src)
        second = await a.knowledge.delete(src)
    assert first["deleted"] is True
    assert first["chunks_tombstoned"] == 1
    assert second["deleted"] is False
    assert second["chunks_tombstoned"] == 0


@pytest.mark.asyncio
async def test_delete_isolates_other_source_uris(
    control_plane_url: str,
    ch_client,
) -> None:
    from loom import Agent

    ns = f"kd-iso-{uuid.uuid4().hex[:6]}"
    src_a = f"doc://a-{uuid.uuid4().hex[:6]}"
    src_b = f"doc://b-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="A", source_uri=src_a, title="A")
        await a.knowledge.add(content="B", source_uri=src_b, title="B")
        await a.knowledge.delete(src_a)

    assert _live_chunk_count(ch_client, ns, src_a) == 0
    assert _live_chunk_count(ch_client, ns, src_b) == 1


@pytest.mark.asyncio
async def test_delete_emits_audit_row(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"kd-aud-{uuid.uuid4().hex[:6]}"
    src = f"doc://aud-{uuid.uuid4().hex[:6]}"

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="x", source_uri=src, title="t")
        await a.knowledge.delete(src)

    # Wait briefly for the audit row
    for _ in range(20):
        rows = ch_client.query(
            """
            SELECT count() FROM loom.audit_log
            WHERE org = {org:String} AND namespace = {ns:String}
              AND event_type = 'knowledge.delete'
            """,
            parameters={"org": ORG, "ns": ns},
        ).result_rows
        if int(rows[0][0]) >= 1:
            break
        await asyncio.sleep(0.1)
    assert int(rows[0][0]) >= 1, "knowledge.delete should emit an audit row"
