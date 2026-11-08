"""`learnings.quality` scopes its rollup as every org-wide read does.

With no namespace an org admin's rollup leaves out the restricted namespaces
and the collections (end users' personal ones included) it cannot read
(`resolve_org_scope`); a named namespace is the transport gate's. CH-free.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.auth import context
from server.auth.jwt import AuthInfo
from server.memory.api.models_learnings import LearningsQualityInput
from server.operations.base import Role

_ADMIN = AuthInfo(
    subject="ops", org="acme", namespaces=("*",), roles=(Role.ORG_ADMIN,), is_service_account=False
)


class _CH:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        self.calls.append((sql, dict(params or {})))
        return []


@pytest.fixture()
def ch(monkeypatch: pytest.MonkeyPatch) -> _CH:
    import server.auth.sharing as sharing

    async def hidden(org: str) -> list[str]:
        return ["secret-ns"]

    async def no_collections(auth: AuthInfo, org: str) -> list[str]:
        return []

    fake = _CH()
    monkeypatch.setattr(context, "current_auth", lambda: _ADMIN)
    monkeypatch.setattr(context, "restricted_namespaces_hidden_from", hidden)
    monkeypatch.setattr(sharing, "readable", no_collections)
    monkeypatch.setattr("server.core.ch.get_client", lambda: fake)
    return fake


@pytest.mark.asyncio
async def test_an_org_admins_rollup_leaves_out_what_it_cannot_read(ch: _CH) -> None:
    from server.memory.app.learnings import handle_learnings_quality

    await handle_learnings_quality(LearningsQualityInput(org="acme"))
    [(sql, params)] = ch.calls
    assert "namespace NOT IN {ns_hidden:Array(String)}" in sql
    assert params["ns_hidden"] == ["secret-ns"]
    assert "startsWith(namespace, {col_prefix:String})" in sql
    assert params["col_prefix"] == "col-" and params["col_readable"] == []


@pytest.mark.asyncio
async def test_an_org_admin_whose_claim_lists_namespaces_gets_the_member_rollup(
    ch: _CH, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.memory.app.learnings import handle_learnings_quality

    listed = AuthInfo(
        subject="ops",
        org="acme",
        namespaces=("own-ns",),
        roles=(Role.ORG_ADMIN,),
        is_service_account=False,
    )

    async def mine(org: str, subject: str | None = None) -> list[str]:
        return ["own-ns"]

    monkeypatch.setattr(context, "current_auth", lambda: listed)
    monkeypatch.setattr(context, "resolve_my_namespaces", mine)
    await handle_learnings_quality(LearningsQualityInput(org="acme"))
    [(sql, params)] = ch.calls
    assert "namespace IN {ns_list:Array(String)}" in sql and params["ns_list"] == ["own-ns"]
