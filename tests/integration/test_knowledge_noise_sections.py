"""A meeting-notes document written through knowledge.upsert: its bookkeeping section is
stored labelled noise and its decision section is not, and knowledge.search reports the
label on each hit."""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.core.hashing import sha256_document_text  # noqa: E402
from server.knowledge.app.writes import _chunk_text  # noqa: E402
from server.knowledge.domain.chunking import chunk_context, embedding_text  # noqa: E402
from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("noise")

TITLE = "Storage sync 2026-03-04"
NOTES = """# Storage sync 2026-03-04

## Attendees

- Ana
- Tomas
- Kai

## Cache

- Agreed: drop the block cache of the disk set; its memory cost is too high for the gain.
"""


@pytest.mark.asyncio
async def test_a_notes_section_is_stored_and_served_as_noise(control_plane_url, ch_client) -> None:
    from loom import Agent

    ns = f"noise-{uuid.uuid4().hex[:6]}"
    src = f"doc://sync-{uuid.uuid4().hex[:6]}"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.upsert(content=NOTES, source_uri=src, title=TITLE)
        out = await a.knowledge.search(
            query="who attended the storage sync and what about the block cache",
            namespace_scope="this",
            top_k=5,
        )

    rows = ch_client.query(
        "SELECT chunk_idx, content, context, tags, doc_metadata, chunk_metadata, agent, "
        "user_id, session_id, source_class, keys FROM loom.chunks WHERE org = {org:String} "
        "AND namespace = {ns:String} AND source_uri = {su:String} AND "
        + LIVE_CHUNK_SQL
        + " ORDER BY chunk_idx",
        parameters={"org": ORG, "ns": ns, "su": src},
    ).result_rows
    # The fields the row takes from the request: the chunk's text and context, the document's tags,
    # metadata and provenance, and the chunker's section metadata with the first revision,
    # the format and the label on the attendee list alone.
    labels = [{"noise": True}, {}]
    chunks = _chunk_text(NOTES)
    expected = [
        (
            c.idx,
            c.text,
            chunk_context(TITLE, "", c.breadcrumb),
            [],
            "{}",
            {**c.meta, "revision": 1, "format": "", **label},
            "-",
            "-",
            "-",
            "asserted",
            [],
        )
        for c, label in zip(chunks, labels, strict=True)
    ]
    assert [(*r[:5], json.loads(r[5]), *r[6:]) for r in rows] == expected
    # And the columns the write derives: one document version, embedding model and write
    # time on every chunk, a vector of the recorded width, and the two content hashes.
    derived = ch_client.query(
        "SELECT doc_version, embedding_model, embedding_dim, length(embedding), "
        "content_sha256, embed_input_sha256, ingested_at, created_at, doc_superseded_at "
        "FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String} "
        "AND source_uri = {su:String} AND " + LIVE_CHUNK_SQL + " ORDER BY chunk_idx",
        parameters={"org": ORG, "ns": ns, "su": src},
    ).result_rows
    assert len({(r[0], r[1], r[6]) for r in derived}) == 1
    for c, (version, model, dim, width, content_sha, input_sha, at, created, gone) in zip(
        chunks, derived, strict=True
    ):
        context = chunk_context(TITLE, "", c.breadcrumb)
        assert version > 0 and model and dim == width > 0
        assert content_sha == sha256_document_text(c.text)
        assert input_sha == hashlib.sha256(embedding_text(context, c.text).encode()).hexdigest()
        assert (created, gone) == (at, None)

    hits = {h.section_path: h for h in out}
    assert hits[("Storage sync 2026-03-04", "Attendees")].noise is True
    assert hits[("Storage sync 2026-03-04", "Cache")].noise is False


@pytest.mark.asyncio
async def test_knowledge_add_and_bulk_add_label_by_their_document_title(
    control_plane_url, ch_client
) -> None:
    """The same text is noise under a dated notes title and content under a design title,
    written by knowledge.add and by knowledge.bulk_add alike."""
    from loom import Agent

    ns = f"noise-{uuid.uuid4().hex[:6]}"
    text = "# Disk set\n\nThe disk set keeps one Bloom filter per block.\n"
    docs = {
        f"doc://add-notes-{uuid.uuid4().hex[:6]}": TITLE,
        f"doc://add-design-{uuid.uuid4().hex[:6]}": "Disk set design",
        f"doc://bulk-notes-{uuid.uuid4().hex[:6]}": TITLE,
        f"doc://bulk-design-{uuid.uuid4().hex[:6]}": "Disk set design",
    }
    adds = [u for u in docs if "//add-" in u]
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        for uri in adds:
            await a.knowledge.add(text, source_uri=uri, title=docs[uri])
        await a.knowledge.bulk_add(
            [
                {"content": text, "source_uri": u, "title": t}
                for u, t in docs.items()
                if u not in adds
            ]
        )

    rows = ch_client.query(
        "SELECT source_uri, JSONExtractBool(chunk_metadata, 'noise') FROM loom.chunks "
        "WHERE org = {org:String} AND namespace = {ns:String} AND " + LIVE_CHUNK_SQL,
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    labels = dict(rows)
    assert labels == {u: t == TITLE for u, t in docs.items()}
