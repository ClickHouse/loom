"""A set chunk whose row was not pooled knows only its writer; once a later leg pools that
writer's row of the key, the chunk folds onto it at assembly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.assemble import fold_writer_hits
from server.memory.domain.search_types import (
    SearchHit,
    hit_row_key,
    names_row,
    writers_covered,
)


def _wire(key: str, score: float, excerpt: str, *, version: int = 1, **row: Any) -> Any:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=1 - score,
        similarity=score,
        rank_score=score,
        namespace="n",
        version=version,
    )
    hit._row_identity = row.get("identity")
    hit._row_writer = row.get("writer")
    return hit


def test_a_chunk_folds_onto_its_writers_newest_row_where_it_sat() -> None:
    chunk = _wire("k", 0.9, "the passage", writer=("a", "u"))
    other = _wire("j", 0.7, "another key", identity=("a", "u", "s1"))
    older = _wire("k", 0.6, "the s1 value", version=1, identity=("a", "u", "s1"))
    newest = _wire("k", 0.5, "the s2 value", version=2, identity=("a", "u", "s2"))
    theirs = _wire("k", 0.4, "u2's row", identity=("a", "u2", "s1"))
    out = fold_writer_hits([chunk, other, older, newest, theirs], "n")
    # the chunks hold the writer's newest value: only that row takes the passage
    assert out == [newest, other, older, theirs]
    assert (newest.rank_score, newest.content_excerpt) == (0.9, "the passage")
    assert newest.distance == pytest.approx(0.1) and newest.similarity == 0.9
    assert older.content_excerpt == "the s1 value"


def test_a_row_that_outscores_its_chunk_keeps_its_text_and_place() -> None:
    row = _wire("k", 0.8, "the value's head", identity=("a", "u", "s1"))
    chunk = _wire("k", 0.3, "the passage", writer=("a", "u"))
    assert fold_writer_hits([row, chunk], "n") == [row]
    assert (row.rank_score, row.content_excerpt) == (0.8, "the value's head")
    assert row.score_components == {"folded_chunks": 1.0}


def test_a_chunk_whose_row_is_not_pooled_stays() -> None:
    chunk = _wire("k", 0.9, "the passage", writer=("a", "u"))
    theirs = _wire("k", 0.4, "u2's row", identity=("a", "u2", "s1"))
    assert fold_writer_hits([chunk, theirs], "n") == [chunk, theirs]


def test_two_chunks_of_one_writer_and_key_from_separate_legs_are_one_hit() -> None:
    # each supplemental leg folds its own chunk; with no row pooled, the first stands for both
    first = _wire("k", 0.6, "one passage", writer=("a", "u"))
    second = _wire("k", 0.8, "another passage", writer=("a", "u"))
    theirs = _wire("k", 0.4, "u2's chunk", writer=("a", "u2"))
    assert fold_writer_hits([first, theirs, second], "n") == [first, theirs]
    assert (first.rank_score, first.content_excerpt) == (0.8, "another passage")


@pytest.mark.asyncio
async def test_the_chunk_folds_before_the_duplicates_are_read(monkeypatch) -> None:
    from server.memory.app.search import assemble

    seen: list[list[Any]] = []

    async def hide(ctx: Any) -> None:
        seen.append(list(ctx.all_hits))

    async def keep(hits: list[Any], *_: Any) -> list[Any]:
        return hits

    async def keep_pool(ctx: Any) -> None:
        return None

    monkeypatch.setattr(assemble, "hide_pool_duplicates", hide)
    monkeypatch.setattr(assemble, "annotate_pool_superseded", keep_pool)
    monkeypatch.setattr(assemble, "_enforce_hard_tag_filter", keep)
    monkeypatch.setattr(assemble, "_enforce_agent_scope", lambda hits, *_: hits)
    row = _wire("k", 0.5, "the value's head", identity=("a", "u", "s1"))
    chunk = _wire("k", 0.9, "the passage", writer=("a", "u"))
    ctx = SimpleNamespace(
        input=SimpleNamespace(namespace="n", exclude_keys=None, quality_filter=False),
        all_hits=[chunk, row],
        actual_work={},
        namespaces=["n"],
    )
    await assemble._drop_excluded_and_duplicates(ctx)
    assert seen == [[row]]


def test_a_writer_only_hit_stands_for_its_writers_row_and_keeps_its_own_row_key() -> None:
    def hit(identity: Any = None, writer: Any = None) -> SearchHit:
        return SearchHit(
            memory_key="k",
            memory_type="semantic",
            content_excerpt="k",
            distance=0.5,
            identity=identity,
            writer=writer,
        )

    chunk = hit(writer=("a", "u"))
    row = hit(identity=("a", "u", ""))  # a row stored with an empty session names its row
    assert (names_row(chunk), names_row(row)) == (False, True)
    assert writers_covered([chunk, row], "n") == {("n", "a", "u", "k")}
    assert hit_row_key(chunk, "n") == ("n", "a", "u", "", "k")
