"""The `as_of` replay axes every leg shares with the journal off, and the validity
fragments the graph and records planes share with it on."""

from __future__ import annotations

from server.memory.domain.temporal_sql import (
    as_of_predicate,
    graph_validity_predicate,
    record_validity_predicate,
    status_predicate,
    tombstone_predicate,
)


def test_a_current_graph_read_keeps_open_and_not_yet_closed_edges() -> None:
    assert graph_validity_predicate(valid=False, known=False) == (
        "(valid_to IS NULL OR valid_to >= now())"
    )


def test_a_graph_read_at_v_is_interval_containment() -> None:
    p = graph_validity_predicate(valid=True, known=False, alias="t")
    assert "t.valid_from <= {valid_as_of:DateTime}" in p
    assert "t.valid_to >= {valid_as_of:DateTime}" in p
    assert "recorded_at" not in p


def test_a_graph_read_known_at_k_reopens_closes_recorded_later() -> None:
    p = graph_validity_predicate(valid=False, known=True)
    # With no V the world is read at K.
    assert "valid_from <= {known_as_of:DateTime}" in p
    assert "recorded_at <= {known_as_of:DateTime}" in p
    assert "closed_at > {known_as_of:DateTime}" in p


def test_a_record_holds_over_a_half_open_interval() -> None:
    p = record_validity_predicate("r")
    assert "r.valid_at <= {valid_as_of:DateTime}" in p
    # A record superseded at T and its successor never both hold at T.
    assert "r.valid_to > {valid_as_of:DateTime}" in p and ">=" not in p


def test_a_graph_knowledge_time_is_bound_in_utc_whatever_its_offset() -> None:
    from datetime import UTC, datetime, timedelta, timezone
    from types import SimpleNamespace

    from server.memory.app.facts import _graph_validity

    utc = datetime(2026, 1, 1, 6, 0, tzinfo=UTC)
    ist = utc.astimezone(timezone(timedelta(hours=5, minutes=30)))
    bound = []
    for k in (utc, ist):
        params: dict = {}
        _graph_validity(SimpleNamespace(known_as_of=k, valid_as_of=k), params)
        bound.append(params)
    assert (
        bound[0]
        == bound[1]
        == {
            "known_as_of": datetime(2026, 1, 1, 6, 0),
            "valid_as_of": utc,
        }
    )


def test_valid_time_authority_order_valid_then_anchor_then_created():
    p = as_of_predicate("valid_time", alias="e")
    assert p == (
        "((e.valid_at IS NOT NULL AND e.valid_at <= {as_of:DateTime}) "
        "OR (e.valid_at IS NULL AND e.temporal_anchor IS NOT NULL AND e.temporal_anchor <= {as_of:DateTime}) "
        "OR (e.valid_at IS NULL AND e.temporal_anchor IS NULL AND e.created_at <= {as_of:DateTime}))"
    )


def test_valid_time_without_anchor_column_is_two_branch():
    p = as_of_predicate("valid_time", valid_col="valid_from", anchor_col=None)
    assert (
        p
        == "((valid_from IS NOT NULL AND valid_from <= {as_of:DateTime}) OR (valid_from IS NULL AND created_at <= {as_of:DateTime}))"
    )


def test_knowledge_time_is_written_by_t_and_not_retired_or_deleted_by_t():
    p = as_of_predicate(
        "knowledge_time", alias="e", invalid_col="invalid_at", tomb_col="tombstoned_at"
    )
    assert p == (
        "(e.created_at <= {as_of:DateTime} AND (e.invalid_at IS NULL OR e.invalid_at > {as_of:DateTime}) "
        "AND (e.tombstoned_at IS NULL OR e.tombstoned_at > {as_of:DateTime}))"
    )
    assert as_of_predicate("knowledge_time") == "(created_at <= {as_of:DateTime})"


def test_param_name_is_bindable():
    assert "{cutoff:DateTime}" in as_of_predicate("knowledge_time", param="cutoff")


def test_status_predicate_readmits_rows_superseded_after_the_anchor_only_under_knowledge_time():
    # A read serves active + conflicted; `duplicate` joins only on request.
    served = "e.status IN ('active', 'conflicted')"
    live = "e.status IN ('active', 'duplicate', 'conflicted')"
    assert status_predicate("valid_time", alias="e", replaying=True) == served
    assert status_predicate("knowledge_time", alias="e", replaying=False) == served
    assert (
        status_predicate("valid_time", alias="e", replaying=True, include_duplicates=True) == live
    )
    assert status_predicate("knowledge_time", alias="e", replaying=True) == (
        f"({served} OR (e.status = 'superseded' AND e.invalid_at IS NOT NULL AND e.invalid_at > {{as_of:DateTime}}))"
    )
    assert status_predicate(
        "knowledge_time", alias="e", replaying=True, include_duplicates=True
    ) == (
        f"({live} OR (e.status = 'superseded' AND e.invalid_at IS NOT NULL AND e.invalid_at > {{as_of:DateTime}}))"
    )


def test_tombstone_predicate_keeps_rows_deleted_after_the_anchor_only_under_knowledge_time():
    assert tombstone_predicate("valid_time", alias="e", replaying=True) == "e.tombstoned_at IS NULL"
    assert tombstone_predicate("knowledge_time", alias="e", replaying=True) == (
        "(e.tombstoned_at IS NULL OR e.tombstoned_at > {as_of:DateTime})"
    )
