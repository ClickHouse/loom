"""CHClient answers ClickHouse running short of the server's or the user's memory (code
241 with "(total)" or "(for user)") as a 503 OVERLOADED the caller retries, not a 500;
a statement's own memory cap stays its own error."""

from __future__ import annotations

from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

import server.core.ch as ch_mod
from server.core.ch_errors import is_passing_state
from server.core.errors import OverloadedError

# captured from ClickHouse 26.10 with max_server_memory_usage at 4 GiB
_TOTAL = (
    "Received ClickHouse exception, code: 241, server response: Code: 241. "
    "DB::Exception: (total) memory limit exceeded: would use 4.01 GiB (attempt to allocate "
    "chunk of 2.00 MiB), current RSS: 4.01 GiB, maximum: 4.00 GiB. (MEMORY_LIMIT_EXCEEDED)"
)
_USER = "Code: 241. DB::Exception: Memory limit (for user) exceeded: would use 12 GiB"
_QUERY = "Code: 241. DB::Exception: Memory limit (for query) exceeded: would use 2 GiB"


def _client() -> Any:
    c = ch_mod.CHClient.__new__(ch_mod.CHClient)
    c._client = None

    async def _reconnect(seen: int | None = None) -> None:
        raise AssertionError("a memory limit is not a connection problem")

    c._reconnect = _reconnect  # type: ignore[method-assign]
    return c


def _raising(message: str):
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise DatabaseError(message)

    return fn, calls


@pytest.mark.parametrize("message", [_TOTAL, _USER])
@pytest.mark.parametrize("op", ["query_all", "insert"])
async def test_the_servers_or_users_memory_running_short_is_a_503(message: str, op: str) -> None:
    fn, calls = _raising(message)
    with pytest.raises(OverloadedError) as caught:
        await _client()._with_retry(op, fn, cls="write" if op == "insert" else "read")
    assert calls["n"] == 1  # answered at once, not retried here
    assert caught.value.status_code == 503 and caught.value.extra.get("retry_after_s") == 5
    # the answer carries no ClickHouse text, and a rows' writer still reads it as passing
    assert "DB::Exception" not in str(caught.value)
    assert isinstance(caught.value.__cause__, DatabaseError)
    assert is_passing_state(caught.value)


async def test_a_statements_own_memory_cap_stays_its_error() -> None:
    fn, _ = _raising(_QUERY)
    with pytest.raises(DatabaseError):
        await _client()._with_retry("query_all", fn)
