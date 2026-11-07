"""Datetime helpers shared across the write and read paths: the clock, the
naive-UTC convention ClickHouse DateTime columns use, the epoch, and the ISO-8601
parser.

The parser's canonical behaviour:

  * accepts ``None`` (-> ``None``), a ``datetime`` (tz stripped), or a string;
  * a date-only string ("2024-03-01") becomes midnight; a full datetime with
    a "T" or " " separator and an optional trailing "Z" / +HH:MM offset is
    accepted; sub-second precision and trailing junk are tolerated;
  * the offset is DROPPED, not converted, so the result is always naive
    wall-clock time;
  * returns ``None`` for empty / non-parseable input (fail-open contract:
    callers write ``valid_at = NULL`` / treat the row as undated).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, overload

# ClickHouse DateTime is 32-bit epoch-seconds: it can only represent
# [1970-01-01, 2106-02-07]. A parsed date outside that window — a historical
# reference the extractor anchors a fact to ("founded in 1850"), a far-future
# date, or a malformed/hallucinated year — overflows the column's encoding and
# crashes the ENTIRE batch insert with a struct range error (surfaced by
# clickhouse_connect as a misleading "not Nullable" message). Out-of-range
# dates are treated as undated (None), matching the fail-open contract: the
# fact is still stored and searchable, it just carries no temporal anchor.
# (Sub-1970 anchors would need a DateTime64 column to retain; out of scope.)
_DT_MIN = datetime(1970, 1, 1)
# A parsed date keeps a margin below the column's end; `fits_datetime_column` is exact.
_DT_MAX = datetime(2106, 1, 1)
_DT_END = datetime(2106, 2, 7, 6, 28, 16)  # 2^32 seconds after the epoch


def fits_datetime_column(dt: datetime) -> bool:
    """Whether a DateTime column holds `dt` (aware or naive UTC)."""
    try:
        return _DT_MIN <= to_naive_utc(dt) < _DT_END
    except OverflowError:  # an aware time at the edge of `datetime`'s own range
        return False


def _in_dt_range(dt: datetime) -> datetime | None:
    return dt if _DT_MIN <= dt <= _DT_MAX else None


def parse_iso_dt(raw: object) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return _in_dt_range(raw.replace(tzinfo=None))
    txt = str(raw).strip()
    if not txt:
        return None
    # Try, in order: the value with Z normalised to a real offset; the value
    # truncated to seconds with a space separator promoted to "T"; the bare
    # date. fromisoformat (3.11+) already accepts a space separator, "Z" via
    # the +00:00 form, and date-only strings, so this covers SQL
    # toString(DateTime) output, extractor ISO strings, and date-only anchors.
    for candidate in (txt.replace("Z", "+00:00"), txt[:19].replace(" ", "T"), txt[:10]):
        try:
            return _in_dt_range(datetime.fromisoformat(candidate).replace(tzinfo=None))
        except ValueError:
            continue
    return None


# The tz-aware epoch: the "never" value of lease and watermark columns.
UNIX_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


# ClickHouse DateTime columns hold naive UTC; these are the clock and the
# conversions every module uses, so a test can pin time at one seam per module
# (`from server.core.datetimes import naive_utc_now as _now`, then patch `_now`).
def utc_now() -> datetime:
    """Now, timezone-aware UTC."""
    return datetime.now(UTC)


def naive_utc_now() -> datetime:
    """Now as naive UTC, the shape a ClickHouse DateTime column stores."""
    return datetime.now(UTC).replace(tzinfo=None)


def naive_utc_now_seconds() -> datetime:
    """`naive_utc_now()` truncated to whole seconds (DateTime has no sub-second part)."""
    return datetime.now(UTC).replace(tzinfo=None, microsecond=0)


@overload
def to_naive_utc(dt: datetime) -> datetime: ...
@overload
def to_naive_utc(dt: None) -> None: ...
def to_naive_utc(dt: datetime | None) -> datetime | None:
    """An aware datetime converted to naive UTC; a naive one (already UTC by
    convention) and None pass through."""
    if dt is None:
        return None
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo else dt


def to_naive_utc_second(dt: datetime) -> datetime:
    """`dt` as naive UTC to the second, as a ClickHouse DateTime holds it."""
    return to_naive_utc(dt).replace(microsecond=0)


@overload
def as_aware_utc(value: datetime) -> datetime: ...
@overload
def as_aware_utc(value: Any) -> datetime | None: ...
def as_aware_utc(value: Any) -> datetime | None:
    """A datetime read back from ClickHouse (naive UTC) as aware UTC; None for
    anything that is not a datetime."""
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=UTC)
