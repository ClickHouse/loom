"""Unit tests for server.memory.domain.memory_types — the shared NON_SEARCHABLE_TYPES
constant and is_searchable_type() helper.

The constant is load-bearing: it gates BOTH whether a write produces an
embedding (memory/app/handlers.py) and whether a row appears in unfiltered
search results (memory/app/retrieval/). Removing or renaming a
type here without updating both sides breaks one of them. These tests pin
the contract.
"""

from __future__ import annotations

import pytest

from server.memory.domain.memory_types import NON_SEARCHABLE_TYPES, is_searchable_type


def test_episodic_is_non_searchable() -> None:
    """episodic is session-scoped, replayed by recency — never embedded."""
    assert "episodic" in NON_SEARCHABLE_TYPES
    assert is_searchable_type("episodic") is False


def test_block_is_non_searchable() -> None:
    """block is always-loaded prompt context, fetched by exact label —
    never embedded. If this assertion fails, blocks.set will start
    writing embedding rows again and memory.search will surface
    persona text in results."""
    assert "profile" in NON_SEARCHABLE_TYPES
    assert is_searchable_type("profile") is False


def test_canonical_types_are_searchable() -> None:
    """The legacy 4-type taxonomy minus episodic is the searchable core."""
    for t in ("semantic", "procedural", "preference"):
        assert is_searchable_type(t), f"{t!r} must be searchable"


def test_custom_types_are_searchable_by_default() -> None:
    """Open-set design: custom memory_types (e.g. 'observation',
    'decision', 'lesson') participate in the searchable workflow
    without explicit opt-in. Only the deny-list excludes."""
    for t in ("observation", "decision", "lesson", "rule", "fact"):
        assert is_searchable_type(t), f"custom type {t!r} must be searchable by default"


def test_non_searchable_is_frozen() -> None:
    """The set is immutable so callers can't accidentally mutate the
    shared state (e.g. a test that did `NON_SEARCHABLE_TYPES.add(...)`
    would persist into subsequent tests)."""
    assert isinstance(NON_SEARCHABLE_TYPES, frozenset)
    with pytest.raises(AttributeError):
        NON_SEARCHABLE_TYPES.add("anything")  # type: ignore[attr-defined]


def test_is_searchable_type_handles_empty_string() -> None:
    """Edge case: an empty memory_type isn't in the deny-list, so it's
    technically "searchable" — but the schema and validators upstream
    reject empty strings. Lock the helper's behavior just so the empty-
    string case isn't a future surprise."""
    assert is_searchable_type("") is True


def test_callers_in_sync() -> None:
    """The two consumers of this module must both stay tracked. If a
    third caller is added that doesn't import from here, this test
    won't catch it — but at minimum the existing two should keep
    referencing the canonical source.

    This is a structural sanity check, not a behavior test. Refactors
    that move the import (e.g. via `from server import memory_types`
    style) need to keep the source under server.memory.domain.memory_types.
    """
    import server.memory.app.handlers as h
    import server.memory.domain.search_sql as sql

    # handler imports is_searchable_type at module level
    assert hasattr(h, "is_searchable_type")
    # the search WHERE-clause builder references NON_SEARCHABLE_TYPES so the
    # default query excludes non-searchable types.
    import inspect

    src = inspect.getsource(sql)
    assert "NON_SEARCHABLE_TYPES" in src, (
        "search_sql.py no longer references the shared constant — "
        "verify the search WHERE clause still excludes non-searchable "
        "types by default"
    )
