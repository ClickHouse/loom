"""`collection.create` refuses a name that holds data with no directory row: the new
collection would open that data to whoever the creator grants it to."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

import pytest

from server.auth import context
from server.auth.jwt import AuthInfo
from server.core.errors import CollectionNameTakenError
from server.identity.api.models_collections import CollectionCreateInput
from server.identity.app import collections
from server.identity.app.directory import IdentityDirectory
from server.operations.base import Role


@pytest.mark.parametrize("admin", [False, True])
def test_a_collection_name_holding_data_is_taken(
    monkeypatch: pytest.MonkeyPatch, admin: bool
) -> None:
    """Refused unless the gate's ADMIN check passes on the name (a platform admin, for a
    name outside the tree), which then creates it."""

    @asynccontextmanager
    async def _held(*_a: Any) -> Any:
        yield

    async def _none(*_a: Any, **_k: Any) -> None:
        return None

    async def _no_grants(*_a: Any, **_k: Any) -> list[dict[str, Any]]:
        return []

    async def _in_use(self: Any, org: str, namespace: str, **_k: Any) -> bool:
        return (org, namespace) == ("acme", "col-x")

    async def _admin(*_a: Any, **_k: Any) -> bool:
        return admin

    writes: list[Any] = []

    async def _write(*a: Any, **_k: Any) -> None:
        writes.append(a)

    async def _zero(*_a: Any, **_k: Any) -> int:
        return 0

    class _CH:
        async def insert(self, *a: Any, **_k: Any) -> None:
            writes.append(a)

    monkeypatch.setattr(collections, "account_change", _held)
    monkeypatch.setattr(collections, "collections_change", _held)
    monkeypatch.setattr(collections.namespaces_repo, "fetch_row", _none)
    monkeypatch.setattr(collections.collections_repo, "fetch", _none)
    monkeypatch.setattr(collections.collections_repo, "write", _write)
    monkeypatch.setattr(collections.collections_repo, "stored_version", _zero)
    monkeypatch.setattr(collections.grant_cache, "changed", _none)
    monkeypatch.setattr(collections, "insert_admin_grant", _write)
    monkeypatch.setattr(collections, "get_client", _CH)
    monkeypatch.setattr(collections.grants_repo, "list_active", _no_grants)
    monkeypatch.setattr(collections, "resolve_subject", lambda *_a, **_k: "user:mallory")
    monkeypatch.setattr("server.auth.authz.row_reach", _admin)
    monkeypatch.setattr(IdentityDirectory, "in_use", _in_use)
    context.set_current_auth(
        AuthInfo(
            subject="user:mallory",
            org="acme",
            namespaces=("*",),
            roles=(Role.WRITER,),
            is_service_account=False,
        )
    )
    create = collections.handle_collection_create(
        CollectionCreateInput(org="acme", namespace="col-x")
    )
    try:
        if admin:
            assert asyncio.run(create).created
        else:
            with pytest.raises(CollectionNameTakenError, match="already used"):
                asyncio.run(create)
    finally:
        context.set_current_auth(None)
    assert bool(writes) is admin
