"""A needle read gets the sentence window, not the section."""

from __future__ import annotations

import pytest

from server.memory.app import spans


def test_sentences_split_conservatively():
    text = "Dr. Smith arrived at 3.5 p.m. He paid $4.20 for tea. Then he left!"
    got = [text[s:e] for s, e in spans.sentences(text)]
    assert got == ["Dr. Smith arrived at 3.5 p.m.", "He paid $4.20 for tea.", "Then he left!"]


def test_windows_whole_text_when_short():
    assert spans.windows("One sentence only.", size=2) == [(0, 18)]
    assert spans.windows("   ", size=2) == []


def test_windows_slide_by_one_sentence():
    text = "A is one. B is two. C is three. D is four."
    ws = [text[s:e] for s, e in spans.windows(text, size=2)]
    assert ws == ["A is one. B is two.", "B is two. C is three.", "C is three. D is four."]


@pytest.mark.asyncio
async def test_best_spans_picks_the_window_nearest_the_query_in_one_batch():
    text = (
        "The meeting covered the roadmap. Alice moved to Paris in 2019. "
        "Bob presented the budget. The team discussed hiring."
    )
    batches: list[list[str]] = []

    async def embed_many(texts):
        batches.append(texts)
        # Toy embedding: [mentions Paris, mentions budget].
        return [[1.0 if "Paris" in t else 0.0, 1.0 if "budget" in t else 0.0] for t in texts]

    got = await spans.best_spans([1.0, 0.0], [text, "Just one line."], embed_many)
    assert len(batches) == 1  # every window of every chunk in ONE embed call
    assert got[1] is None  # a single-window chunk is already the span
    sp = got[0]
    assert sp is not None
    assert "Alice moved to Paris in 2019." in sp.text
    assert text[sp.start : sp.end].strip() == sp.text
    assert sp.similarity > 0.5


@pytest.mark.asyncio
async def test_best_spans_no_windows_no_embed_call():
    called = False

    async def embed_many(texts):
        nonlocal called
        called = True
        return []

    assert await spans.best_spans([1.0], ["Short.", ""], embed_many) == [None, None]
    assert not called


def test_search_hit_carries_span_fields_default_empty():
    from server.knowledge.api.models_knowledge import KnowledgeSearchInput, _SearchHit

    h = _SearchHit(
        source_uri="u",
        chunk_id="c",
        chunk_idx=0,
        content="x",
        tags=[],
        similarity=0.5,
        distance=0.5,
        score=0.5,
    )
    assert (h.span, h.span_start, h.span_end, h.span_similarity) == ("", 0, 0, 0.0)
    assert KnowledgeSearchInput(org="o", namespace="n", query="q").narrow_spans is False
