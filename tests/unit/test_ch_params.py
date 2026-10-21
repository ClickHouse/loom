"""A list parameter is cut to what one ClickHouse form field carries: every chunk,
rendered as the driver sends it, fits the bound, and the chunks keep every value
in order."""

from __future__ import annotations

import uuid

from clickhouse_connect.driver.binding import format_bind_value

from server.core.ch_params import FIELD_BYTES, chunks, fitting


def _cell_key(i: int) -> tuple[str, ...]:
    """A stub key the size a cell's are: uuid-shaped org, namespace and user."""
    return (
        str(uuid.UUID(int=7)),
        f"cell-{uuid.UUID(int=i)}-{uuid.UUID(int=i + 1)}",
        f"agent-{uuid.UUID(int=i + 2)}",
        str(uuid.UUID(int=i + 3)),
        str(uuid.UUID(int=i + 4)),
        "private",
        "text-embedding-3-small",
        f"user_preference_{i:06d}_" + "x" * 24,
    )


def test_cell_shaped_keys_are_cut_into_parameters_that_fit() -> None:
    keys = [_cell_key(i) for i in range(1600)]
    assert len(format_bind_value(keys)) > 2 * 128 * 1024  # whole, far over the server's field
    parts = list(chunks(keys))
    assert [k for part in parts for k in part] == keys  # every key, in order
    assert len(parts) > 1
    assert all(len(format_bind_value(part)) <= FIELD_BYTES for part in parts)


def test_fitting_takes_the_leading_values_one_parameter_carries() -> None:
    keys = [_cell_key(i) for i in range(1600)]
    head = fitting(keys)
    assert head == keys[: len(head)] and 0 < len(head) < len(keys)
    assert len(format_bind_value(head)) <= FIELD_BYTES
    assert fitting(keys[:3]) == keys[:3]
    assert fitting([]) == []


def test_a_value_longer_than_the_bound_goes_alone() -> None:
    big, small = "x" * (FIELD_BYTES + 10), "y"
    assert list(chunks([small, big, small])) == [[small], [big], [small]]
