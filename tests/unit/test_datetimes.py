"""Unit tests for server.core.datetimes: the shared ISO-8601 parser (parse_iso_dt)
and epoch_ms.

It is the one parser on the write + read paths, so its
behaviour is load-bearing: entity-edge/triple validity intervals, the
bulk-set temporal anchor, and the memory.search chronological re-sort all
depend on it. Pure function — no env / CH / API key needed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone

from server.core.datetimes import epoch_ms, parse_iso_dt


def test_date_only_becomes_midnight() -> None:
    assert parse_iso_dt("2024-03-01") == datetime(2024, 3, 1, 0, 0, 0)


def test_full_datetime_t_separator() -> None:
    assert parse_iso_dt("2024-03-01T12:30:45") == datetime(2024, 3, 1, 12, 30, 45)


def test_space_separator_sql_form() -> None:
    # ClickHouse toString(DateTime) emits a space separator, not "T".
    assert parse_iso_dt("2024-03-01 12:30:45") == datetime(2024, 3, 1, 12, 30, 45)


def test_trailing_z_and_offset_are_dropped_not_converted() -> None:
    # The contract is naive wall-clock time: the offset is dropped, NOT
    # UTC-converted.
    assert parse_iso_dt("2024-03-01T12:30:45Z") == datetime(2024, 3, 1, 12, 30, 45)
    assert parse_iso_dt("2024-03-01T12:30:45+05:00") == datetime(2024, 3, 1, 12, 30, 45)


def test_result_is_always_naive() -> None:
    assert parse_iso_dt("2024-03-01T12:30:45Z").tzinfo is None


def test_datetime_passthrough_strips_tzinfo() -> None:
    aware = datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
    out = parse_iso_dt(aware)
    assert out == datetime(2024, 3, 1, 12, 0)
    assert out.tzinfo is None


def test_none_empty_and_garbage_return_none() -> None:
    assert parse_iso_dt(None) is None
    assert parse_iso_dt("") is None
    assert parse_iso_dt("   ") is None
    assert parse_iso_dt("not-a-date") is None


def test_out_of_datetime_range_returns_none() -> None:
    # ClickHouse DateTime is 32-bit [1970, 2106]: a date outside that window
    # would overflow the column's epoch-seconds encoding and crash the whole
    # batch insert. parse_iso_dt clamps such dates to None (undated), matching
    # the fail-open contract, for extractor output anchored to historical,
    # far-future or malformed years.
    assert parse_iso_dt("1850-01-01") is None  # pre-epoch historical reference
    assert parse_iso_dt("1969-12-31") is None  # just before the lower bound
    assert parse_iso_dt("2200-01-01") is None  # beyond the 2106 upper bound
    assert parse_iso_dt("0901-01-01") is None  # malformed / hallucinated year
    assert parse_iso_dt(datetime(1850, 6, 1)) is None  # datetime passthrough too
    # In-range boundaries still parse.
    assert parse_iso_dt("1970-01-01") == datetime(1970, 1, 1)
    assert parse_iso_dt("2105-12-31") == datetime(2105, 12, 31)


def test_chronological_ordering_is_not_lexical() -> None:
    # memory.search's chronological re-sort: a lexical string sort would
    # misorder mixed-precision values. As datetimes the
    # comparative order is correct.
    a = parse_iso_dt("2024-03-01")  # date-only
    b = parse_iso_dt("2024-03-01 09:00:00")  # same day, with time
    c = parse_iso_dt("2024-12-31T23:59:59Z")  # later
    assert a is not None and b is not None and c is not None
    assert a < b < c


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
