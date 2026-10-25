"""`epoch_ms` binds a fixed time into a `DateTime64(3)` as whole milliseconds
since the epoch — exact integer arithmetic, aware or naive (UTC) input — so no
writer spells it as `timestamp() * 1000`, the shape the version-unit gate
(`test_rmt_version_unit.py`) refuses because a ReplacingMergeTree version is
nanoseconds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from server.core.datetimes import epoch_ms


def test_whole_milliseconds_since_the_epoch() -> None:
    assert epoch_ms(datetime(1970, 1, 1, tzinfo=UTC)) == 0
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 1, 500_000, tzinfo=UTC)) == 1_500
    # sub-millisecond digits are dropped, never rounded up into the next ms
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 0, 999, tzinfo=UTC)) == 0
    assert epoch_ms(datetime(1970, 1, 1, 0, 0, 0, 1_000, tzinfo=UTC)) == 1


def test_naive_is_utc_and_an_offset_is_honoured() -> None:
    at = datetime(2026, 10, 25, 12, 0, 0, 250_000)
    expect = int((at.replace(tzinfo=UTC) - datetime(1970, 1, 1, tzinfo=UTC)).total_seconds()) * 1000
    assert epoch_ms(at) == expect + 250
    assert epoch_ms(at.replace(tzinfo=UTC)) == epoch_ms(at)
    plus_two = at.replace(tzinfo=timezone(timedelta(hours=2)))
    assert epoch_ms(plus_two) == epoch_ms(at) - 2 * 3_600_000


def test_matches_the_float_formula_where_it_is_exact() -> None:
    at = datetime(2026, 10, 25, 11, 30, 15, 123_000, tzinfo=UTC)
    assert epoch_ms(at) == int(at.timestamp() * 1000)
