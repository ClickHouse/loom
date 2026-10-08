"""`grants.create` records who issued the grant from the token, the way
grants.approve records the approver: a caller cannot write another
principal's name into `granted_by`. With auth off there is no caller, so the
body's value (default "admin") stands."""

from __future__ import annotations

from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.identity.api.models_grants import GrantsCreateInput
from server.identity.app import grants as grants_mod
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo
from server.operations.base import Role


@pytest.fixture
def inserted(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []

    async def _find(*_a: Any, **_k: Any) -> None:
        return None

    async def _insert(row: dict[str, Any], **_: Any) -> None:
        rows.append(row)

    async def _no_ceiling(*_a: Any, **_k: Any) -> None:
        return None

    async def _canonical(org: str, subject: str) -> str:
        return subject

    monkeypatch.setattr(grants_repo, "find_active_grant", _find)
    monkeypatch.setattr(grants_repo, "insert_row", _insert)
    monkeypatch.setattr(grants_mod, "_enforce_code_ceiling", _no_ceiling)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _canonical)
    yield rows
    set_current_auth(None)


def _create(**over: Any) -> GrantsCreateInput:
    body: dict[str, Any] = {"org": "o", "namespace": "ns", "subject": "bob", "role": "writer"}
    body.update(over)
    return GrantsCreateInput(**body)


@pytest.mark.asyncio
async def test_the_authenticated_caller_is_the_granter(inserted: list[dict[str, Any]]) -> None:
    set_current_auth(
        AuthInfo(
            subject="carol",
            org="o",
            namespaces=("ns",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    await grants_mod.handle_grants_create(_create(granted_by="the-ceo"))
    await grants_mod.handle_grants_create(_create(subject="dan"))
    assert [r["granted_by"] for r in inserted] == ["carol", "carol"]


@pytest.mark.asyncio
async def test_auth_off_keeps_the_body_value(inserted: list[dict[str, Any]]) -> None:
    set_current_auth(None)
    await grants_mod.handle_grants_create(_create(granted_by="ops"))
    await grants_mod.handle_grants_create(_create(subject="dan"))
    assert [r["granted_by"] for r in inserted] == ["ops", "admin"]


@pytest.mark.asyncio
async def test_approve_on_a_grant_with_nothing_pending_writes_nothing(
    inserted: list[dict[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    """grants.approve decides a request. On a live grant with no pending elevation
    (the namespace's admin, say) it is a no-op: approving with `role="viewer"` would
    otherwise demote the grant past the downgrade and last-admin guards."""
    from server.identity.api.models_grants import GrantsApproveInput

    async def _active_admin(*_a: Any, **_k: Any) -> GrantState:
        return GrantState.from_row(
            {
                "role": "admin",
                "granted_by": "root",
                "status": "active",
                "pending_role": None,
                "pending_since": None,
            }
        )

    monkeypatch.setattr(grants_repo, "find_any_grant", _active_admin)
    out = await grants_mod.handle_grants_approve(
        GrantsApproveInput(
            org="o", namespace="ns", subject="alice", decision="approve", role="viewer"
        )
    )
    assert out.role is None and inserted == []
