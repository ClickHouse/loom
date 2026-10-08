"""Unit tests for the pure memory.record.query SQL builder.

Covers the additive `predicates` surface (numeric/bool/string comparisons +
value_state / negative queries) and the legacy `filters` map, asserting that
every caller value is parameterized (never interpolated) and field names are
validated. Predicates collapse to a single LEFT JOIN + GROUP BY + one
`sumIf(...) > 0` HAVING term each (not N IN-subqueries), and every table
reference uses the CH-valid `AS <alias> FINAL` ordering.
"""

from __future__ import annotations

import pytest
from fastapi import HTTPException

from server.memory.api.models_records import MemoryRecordQueryInput, RecordPredicate
from server.memory.store.record_query import _build_record_query


def _inp(**kw) -> MemoryRecordQueryInput:
    base = {"org": "o", "namespace": "n", "schema_name": "s"}
    base.update(kw)
    return MemoryRecordQueryInput(**base)


def test_legacy_filters_are_parameterized() -> None:
    sql, params = _build_record_query(_inp(filters={"city": "NYC"}))
    assert "f.value_string = {v0:String}" in sql
    assert "sumIf(1, f.field_name = {fn0:String}" in sql
    assert params["fn0"] == "city"
    assert params["v0"] == "NYC"
    # the value must NOT be inlined into the SQL text
    assert "'NYC'" not in sql


def test_numeric_predicate_gt() -> None:
    sql, params = _build_record_query(
        _inp(predicates=[RecordPredicate(field="age", op="gt", number=30)])
    )
    assert "f.value_number > {v0:Float64}" in sql
    assert "f.value_state = 'value'" in sql
    assert params["v0"] == 30.0


def test_bool_predicate_eq() -> None:
    sql, params = _build_record_query(
        _inp(predicates=[RecordPredicate(field="active", boolean=True)])
    )
    assert "f.value_bool = {v0:Bool}" in sql
    assert params["v0"] is True


def test_string_ne_predicate() -> None:
    # `ne` is a presence-AND-differs test: the field must exist with a concrete
    # value (value_state='value') whose value != the target. NOT an equality flip.
    sql, params = _build_record_query(
        _inp(predicates=[RecordPredicate(field="status", op="ne", string="closed")])
    )
    assert "f.value_string != {v0:String}" in sql
    assert "f.value_state = 'value'" in sql
    assert ") > 0" in sql
    assert params["v0"] == "closed"


def test_value_state_negative_query() -> None:
    sql, _ = _build_record_query(
        _inp(predicates=[RecordPredicate(field="email", value_state="unknown")])
    )
    assert "f.value_state = 'unknown'" in sql
    # a state-only predicate compares no value column
    assert "value_number" not in sql
    assert "f.value_string" not in sql


def test_filters_and_predicates_get_distinct_params() -> None:
    sql, params = _build_record_query(
        _inp(
            filters={"city": "NYC"},
            predicates=[RecordPredicate(field="age", op="gte", number=18)],
        )
    )
    assert params["fn0"] == "city" and params["fn1"] == "age"
    assert "{v0:String}" in sql and "{v1:Float64}" in sql


def test_predicate_query_is_single_join_not_n_subqueries() -> None:
    sql, _ = _build_record_query(
        _inp(
            predicates=[
                RecordPredicate(field="age", op="gte", number=18),
                RecordPredicate(field="city", string="NYC"),
            ]
        )
    )
    # one JOIN over the field table, not one IN-subquery per predicate
    assert sql.count("loom.memory_record_fields") == 1
    assert "LEFT JOIN loom.memory_record_fields AS f FINAL" in sql
    assert "FROM loom.memory_records AS r FINAL" in sql
    assert "GROUP BY r.record_id" in sql
    assert sql.count("sumIf(1, f.field_name") == 2  # one HAVING term per predicate
    assert "IN (SELECT" not in sql


def test_zero_predicate_query_has_no_join_or_group_by() -> None:
    sql, _ = _build_record_query(_inp())
    assert "FROM loom.memory_records AS r FINAL" in sql
    assert "JOIN" not in sql
    assert "GROUP BY" not in sql
    assert "HAVING" not in sql


def test_alias_precedes_final_for_ch_validity() -> None:
    # `FROM t FINAL AS r` is a SYNTAX_ERROR on ClickHouse 26.4; alias must precede FINAL.
    for sql in (
        _build_record_query(_inp())[0],
        _build_record_query(_inp(filters={"city": "NYC"}))[0],
    ):
        assert "FINAL AS" not in sql
        assert "AS r FINAL" in sql


def test_empty_predicate_rejected() -> None:
    with pytest.raises(HTTPException):
        _build_record_query(_inp(predicates=[RecordPredicate(field="x")]))


def test_bad_field_name_rejected() -> None:
    with pytest.raises(HTTPException):
        _build_record_query(_inp(filters={"bad-name!": "x"}))


def test_bad_predicate_field_name_rejected() -> None:
    with pytest.raises(HTTPException):
        _build_record_query(_inp(predicates=[RecordPredicate(field="1bad", number=1)]))


def test_as_of_adds_validity_window() -> None:
    from datetime import UTC, datetime

    sql, params = _build_record_query(_inp(as_of=datetime(2026, 1, 1, tzinfo=UTC)))
    assert "r.valid_at <= {as_of:DateTime}" in sql
    assert "as_of" in params
