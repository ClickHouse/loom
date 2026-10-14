"""An insert hands the driver UTC instants: a naive datetime is UTC by Loom's
convention, and the driver would read it in the host's time zone."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta, timezone

from server.core.ch import _insert_data

NAIVE = datetime(2026, 1, 2, 3, 4, 5)


def test_a_naive_datetime_is_marked_utc() -> None:
    [[value]] = _insert_data([{"t": NAIVE}], ["t"])
    assert value == NAIVE.replace(tzinfo=UTC) and value.tzinfo is UTC


def test_an_aware_datetime_keeps_its_instant() -> None:
    kolkata = NAIVE.replace(tzinfo=timezone(timedelta(hours=5, minutes=30)))
    [[value]] = _insert_data([{"t": kolkata}], ["t"])
    assert value is kolkata


def test_an_array_of_datetimes_is_marked_too() -> None:
    [[value]] = _insert_data([{"hours": [NAIVE, NAIVE + timedelta(hours=1)]}], ["hours"])
    assert [v.tzinfo for v in value] == [UTC, UTC]


def test_other_values_pass_through() -> None:
    vector, today = [0.5, 0.25], date(2026, 1, 2)
    [row] = _insert_data([{"v": vector, "d": today, "s": "x", "e": []}], ["v", "d", "s", "e"])
    assert row[0] is vector and row[1] is today and row[2:] == ["x", []]
