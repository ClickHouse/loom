"""admin.users is an ORG_ADMIN op; the deployment's platform-admin list (operators of every
org) is answered only to a platform admin."""

from __future__ import annotations

from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.operations.base import Role


class _CH:
    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[Any]:
        return []


async def _run(monkeypatch: pytest.MonkeyPatch, role: Role) -> Any:
    from server.identity.api.models_admin_users import AdminUsersInput
    from server.identity.app import admin_users as h
    from server.settings import settings

    monkeypatch.setattr(h, "get_client", lambda: _CH())
    monkeypatch.setattr(type(settings), "platform_admins_set", property(lambda _s: {"root@op"}))
    set_current_auth(
        AuthInfo(subject="a", org="o", namespaces=("*",), roles=(role,), is_service_account=False)
    )
    try:
        return await h.handle_admin_users(AdminUsersInput(org="o"))
    finally:
        set_current_auth(None)


async def test_an_org_admin_does_not_get_the_platform_admin_list(monkeypatch) -> None:
    out = await _run(monkeypatch, Role.ORG_ADMIN)
    assert out.platform_admins_configured == []


async def test_a_platform_admin_does(monkeypatch) -> None:
    out = await _run(monkeypatch, Role.PLATFORM_ADMIN)
    assert out.platform_admins_configured == ["root@op"]
