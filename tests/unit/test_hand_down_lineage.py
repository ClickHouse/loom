"""`hand_down_lineage`: a token its former holder made whose claim cannot be
taken stops the new token's save with an error that names no token, since that
token may be another person's."""

from __future__ import annotations

import asyncio
import time
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
        self.at_once = self.most = 0

    async def query_one(self, sql: str, params: dict[str, Any]) -> dict[str, Any] | None:
        if not sql.startswith("SELECT *"):  # the former holder
            return {"sa_subject": "sa:ci-1", "created_by": "lee", "lineage": ["lee"]}
        self.at_once += 1
        self.most = max(self.most, self.at_once)
        await asyncio.sleep(0.01)  # a round trip
        self.at_once -= 1
        return {"name": params["name"], "lineage": [], "version": 1}

    async def query_all(self, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        return [{"name": f"job-{i}"} for i in range(400)]

    async def insert(self, table: str, rows: list[dict[str, Any]], **k: Any) -> None:
        self.written += [r["name"] for r in rows]


async def test_hundreds_of_children_are_handed_down_a_few_at_a_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _ManyChildren()

    @asynccontextmanager
    async def claimed(org: str, name: str) -> AsyncIterator[None]:
        yield

    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    monkeypatch.setattr(grant_claims, "service_account_change", claimed)
    started = time.monotonic()
    await service_accounts.hand_down_lineage("acme", "ci")
    assert sorted(ch.written) == sorted(f"job-{i}" for i in range(400))
    assert 1 < ch.most <= service_accounts._CLAIMS_AT_ONCE
    assert time.monotonic() - started < 400 * 0.01 / 2  # not one after another
