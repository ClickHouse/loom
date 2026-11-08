"""The epoch sentinels encode as the epoch on any host clock: the driver reads a
naive datetime as host-local time, which east of UTC is before 1970 and does
not fit an unsigned DateTime."""

from __future__ import annotations

import os
import struct
import time
from collections.abc import Iterator
from datetime import datetime

import pytest
from clickhouse_connect.datatypes.registry import get_from_name
from clickhouse_connect.driver.insert import InsertContext

from server.identity.app.namespace_autoregister import AUTO_ROW_VERSION, OWNERLESS_ROW_VERSION
from server.memory.store.conflict_resolver import JUDGED_MARK, OWED_MARK


@pytest.fixture
def east_of_utc() -> Iterator[None]:
    saved = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Kolkata"
    time.tzset()
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = saved
        time.tzset()


def _encoded(type_name: str, value: datetime) -> int:
    ch_type = get_from_name(type_name)
    out = bytearray()
    ch_type.write_column([value], out, InsertContext("t", ["c"], [ch_type], [[value]]))
    return int(struct.unpack("<I", bytes(out[-4:]))[0])


@pytest.mark.usefixtures("east_of_utc")
@pytest.mark.parametrize(
    ("type_name", "value", "seconds"),
    [
        ("Nullable(DateTime)", OWED_MARK, 1),  # memory_embeddings.conflict_checked_at
        ("Nullable(DateTime)", JUDGED_MARK, 2),
        ("DateTime", AUTO_ROW_VERSION, 1),  # namespaces.updated_at
        ("DateTime", OWNERLESS_ROW_VERSION, 0),
    ],
)
def test_a_sentinel_is_the_epoch_east_of_utc(type_name: str, value: datetime, seconds: int) -> None:
    assert _encoded(type_name, value) == seconds
