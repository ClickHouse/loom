"""`hand_down_lineage`: a token its former holder made whose claim cannot be
taken stops the new token's save with an error that names no token, since that
token may be another person's; any other failure is raised as itself; and a
former holder's many tokens are rewritten at most `_CLAIMS_AT_ONCE` at a time."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.auth import grant_claims, service_accounts
from server.core import ch as ch_mod
from server.core.errors import GrantBusyError


class _CH:
    """The name's former holder, and one token it made."""

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
        return {"sa_subject": "sa:parent-1", "created_by": "lee", "lineage": ["lee"]}

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": "kims-secret-token"}]

    async def insert(self, *a: Any, **k: Any) -> None:
        raise AssertionError("nothing is written past a claim not taken")


@pytest.mark.parametrize(
    ("raised", "answered"),
    [
        (GrantBusyError, GrantBusyError),
        (lambda m: grant_claims.NotClaimed(m, operation="grant_change"), grant_claims.NotClaimed),
    ],
)
async def test_a_childs_claim_not_taken_names_no_token(
    monkeypatch: pytest.MonkeyPatch, raised: Any, answered: type[Exception]
) -> None:
    @asynccontextmanager
    async def busy(org: str, name: str) -> AsyncIterator[None]:
        raise raised(f"another change to the token name {name!r} is running; retry shortly")
        yield

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(grant_claims, "service_account_change", busy)
    with pytest.raises(answered) as e:
        await service_accounts.hand_down_lineage("acme", "parent")
    assert "kims-secret-token" not in str(e.value)


class _ManyChildren:
    """The name's former holder and 400 tokens it made, each rewritten once."""

    def __init__(self) -> None:
        self.written: list[str] = []

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if not sql.startswith("SELECT *"):  # the former holder
            return {"sa_subject": "sa:ci-1", "created_by": "lee", "lineage": ["lee"]}
        await asyncio.sleep(0.01)  # a round trip
        return {"name": params["name"], "lineage": [], "version": 1}

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": f"job-{i}"} for i in range(400)]

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        self.written += [r["name"] for r in rows]


async def test_hundreds_of_children_are_handed_down_a_few_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ManyChildren()
    held = most = 0

    @asynccontextmanager
    async def claimed(org: str, name: str) -> AsyncIterator[None]:
        nonlocal held, most
        held += 1
        most = max(most, held)
        try:
            yield
        finally:
            held -= 1

    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(grant_claims, "service_account_change", claimed)
    await service_accounts.hand_down_lineage("acme", "ci")
    assert sorted(ch.written) == sorted(f"job-{i}" for i in range(400))
    assert 1 < most <= service_accounts._CLAIMS_AT_ONCE  # claims held at once


async def test_another_failure_is_raised_as_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Down(_CH):
        async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any]:
            if sql.startswith("SELECT *"):
                raise ConnectionError("ClickHouse is away")
            return await super().query_one(sql, params)

    @asynccontextmanager
    async def claimed(org: str, name: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(ch_mod, "get_client", lambda: _Down())
    monkeypatch.setattr(grant_claims, "service_account_change", claimed)
    with pytest.raises(ConnectionError):
        await service_accounts.hand_down_lineage("acme", "parent")
