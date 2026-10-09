"""CHClient retries a replica-bound server error (code 10) exactly once on a
fresh connection; other database errors are not retried."""

from __future__ import annotations

from typing import Any

import pytest
from clickhouse_connect.driver.exceptions import DatabaseError

import server.core.ch as ch_mod
from server.core import ch_errors


def _client() -> Any:
    c = ch_mod.CHClient.__new__(ch_mod.CHClient)
    c._client = None
    c.reconnects = 0

    async def _reconnect(seen: int | None = None) -> None:  # Generation arg
        c.reconnects += 1

    c._reconnect = _reconnect  # type: ignore[method-assign]
    return c


async def test_code_10_is_retried_once_after_reconnect() -> None:
    c = _client()
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise DatabaseError(
                "Received ClickHouse exception, code: 10, server response: Code: 10. DB::Exception: Not found column org: in block ..."
            )
        return "ok"

    assert await c._with_retry("query_all", fn) == "ok"
    assert calls["n"] == 2 and c.reconnects == 1


async def test_code_10_on_every_connection_is_raised_after_three() -> None:
    c = _client()

    async def fn() -> str:
        raise DatabaseError("Code: 10. DB::Exception: Not found column org")

    with pytest.raises(DatabaseError):
        await c._with_retry("query_all", fn)
    assert c.reconnects == 2


async def test_other_database_errors_are_not_retried() -> None:
    c = _client()
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        raise DatabaseError("Code: 43. DB::Exception: A value of illegal type was provided")

    with pytest.raises(DatabaseError):
        await c._with_retry("query_all", fn)
    assert calls["n"] == 1 and c.reconnects == 0


def test_predicate_matches_only_listed_codes() -> None:
    assert ch_errors.is_replica_bound_error(DatabaseError("x Code: 10. y"))
    assert not ch_errors.is_replica_bound_error(DatabaseError("x Code: 100. y"))
    assert not ch_errors.is_replica_bound_error(RuntimeError("Code: 10."))


@pytest.mark.asyncio
async def test_code_10_twice_then_ok_is_served_on_the_third_connection() -> None:
    """Cloud's balancer can hand the reconnect the same replica
    tries up to three connections before giving up."""
    c = _client()
    calls = {"n": 0}

    async def fn():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise DatabaseError("Code: 10. DB::Exception: Not found column org")
        return "ok"

    assert await c._with_retry("query_all", fn) == "ok"
    assert calls["n"] == 3 and c.reconnects == 2


@pytest.mark.parametrize("code", [242, 999])
def test_read_only_and_keeper_session_errors_are_replica_bound(code: int) -> None:
    """A replica that lost its Keeper session answers 242 or
    999; the others still serve, so reconnect and retry."""

    assert ch_errors.is_replica_bound_error(
        DatabaseError(
            f"Received ClickHouse exception, code: {code}, server response: Code: {code}. x"
        )
    )
