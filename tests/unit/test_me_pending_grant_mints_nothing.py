"""A pending join request is not a grant on the /me token paths.

`namespace.join` on an approval-gated namespace writes a row with
`status='pending'` and `role` = the role asked for. The Setup and
service-account mints cap each namespace's role at the caller's grant there,
and a scoped token's `ns_roles` entry is authoritative at the gate, so reading
that row as a grant would let a requester mint an `admin` token for a namespace no
admin had let them into. The fake client below answers the way the grants
table does: a pending row matches only a statement that does not filter on
status.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.identity.app.me_callers as me

_PENDING_ADMIN = {"role": "admin", "status": "pending", "x": 1, "namespace": "gated"}


class _Grants:
    def __init__(self) -> None:
        self.sql: list[str] = []

    def _rows(self, sql: str) -> list[dict[str, Any]]:
        self.sql.append(sql)
        live_only = "status = 'active'" in sql
        return [] if live_only else [dict(_PENDING_ADMIN)]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        rows = self._rows(sql)
        return rows[0] if rows else None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return self._rows(sql)


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> _Grants:
    ch = _Grants()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def aliases(org: str, subject: str) -> list[str]:
        return [subject]

    monkeypatch.setattr(me, "subject_aliases_of", aliases)
    return ch


def test_a_pending_admin_request_is_no_role_to_mint_from(grants: _Grants) -> None:
    assert asyncio.run(me.caller_role_on("o", "gated", "mallory")) is None


def test_a_pending_admin_request_administers_nothing(grants: _Grants) -> None:
    assert asyncio.run(me.caller_admin_of("o", "gated", "mallory")) is False
