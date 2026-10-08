"""Unit tests for as_of replay basis (valid-time vs knowledge-time) in the
search WHERE builder."""

from __future__ import annotations

from datetime import datetime

from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery


def _q(**kw) -> SearchQuery:
    base = dict(
        org="o",
        namespace="n",
        scope="private",
        agent="a",
        embedding_model="m",
        query_vec=[0.0],
        top_k=5,
    )
    base.update(kw)
    return SearchQuery(**base)


def test_no_as_of_emits_no_time_filter():
    where, params = _build_where(_q())
    joined = " ".join(where)
    assert "as_of" not in joined and "as_of" not in params


def test_valid_time_is_default_or_form():
    where, params = _build_where(_q(as_of=datetime(2026, 9, 1)))
    joined = " ".join(where)
    assert "e.valid_at" in joined and "e.created_at" in joined  # coalesce OR-form
    assert params["as_of"] == datetime(2026, 9, 1)


def test_knowledge_time_filters_created_at_only():
    where, params = _build_where(_q(as_of=datetime(2026, 9, 1), as_of_basis="knowledge_time"))
    joined = " ".join(where)
    assert "e.created_at <= {as_of:DateTime}" in joined
    # knowledge-time ignores valid_at entirely — a backdated fact stays excluded
    assert "valid_at <=" not in joined
    # ...but keeps a row retired or deleted AFTER the anchor: loom still believed it then
    assert "(e.invalid_at IS NULL OR e.invalid_at > {as_of:DateTime})" in joined
    assert "(e.tombstoned_at IS NULL OR e.tombstoned_at > {as_of:DateTime})" in joined
    assert (
        "e.status = 'superseded' AND e.invalid_at IS NOT NULL AND e.invalid_at > {as_of:DateTime}"
        in joined
    )
    assert "e.tombstoned_at IS NULL," not in joined  # the bare live-read form is gone under replay
    assert params["as_of"] == datetime(2026, 9, 1)


def test_valid_time_prefers_event_date_over_ingest_clock():
    # valid_at wins; else the event date the fact is ABOUT; else the write time.
    where, _ = _build_where(_q(as_of=datetime(2026, 9, 1)))
    joined = " ".join(where)
    assert (
        "(e.valid_at IS NULL AND e.temporal_anchor IS NOT NULL AND e.temporal_anchor <= {as_of:DateTime})"
        in joined
    )
    assert (
        "(e.valid_at IS NULL AND e.temporal_anchor IS NULL AND e.created_at <= {as_of:DateTime})"
        in joined
    )
    # live-read shape under valid_time replay: superseded stays excluded, tombstones stay hidden
    assert "e.status IN ('active', 'conflicted')" in joined
    assert "e.tombstoned_at IS NULL" in joined and "e.tombstoned_at >" not in joined


def test_live_read_shape_unchanged_without_as_of():
    where, _ = _build_where(_q())
    joined = " ".join(where)
    assert "e.tombstoned_at IS NULL" in joined
    assert "e.status IN ('active', 'conflicted')" in joined
    assert "invalid_at" not in joined


def test_as_of_interval_is_half_open_start_inclusive_end_exclusive():
    """The boundary rule, stated once so it cannot drift leg by leg (the
    replay interval is half-open):

    a row is IN at T when its start stamp is <= T (valid_at / temporal_anchor
    / created_at, by authority) and its end stamp is > T (invalid_at /
    tombstoned_at). A row whose start equals T is visible at T; a row retired
    exactly at T is already gone at T. Every leg that builds its own fragment
    inherits this from temporal_sql; a leg that hand-rolls `<` or `>=` here
    drifts."""
    from server.memory.domain.temporal_sql import as_of_predicate, status_predicate

    vt = as_of_predicate("valid_time", alias="e")
    assert vt.count("<= {as_of:DateTime}") == 3 and "<" + " {as_of" not in vt
    assert ">= {as_of" not in vt and "> {as_of" not in vt

    kt = as_of_predicate(
        "knowledge_time", alias="e", invalid_col="invalid_at", tomb_col="tombstoned_at"
    )
    assert "e.created_at <= {as_of:DateTime}" in kt
    assert "e.invalid_at > {as_of:DateTime}" in kt and "e.invalid_at >=" not in kt
    assert "e.tombstoned_at > {as_of:DateTime}" in kt and "e.tombstoned_at >=" not in kt

    # a supersession that happened exactly at T is not believed at T
    st = status_predicate("knowledge_time", alias="e", replaying=True)
    assert "e.invalid_at > {as_of:DateTime}" in st and ">=" not in st
