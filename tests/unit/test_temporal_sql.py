"""server/temporal_sql: one definition of both replay axes for every leg."""

from __future__ import annotations

from server.memory.domain.temporal_sql import as_of_predicate, status_predicate, tombstone_predicate


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


def test_holds_value_sql_keeps_every_status_but_the_retired_ones() -> None:
    # The first write's fence: a proposal still gets its edges, a retired row does not.
    from server.memory.domain.temporal_sql import HOLDS_VALUE_SQL

    for status in ("active", "conflicted", "duplicate", "proposed"):
        assert repr(status) in HOLDS_VALUE_SQL
    for status in ("superseded", "rejected", "tombstoned"):
        assert repr(status) not in HOLDS_VALUE_SQL
    assert "sensitivity != 'high'" in HOLDS_VALUE_SQL and "tombstoned_at IS NULL" in HOLDS_VALUE_SQL
