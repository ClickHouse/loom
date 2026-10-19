"""`hand_down_lineage`: a token its former holder made whose claim cannot be
taken stops the new token's save with an error that names no token, since that
token may be another person's."""

from __future__ import annotations

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
