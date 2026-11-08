"""A code repository or tracker source registers a namespace only when it is
free: never registered and holding no data or grant someone else controls, or
soft-deleted and taken back by one of its admins. A soft-deleted namespace keeps
its data and grant rows, so anyone else, a subject whose grant there was revoked
included, would become admin over them; a name in use without a row the same."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth.authz import Role
from server.identity.app import registration
from server.identity.app.directory import IdentityDirectory


@pytest.mark.parametrize(
    ("row", "in_use", "role", "subject", "claimable"),
    [
        (None, False, None, "alice", True),
        (None, True, None, "alice", False),  # data or a grant, and no row
        (None, True, Role.WRITER, "alice", False),
        (None, True, Role.ADMIN, "alice", True),  # already acts there as admin
        # a row: the in-use read is not asked
        ({"active": True}, False, Role.ADMIN, "alice", False),
        ({"active": False}, False, Role.ADMIN, "alice", True),
        ({"active": False}, False, Role.WRITER, "alice", False),
        ({"active": False}, False, None, "alice", False),  # revoked there, or never granted
        ({"active": False}, False, Role.ADMIN, "", False),
    ],
)
def test_a_namespace_is_claimable_only_when_free_or_taken_back_by_its_admin(
    monkeypatch: pytest.MonkeyPatch,
    row: dict[str, Any] | None,
    in_use: bool,
    role: Role | None,
    subject: str,
    claimable: bool,
) -> None:
    async def _row(org: str, namespace: str, **_k: Any) -> dict[str, Any] | None:
        return row

    async def _role(org: str, namespace: str, who: str) -> Role | None:
        return role

    async def _in_use(self: Any, org: str, namespace: str, **_k: Any) -> bool:
        return in_use

    monkeypatch.setattr(registration, "namespace_row", _row)

    async def _in_scope(org: str, namespace: str, required: Role, **_k: Any) -> bool:
        return role is not None and role.value >= required.value

    monkeypatch.setattr("server.auth.authz.grant_role_for", _role)
    monkeypatch.setattr("server.auth.authz.row_reach", _in_scope)
    monkeypatch.setattr(IdentityDirectory, "in_use", _in_use)
    monkeypatch.setattr("server.auth.context.current_caller", lambda: (subject, None))
    monkeypatch.setattr("server.auth.default_namespace.auth_off_subject", lambda: "")
    assert asyncio.run(registration.namespace_claimable("acme", "code-x")) is claimable


def test_an_unverifiable_admin_check_refuses_a_name_in_use_as_the_server_s(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`row_reach` answers None when the grant could not be read: the name in use is
    refused, and the request is marked unverified."""
    marked: list[bool] = []

    async def _unverified(*_a: Any, **_k: Any) -> None:
        return None

    async def _in_use(self: Any, org: str, namespace: str, **_k: Any) -> bool:
        return True

    monkeypatch.setattr("server.auth.authz.row_reach", _unverified)
    monkeypatch.setattr(IdentityDirectory, "in_use", _in_use)
    monkeypatch.setattr(
        "server.auth.fail_closed.note_answered_unverified", lambda: marked.append(True)
    )
    assert asyncio.run(registration.refused_as_in_use("acme", "team")) is True
    assert marked == [True]
