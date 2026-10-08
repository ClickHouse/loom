"""One org-level call answers "what can I approve"."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

import server.identity.app.grants as grants_mod
from server.auth.jwt import AuthInfo
from server.identity.domain.grants import PendingGrant
from server.identity.store import grants as grants_repo
from server.operations.base import Role

ORG = "clickhouse"


@pytest.mark.asyncio
async def test_pending_for_me_reads_one_query_scoped_by_the_callers_aliases(monkeypatch) -> None:
    import server.auth.aliases as aliases
    import server.auth.context as ctx
    from server.identity.api.models_grants import GrantsPendingForMeInput

    calls: list[tuple[str, list[str], int]] = []

    async def _pending(org: str, subjects: list[str], *, limit: int = 500, ch: Any = None):
        calls.append((org, subjects, limit))
        rows = [
            {
                "namespace": "code-private",
                "kind": "code",
                "subject": "bob",
                "role": "viewer",
                "status": "pending",
                "pending_role": None,
                "pending_since": datetime(2026, 9, 18, 12),
                "granted_at": datetime(2026, 9, 18, 12),
            },
            {
                "namespace": "team",
                "kind": "memory",
                "subject": "carol",
                "role": "viewer",
                "status": "active",
                "pending_role": "writer",
                "pending_since": None,
                "granted_at": datetime(2026, 9, 18, 11),
            },
        ]
        return [PendingGrant.from_row(r) for r in rows]

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject, subject + "@x"]

    monkeypatch.setattr(grants_repo, "pending_for_admins", _pending)
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(
        ctx,
        "current_auth",
        lambda: AuthInfo(
            subject="alice",
            org=ORG,
            namespaces=("*",),
            roles=(Role.ADMIN,),
            is_service_account=False,
        ),
    )
    out = await grants_mod.handle_grants_pending_for_me(GrantsPendingForMeInput(org=ORG))
    assert calls == [(ORG, ["alice", "alice@x"], 500)]
    rows = {r.namespace: r for r in out.rows}
    # a join request: role IS the request
    assert (
        rows["code-private"].requested_role == "viewer"
        and rows["code-private"].current_role == "viewer"
    )
    assert rows["code-private"].kind == "code"
    # an elevation request on an existing grant
    assert rows["team"].requested_role == "writer" and rows["team"].current_role == "viewer"
    # no pending_since → falls back to granted_at so the bell can sort
    assert rows["team"].pending_since == datetime(2026, 9, 18, 11)


@pytest.mark.asyncio
async def test_pending_for_me_without_a_principal_is_empty(monkeypatch) -> None:
    import server.auth.context as ctx
    from server.identity.api.models_grants import GrantsPendingForMeInput

    monkeypatch.setattr(ctx, "current_auth", lambda: None)
    out = await grants_mod.handle_grants_pending_for_me(GrantsPendingForMeInput(org=ORG))
    assert out.rows == []


def test_the_op_is_registered_org_level_and_not_listed_on_the_curated_toolsets() -> None:
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.operations.toolsets import op_toolsets

    op = OPERATIONS_BY_NAME["grants.pending_for_me"]
    assert op.required_role == Role.VIEWER and op.idempotent
    # by-name only: the 14,000-token listing ceiling is untouched
    assert not ({"core", "coding", "atomic"} & set(op_toolsets("grants.pending_for_me")))
