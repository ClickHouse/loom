"""A list parameter cut to what one ClickHouse form field carries.

A bound parameter travels as one HTTP form field (clickhouse-connect moves any longer
than 4 KiB into the request body), and ClickHouse refuses a field longer than its
`http_max_field_value_size`, 128 KiB by default: "Field value too long". A statement
that binds a list whole (keys to read, groups to order last) takes it in chunks that
each fit, measured as the driver renders them.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from clickhouse_connect.driver.binding import format_bind_value

# Half the server's default field limit: room for the driver's quoting and for a
# server configured lower.
FIELD_BYTES = 64 * 1024  # not a setting: follows ClickHouse's default field limit


def chunks[T](values: Sequence[T], *, limit: int = FIELD_BYTES) -> Iterator[list[T]]:
    """`values` in order, in lists whose rendered parameter (`[v1, v2, ...]`) is at
    most `limit` bytes; a value longer than `limit` on its own is a chunk alone."""
    chunk: list[T] = []
    size = 2  # the brackets
    for value in values:
        width = len(format_bind_value(value, top_level=False)) + 2  # and its ", "
        if chunk and size + width > limit:
            yield chunk
            chunk, size = [], 2
        chunk.append(value)
        size += width
    if chunk:
        yield chunk


def fitting[T](values: Sequence[T], *, limit: int = FIELD_BYTES) -> list[T]:
    """The leading values of `values` that one parameter of at most `limit` bytes
    carries (all of them when they fit)."""
    return next(chunks(values, limit=limit), [])
