"""knowledge.move: a document goes to another namespace whole (chunks, original,
metadata), is deleted where it was, and comes back there with knowledge.restore."""

from __future__ import annotations

import base64
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("kmove")


def _live(ch_client: Any, ns: str, uri: str) -> list[tuple[int, str, str]]:
    """The live chunks of a source: (chunk_idx, content, user_id)."""
    rows = ch_client.query(
        "SELECT chunk_idx, content, user_id FROM loom.chunks WHERE org = {org:String}"
        " AND namespace = {ns:String} AND source_uri = {uri:String} AND "
        + LIVE_CHUNK_SQL
        + " ORDER BY chunk_idx",
        parameters={"org": ORG, "ns": ns, "uri": uri},
    ).result_rows
    return [(int(r[0]), str(r[1]), str(r[2])) for r in rows]


def _places() -> tuple[str, str, str]:
    tag = uuid.uuid4().hex[:6]
    return f"km-a-{tag}", f"km-b-{tag}", f"doc://move-{tag}.md"


async def _move(a: Any, src: str, dst: str, uris: list[str]) -> dict[str, Any]:
    return await a.transport.post(
        "/v1/knowledge.move",
        {"org": ORG, "namespace": src, "to_namespace": dst, "source_uris": uris},
    )


@pytest.mark.asyncio
async def test_a_moved_document_keeps_its_chunks_original_and_metadata(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    text = "Drain a stalled ingest lane.\n\n" + "Replay the dead-letter queue. " * 80
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.transport.post(
            "/v1/knowledge.add",
            {
                "org": ORG,
                "namespace": src,
                "source_uri": uri,
                "content": text,
                "title": "Stalled lane",
                "description": "Runbook",
                "tags": ["runbook"],
                "original_bytes_b64": base64.b64encode(b"%PDF-original").decode(),
                "original_filename": "lane.pdf",
                "blob_content_type": "application/pdf",
            },
        )
        before = _live(ch_client, src, uri)
        assert len(before) > 1

        out = await _move(a, src, dst, [uri, uri])
        assert out["found"] is True
        assert out["to_namespace"] == dst
        assert out["results"] == [{"source_uri": uri, "moved": True, "reason": ""}]

        # The same chunks, by the same author, now live in the new place only.
        assert _live(ch_client, dst, uri) == before
        assert _live(ch_client, src, uri) == []
        there = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": dst, "limit": 10}
        )
        [row] = there["rows"]
        assert (row["source_uri"], row["title"], row["description"], row["tags"]) == (
            uri,
            "Stalled lane",
            "Runbook",
            ["runbook"],
        )
        assert (row["has_blob"], row["original_filename"]) == (True, "lane.pdf")
        listed = await a.transport.post(
            "/v1/knowledge.list", {"org": ORG, "namespace": src, "limit": 10}
        )
        assert [r["source_uri"] for r in listed["rows"]] == []

        # Searchable where it went, by its own text.
        hits = await a.transport.post(
            "/v1/knowledge.search",
            {
                "org": ORG,
                "namespace": dst,
                "query": "stalled ingest lane",
                "namespace_scope": "this",
            },
        )
        assert uri in {h["source_uri"] for h in hits["results"]}

        # Deleted where it was, so a restore there brings it back.
        restored = await a.knowledge.restore(uri)
        assert restored["restored"] is True
        assert _live(ch_client, src, uri) == before


@pytest.mark.asyncio
async def test_a_retry_finds_nothing_left_to_move(control_plane_url: str) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="once", source_uri=uri, title="t")
        assert (await _move(a, src, dst, [uri]))["results"][0]["moved"] is True
        again = await _move(a, src, dst, [uri])
    assert again["results"] == [{"source_uri": uri, "moved": False, "reason": "not_found"}]


@pytest.mark.asyncio
async def test_a_live_document_where_it_goes_is_not_overwritten(
    control_plane_url: str, ch_client: Any
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="mine here", source_uri=uri, title="here")
    async with Agent(org=ORG, namespace=dst, agent="w", base_url=control_plane_url) as b:
        await b.knowledge.add(content="theirs there", source_uri=uri, title="there")
        out = await _move(b, src, dst, [uri])
    assert out["results"] == [{"source_uri": uri, "moved": False, "reason": "exists"}]
    assert [c[1] for c in _live(ch_client, dst, uri)] == ["theirs there"]
    assert [c[1] for c in _live(ch_client, src, uri)] == ["mine here"]


@pytest.mark.asyncio
async def test_a_move_over_a_deleted_document_is_its_next_revision(
    control_plane_url: str,
) -> None:
    from loom import Agent

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=dst, agent="w", base_url=control_plane_url) as b:
        await b.knowledge.add(content="old text", source_uri=uri, title="old")
        await b.knowledge.delete(uri)
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content="new text", source_uri=uri, title="new")
        out = await _move(a, src, dst, [uri])
        assert out["results"][0]["moved"] is True
        got = await a.transport.post(
            "/v1/knowledge.get", {"org": ORG, "namespace": dst, "source_uri": uri}
        )
    assert (got["title"], got["revision"]) == ("new", 2)


@pytest.mark.asyncio
async def test_a_move_to_the_same_namespace_is_refused(control_plane_url: str) -> None:
    from loom import Agent
    from loom.errors import LoomError

    src, _, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        with pytest.raises(LoomError) as e:
            await _move(a, src, src, [uri])
    assert "VALIDATION" in str(e.value).upper()


@pytest.mark.asyncio
async def test_a_moved_document_brings_its_tables(control_plane_url: str, ch_client: Any) -> None:
    from loom import Agent

    from server.knowledge.store import datasets as repo

    src, dst, uri = _places()
    async with Agent(org=ORG, namespace=src, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(
            content="region,total\nnorth,3\nsouth,4", source_uri=uri, title="sales"
        )
        [[version]] = ch_client.query(
            "SELECT version FROM loom.documents FINAL WHERE org = {org:String}"
            " AND namespace = {ns:String} AND source_uri = {uri:String}",
            parameters={"org": ORG, "ns": src, "uri": uri},
        ).result_rows
        catalog = repo.dataset_row(
            None,
            org=ORG,
            namespace=src,
            source_uri=uri,
            dataset_id="ds_sales",
            doc_version=int(version),
            snapshot_id=7,
            revision=1,
            table_id="t1",
            position=0,
            name="sales",
            sheet="",
            row_start=1,
            row_end=3,
            schema="[]",
            schema_state="needs_clarification",
            clarifications="[]",
            row_count=2,
            rejected_rows=0,
            rejected="[]",
            complete=True,
            artifact_sha256="",
            agent="w",
            user_id="-",
            session_id="-",
            actor="",
            source_class="asserted",
        )
        rows = [
            repo.slot_row(catalog, {"row_id": i, "s1": region, "n1": n}, catalog["created_at"])
            for i, (region, n) in enumerate([("north", 3), ("south", 4)], start=1)
        ]
        for table, written in (("loom.datasets", [catalog]), ("loom.dataset_rows", rows)):
            ch_client.insert(
                table, [list(r.values()) for r in written], column_names=list(written[0])
            )

        assert (await _move(a, src, dst, [uri]))["results"][0]["moved"] is True
        got = await a.transport.post(
            "/v1/knowledge.dataset_get",
            {"org": ORG, "namespace": dst, "source_uri": uri, "sample_rows": 5},
        )
    assert got["found"] is True
    [table] = got["datasets"]
    assert table["name"] == "sales"
    rows = ch_client.query(
        "SELECT s1, toFloat64(n1) FROM loom.dataset_rows WHERE org = {org:String}"
        " AND namespace = {ns:String} AND dataset_id = 'ds_sales' ORDER BY row_id",
        parameters={"org": ORG, "ns": dst},
    ).result_rows
    assert [tuple(r) for r in rows] == [("north", 3.0), ("south", 4.0)]
