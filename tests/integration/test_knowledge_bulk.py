"""knowledge.bulk_add — batched document ingestion."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))


from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kbulk")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


@pytest.mark.asyncio
async def test_bulk_add_writes_all_docs(control_plane_url: str, ch_client) -> None:
    from loom import Agent

    ns = f"kb-{uuid.uuid4().hex[:6]}"
    entries = [
        {
            "content": f"doc body {i}",
            "source_uri": f"doc://{ns}/{i}",
            "title": f"t-{i}",
            "tags": ["batch"],
        }
        for i in range(5)
    ]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.knowledge.bulk_add(entries)
    assert len(result["written"]) == 5
    for w in result["written"]:
        assert w["chunks_created"] >= 1
        assert w["doc_id"]
    uris = {w["source_uri"] for w in result["written"]}
    assert uris == {e["source_uri"] for e in entries}

    # CH check
    rows = ch_client.query(
        """
        SELECT count() FROM loom.documents FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND superseded_at IS NULL
        """,
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert int(rows[0][0]) == 5


@pytest.mark.asyncio
async def test_bulk_add_searchable(control_plane_url: str) -> None:
    """Docs added via bulk_add are findable via search."""
    from loom import Agent

    ns = f"kb-srch-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.bulk_add(
            [
                {
                    "content": "clickhouse columnar fast",
                    "source_uri": f"doc://{ns}/a",
                    "title": "a",
                },
                {
                    "content": "python interpreter",
                    "source_uri": f"doc://{ns}/b",
                    "title": "b",
                },
            ]
        )
        hits = await a.knowledge.search("clickhouse columnar", top_k=5)
    uris = {h.source_uri for h in hits}
    assert any("/a" in u for u in uris)


@pytest.mark.asyncio
async def test_bulk_add_long_docs_chunk(control_plane_url: str, ch_client) -> None:
    """Sliding-window chunking applies per-doc in bulk_add."""
    from loom import Agent

    ns = f"kb-chnk-{uuid.uuid4().hex[:6]}"
    long_body = "ClickHouse is columnar. " * 150  # > 1500 chars (the default chunk size)
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.knowledge.bulk_add(
            [
                {
                    "content": long_body,
                    "source_uri": f"doc://{ns}/long",
                    "title": "long",
                },
                {
                    "content": "short",
                    "source_uri": f"doc://{ns}/short",
                    "title": "short",
                },
            ]
        )
    by_uri = {w["source_uri"]: w for w in result["written"]}
    assert by_uri[f"doc://{ns}/long"]["chunks_created"] > 1
    assert by_uri[f"doc://{ns}/short"]["chunks_created"] == 1


@pytest.mark.asyncio
async def test_bulk_add_empty_rejected(control_plane_url: str) -> None:
    """Pydantic validates min_length=1."""
    from loom import Agent
    from loom.errors import ValidationError

    ns = f"kb-empty-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(ValidationError):
            await a.knowledge.bulk_add([])
