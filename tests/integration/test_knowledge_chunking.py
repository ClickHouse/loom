"""Chunking for knowledge.add / knowledge.upsert.

The pure chunker is verified inline (headless text takes the legacy sliding
window; headed documents get section-aware chunking);
end-to-end checks that a long-form document produces N chunks and search
returns results from the matching chunk index."""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

from server.knowledge.domain.live_chunks import LIVE_CHUNK_SQL

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "sdk" / "python"))
sys.path.insert(0, str(ROOT))

from server.knowledge.app.writes import _chunk_text  # noqa: E402
from tests.integration._resources import unique_org  # noqa: E402

ORG = unique_org("chunking")


# ch_client fixture comes from tests/conftest.py (env-aware so Cloud runs work)


def _pairs(chunks):  # (idx, text) view of the Chunk objects the chunker returns
    return [(c.idx, c.text) for c in chunks]


def test_short_content_is_one_chunk() -> None:
    chunks = _chunk_text("hello world")
    assert _pairs(chunks) == [(0, "hello world")]
    assert chunks[0].section_path == () and chunks[0].meta == {}


def test_long_content_is_windowed(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_chunk_size", 10)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 8)
    # 25 chars: idx0=[0:10], idx1=[8:18], idx2=[16:25]; loop terminates
    # because idx 2's window covers to end-of-string.
    text = "0123456789abcdefghij56789"  # len 25
    chunks = _pairs(_chunk_text(text))
    assert len(chunks) == 3
    assert chunks[0] == (0, text[0:10])
    assert chunks[1] == (1, text[8:18])
    assert chunks[2] == (2, text[16:25])
    # Windows overlap by (size - stride) = 2 chars
    assert text[8:10] in chunks[0][1] and text[8:10] in chunks[1][1]


def test_headed_content_chunks_by_section(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_chunk_size", 40)
    monkeypatch.setattr(settings, "knowledge_chunk_stride", 30)
    doc = "# Intro\n\nintro text\n\n## Details\n\ndetails text that is long enough to window twice over\n"
    chunks = _chunk_text(doc)
    assert [c.section_path for c in chunks][:1] == [("Intro",)]
    assert all(c.section_path in {("Intro",), ("Intro", "Details")} for c in chunks)
    assert not any("## Details" in c.text for c in chunks)  # no chunk straddles the heading
    assert [c.idx for c in chunks] == list(range(len(chunks)))


def test_disabled_chunking_with_size_zero(monkeypatch) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "knowledge_chunk_size", 0)
    chunks = _chunk_text("a" * 1000)
    assert len(chunks) == 1


@pytest.mark.asyncio
async def test_long_doc_lands_as_multiple_chunks(
    control_plane_url,
    ch_client,
    monkeypatch,
) -> None:
    """A long content body produces multiple chunk rows in CH."""
    from loom import Agent

    from server.settings import settings

    # We can't monkeypatch settings in the server process from here. But the
    # default chunk_size=1500 in settings.py applies in the server. Build a
    # >1500-char document and verify multiple chunks.
    assert settings.knowledge_chunk_size == 1500

    ns = f"ck-{uuid.uuid4().hex[:6]}"
    src = f"doc://long-{uuid.uuid4().hex[:6]}"
    long_text = (
        "ClickHouse is a column-oriented database management system "
        "that allows generating analytical data reports in real time. "
    ) * 60  # well over 1500 chars
    assert len(long_text) > 1500

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        result = await a.knowledge.add(
            content=long_text,
            source_uri=src,
            title="long",
        )
    assert result["chunks_created"] > 1, (
        f"expected multiple chunks for a {len(long_text)}-char doc; got {result['chunks_created']}"
    )

    rows = ch_client.query(
        """
        SELECT count() FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri = {su:String} AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        parameters={"org": ORG, "ns": ns, "su": src},
    ).result_rows
    assert int(rows[0][0]) == result["chunks_created"]


@pytest.mark.asyncio
async def test_upsert_supersedes_all_old_chunks(control_plane_url, ch_client) -> None:
    """When a long doc is replaced via upsert, ALL old chunks tombstone."""
    from loom import Agent

    ns = f"ck-up-{uuid.uuid4().hex[:6]}"
    src = f"doc://re-{uuid.uuid4().hex[:6]}"
    # >1500 chars so the chunker (default size 1500) splits into >1
    long_a = "alpha content for chunking. " * 80
    long_b = "beta content for chunking. " * 80

    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        first = await a.knowledge.add(content=long_a, source_uri=src, title="A")
        result = await a.knowledge.upsert(content=long_b, source_uri=src, title="B")

    assert first["chunks_created"] > 1
    assert result["chunks_superseded"] == first["chunks_created"], (
        f"all {first['chunks_created']} old chunks should supersede; "
        f"got {result['chunks_superseded']}"
    )
    # Live chunk count should equal the new chunks_created
    live = ch_client.query(
        """
        SELECT count() FROM loom.chunks
        WHERE org = {org:String} AND namespace = {ns:String}
          AND source_uri = {su:String} AND """
        + LIVE_CHUNK_SQL
        + """
        """,
        parameters={"org": ORG, "ns": ns, "su": src},
    ).result_rows
    assert int(live[0][0]) == result["chunks_created"]


@pytest.mark.asyncio
async def test_a_section_named_only_by_its_title_is_found_by_the_lexical_leg(
    control_plane_url, ch_client
) -> None:
    """The context (title, breadcrumb) is stored on each chunk and the lexical leg
    scores it: a query naming the document's subject finds a section whose text never
    names it."""
    from loom import Agent

    ns = f"ck-ctx-{uuid.uuid4().hex[:6]}"
    doc = "## Refunds\n\nRefunds are available within 14 days of the purchase.\n"
    async with Agent(org=ORG, namespace=ns, agent="w", base_url=control_plane_url) as a:
        await a.knowledge.add(content=doc, source_uri="doc://zorbel", title="Zorbel handbook")
        await a.knowledge.add(
            content="## Refunds\n\nRefunds are never offered.\n",
            source_uri="doc://other",
            title="Other handbook",
        )
        hits = await a.knowledge.search(
            query="zorbel refunds", search_mode="lexical", namespace_scope="this", top_k=5
        )

    rows = ch_client.query(
        "SELECT context FROM loom.chunks WHERE org = {org:String} AND namespace = {ns:String}"
        " AND source_uri = 'doc://zorbel'",
        parameters={"org": ORG, "ns": ns},
    ).result_rows
    assert rows == [("Zorbel handbook\nRefunds",)]
    # document frequency counts a chunk once per term, whether the term is in its
    # context ("zorbel"), or in its context and its text ("refunds", in both chunks)
    df = dict(
        ch_client.query(
            "SELECT term, uniqExactMerge(df_state) FROM loom.chunk_term_freq"
            " WHERE org = {org:String} AND namespace = {ns:String}"
            " AND term IN ('zorbel', 'refunds') GROUP BY term",
            parameters={"org": ORG, "ns": ns},
        ).result_rows
    )
    assert df == {"zorbel": 1, "refunds": 2}
    assert hits and hits[0].source_uri == "doc://zorbel"
    assert hits[0].content == "Refunds are available within 14 days of the purchase."
