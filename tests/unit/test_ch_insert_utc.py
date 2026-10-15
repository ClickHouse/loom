"""An insert hands the driver UTC instants: a naive datetime is UTC by Loom's
convention, and the driver would read it in the host's time zone."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from server.core.ch import _insert_data

NAIVE = datetime(2026, 1, 2, 3, 4, 5)


def test_a_naive_datetime_is_marked_utc() -> None:
    [[value]] = _insert_data([{"t": NAIVE}], ["t"], ["DateTime64(3)"])
    assert value == NAIVE.replace(tzinfo=UTC) and value.tzinfo is UTC


def test_an_aware_datetime_keeps_its_instant() -> None:
    kolkata = NAIVE.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    [[value]] = _insert_data([{"t": kolkata}], ["t"], ["DateTime"])
    assert value is kolkata


def test_an_array_of_datetimes_is_marked_too() -> None:
    hours = [NAIVE, NAIVE + timedelta(hours=1)]
    [[value]] = _insert_data([{"hours": hours}], ["hours"], ["Array(DateTime)"])
    assert [v.tzinfo for v in value] == [UTC, UTC]


def test_an_array_of_nullable_datetimes_is_marked_past_a_null() -> None:
    [[value]] = _insert_data([{"t": [None, NAIVE]}], ["t"], ["Array(Nullable(DateTime))"])
    assert value[0] is None and value[1].tzinfo is UTC


def test_other_columns_are_not_walked() -> None:
    vector, today = [0.5, 0.25], date(2026, 1, 2)
    types = ["Array(BFloat16)", "Date", "String", "Array(Float32)"]
    [row] = _insert_data(
        [{"v": vector, "d": today, "s": "x", "e": []}], ["v", "d", "s", "e"], types
    )
    assert row[0] is vector and row[1] is today and row[2:] == ["x", []]


def test_unknown_types_check_every_cell() -> None:
    vector = [0.5, 0.25]
    [row] = _insert_data([{"v": vector, "t": NAIVE, "h": [None, NAIVE]}], ["v", "t", "h"], None)
    assert row[0] is vector and row[1].tzinfo is UTC and row[2][1].tzinfo is UTC


def test_datetimes_inside_tuples_and_maps_are_marked_too() -> None:
    types = ["Array(Tuple(DateTime, UInt32))", "Map(String, DateTime)"]
    [[pairs, by_name]] = _insert_data([{"p": [(NAIVE, 7)], "m": {"k": NAIVE}}], ["p", "m"], types)
    assert pairs == [(NAIVE.replace(tzinfo=UTC), 7)] and pairs[0][0].tzinfo is UTC
    assert by_name["k"].tzinfo is UTC
