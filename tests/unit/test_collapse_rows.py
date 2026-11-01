"""Legs that pooled one stored row twice (a set chunk's passage in one leg, the value's head
in another) leave one hit of it at assembly."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.assemble import collapse_rows


def _wire(key: str, score: float, excerpt: str, identity: Any = None) -> Any:
    hit = _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt,
        distance=1 - score,
        similarity=score,
        rank_score=score,
        namespace="n",
    )
    hit._row_identity = identity
    return hit


def test_one_row_pooled_twice_keeps_its_stronger_hit_where_the_first_sat() -> None:
    head = _wire("k", 0.5, "the value's head", ("a", "u", "s1"))
    other = _wire("j", 0.7, "another key", ("a", "u", "s1"))
    passage = _wire("k", 0.9, "the passage", ("a", "u", "s1"))
    theirs = _wire("k", 0.4, "u2's row", ("a", "u2", "s1"))
    assert collapse_rows([head, other, passage, theirs], "n") == [passage, other, theirs]


@pytest.mark.parametrize("passage_first", [True, False])
def test_the_kept_hit_has_the_tags_a_leg_read_of_its_row(passage_first: bool) -> None:
    # a hit made from a shared-scope chunk read none; the hard tag belt checks `_row_tags`
    head = _wire("k", 0.5, "the value's head", ("_shared", "-", "-"))
    head._row_tags = ("kind:lesson",)
    passage = _wire("k", 0.9, "the passage", ("_shared", "-", "-"))
    passage._row_tags = ()
    hits = [passage, head] if passage_first else [head, passage]
    (kept,) = collapse_rows(hits, "n")
    assert kept is passage and kept._row_tags == ("kind:lesson",)


def test_a_kept_hit_that_read_its_tags_keeps_them() -> None:
    stronger = _wire("k", 0.9, "the passage", ("a", "u", "s1"))
    stronger._row_tags = ("kind:lesson",)
    weaker = _wire("k", 0.5, "the value's head", ("a", "u", "s1"))
    weaker._row_tags = ()  # stamped empty, as a shared-scope chunk hit is
    (kept,) = collapse_rows([weaker, stronger], "n")
    assert kept is stronger and kept._row_tags == ("kind:lesson",)


def test_hits_that_name_no_row_are_left_alone() -> None:
    a, b = _wire("k", 0.5, "one"), _wire("k", 0.6, "two")
    assert collapse_rows([a, b], "n") == [a, b]


@pytest.mark.asyncio
async def test_the_pool_collapses_before_the_duplicates_are_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app.search import assemble

    seen: list[list[Any]] = []

    async def hide(ctx: Any) -> None:
        seen.append(list(ctx.all_hits))

    async def keep(hits: list[Any], *_: Any) -> list[Any]:
        return hits

    async def nothing(ctx: Any) -> None:
        return None

    monkeypatch.setattr(assemble, "hide_pool_duplicates", hide)
    monkeypatch.setattr(assemble, "annotate_pool_superseded", nothing)
    monkeypatch.setattr(assemble, "_enforce_hard_tag_filter", keep)
    monkeypatch.setattr(assemble, "_enforce_agent_scope", lambda hits, *_: hits)
    head = _wire("k", 0.5, "the value's head", ("a", "u", "s1"))
    passage = _wire("k", 0.9, "the passage", ("a", "u", "s1"))
    ctx = SimpleNamespace(
        input=SimpleNamespace(namespace="n", exclude_keys=None, quality_filter=False),
        all_hits=[head, passage],
        actual_work={},
        namespaces=["n"],
    )
    await assemble._drop_excluded_and_duplicates(ctx)
    assert seen == [[passage]]
