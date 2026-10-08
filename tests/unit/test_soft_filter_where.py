"""Unit: the hard-filter vs soft-boost switch in _build_where.

When *_soft is set, the tag / category predicate must be DROPPED from the
WHERE clause (it becomes an additive rank boost in vector_search instead).
When unset, the hard filter is preserved. Deterministic — no CH round-trip.
"""

from __future__ import annotations

from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery


def _q(**kw) -> SearchQuery:
    base = dict(
        org="o",
        namespace="ns",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.0],
        top_k=5,
    )
    base.update(kw)
    return SearchQuery(**base)


def _has(where: list[str], needle: str) -> bool:
    return any(needle in clause for clause in where)


def test_tags_hard_filter_by_default() -> None:
    where, params = _build_where(_q(tags=["x"]))
    assert _has(where, "hasAny(e.tags"), where
    assert params.get("tags") == ["x"]


def test_tags_soft_drops_hard_filter() -> None:
    where, params = _build_where(_q(tags=["x"], tags_soft=True))
    assert not _has(where, "hasAny(e.tags"), where
    assert "tags" not in params  # boost param is bound in vector_search, not here


def test_category_hard_filter_by_default() -> None:
    where, params = _build_where(_q(category="c"))
    assert _has(where, "memory_categories"), where
    assert params.get("category") == "c"


def test_category_soft_drops_hard_filter() -> None:
    where, params = _build_where(_q(category="c", category_soft=True))
    assert not _has(where, "memory_categories"), where
    assert "category" not in params
