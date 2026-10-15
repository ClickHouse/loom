"""A code repository or tracker source registers a namespace only when it is
free: never registered, or soft-deleted and taken back by one of its admins. A
soft-deleted namespace keeps its data and grant rows, so anyone else, a subject
whose grant there was revoked included, would become admin over them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth.authz import Role
from server.identity.app import registration


@pytest.mark.parametrize(
    ("row", "role", "subject", "claimable"),
    [
        (None, None, "alice", True),
        ({"active": True}, Role.ADMIN, "alice", False),
        ({"active": False}, Role.ADMIN, "alice", True),
        ({"active": False}, Role.WRITER, "alice", False),
        ({"active": False}, None, "alice", False),  # revoked there, or never granted
        ({"active": False}, Role.ADMIN, "", False),
    ],
)
def test_a_namespace_is_claimable_only_when_free_or_taken_back_by_its_admin(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any] | None,
    role: Role | None,
    subject: str,
    claimable: bool,
) -> None:
    async def _row(org: str, namespace: str, **_k: Any) -> dict[str, Any] | None:
        return row

    async def _role(org: str, namespace: str, who: str) -> Role | None:
        return role

    monkeypatch.setattr(registration, "namespace_row", _row)
    monkeypatch.setattr("server.auth.authz.grant_role_for", _role)
    assert asyncio.run(registration.namespace_claimable("acme", "code-x", subject)) is claimable
