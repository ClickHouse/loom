"""The handler-side ADMIN/WRITER/VIEWER checks of memory.cross_promote,
memory.copy_to, namespace.register (update), namespace.restore (existing
target), erasure.request (namespace scope) and procedure.fork go through the
canonical check. Two principals must be refused, with nothing written:

- a scoped token whose flat `roles` say ADMIN but whose `ns_roles` entry for
  the target says viewer: the per-namespace role is its ceiling;
- another tenant's token whose subject holds a same-named grant in this org:
  a subject string is not unique across orgs, so that grant is not theirs.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import handlers as erasure
from server.identity.api.models import (
    NamespaceRegisterInput,
    NamespaceRestoreInput,
    NamespaceSnapshotOutput,
)
from server.identity.app import namespace, namespace_snapshot
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryCopyToInput, MemoryCrossPromoteInput
from server.memory.app import copy_ops as copy_ops
from server.memory.app import procedure_evolve as procedure_evolve
from server.operations.base import Role

# A scoped token: flat ADMIN, but viewer on B by its authoritative map.
SCOPED = AuthInfo(
    subject="sa:bot",
    org="o",
    namespaces=("A", "B"),
    roles=(Role.ORG_ADMIN,),
    is_service_account=True,
    ns_roles=(("A", "admin"), ("B", "viewer")),
)
# Another tenant whose subject happens to hold an ADMIN grant in org "o".
FOREIGN = AuthInfo(
    subject="alice",
    org="other",
    namespaces=("*",),
    roles=(Role.ADMIN,),
    is_service_account=False,
)
# A flat token that names nothing in "o"; only its own grant decides.
GRANTED = AuthInfo(
    subject="alice",
    org="o",
    namespaces=("elsewhere",),
    roles=(Role.VIEWER,),
    is_service_account=False,
)
REFUSED = (SCOPED, FOREIGN)


class _FakeCH:
    """Answers the handlers' pre-check reads; any write is recorded."""

    def __init__(self, existing: dict[str, Any] | None) -> None:
        self.existing = existing
        self.inserts: list[Any] = []

    async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if "loom.audit_log" in sql:
            return None
        return self.existing

    async def insert(self, table: str, rows: list[Any], **kw: Any) -> None:
        self.inserts.append((table, rows))


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every (org, namespace, subject) grant is ADMIN, so only the token's own
    scope can refuse; nothing is restricted; writes are recorded."""
    effects: dict[str, Any] = {"writes": []}

    async def _admin_grant(org: str, ns: str, subject: str) -> Role:
        return Role.ADMIN

    async def _plain(org: str, ns: str) -> bool:
        return False

    async def _record(*a: Any, **kw: Any) -> None:
        effects["writes"].append(a)

    monkeypatch.setattr(authz, "_lookup_grant", _admin_grant)
    monkeypatch.setattr(authz, "may_be_restricted", _plain)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", _record)
    monkeypatch.setattr(memory_repo, "get_by_id", _record)
    monkeypatch.setattr(memory_repo, "get_by_key", _record)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    ch = _FakeCH({"classification": "standard", "tags": [], "owner_email": "someone"})
    effects["ch"] = ch
    monkeypatch.setattr(namespace, "get_client", lambda: ch)
    monkeypatch.setattr(namespace_snapshot, "get_client", lambda: ch)

    async def _begin(*a: Any, **kw: Any) -> Any:
        effects["writes"].append(("begin_erasure", a))
        raise AssertionError("the refused request reached the saga")

    monkeypatch.setattr(erasure, "begin_erasure", _begin)
    yield effects
    set_current_auth(None)


def _snapshot() -> NamespaceSnapshotOutput:
    return NamespaceSnapshotOutput(
        snapshot_id="99999999-9999-9999-9999-999999999999",
        org="o",
        source_namespace="A",
        created_at=datetime(2026, 9, 1, tzinfo=UTC),
        memory_count=0,
        type_count=0,
        grant_count=0,
        audit_tail_count=0,
        include_embeddings=False,
        memories=[],
        types=[],
        grants=[],
        audit_tail=[],
    )


async def _cross_promote() -> Any:
    return await copy_ops.handle_memory_cross_promote(
        MemoryCrossPromoteInput(
            from_org="o",
            from_namespace="A",
            from_memory_id="11111111-1111-1111-1111-111111111111",
            to_namespace="B",
        )
    )


async def _copy_to() -> Any:
    return await copy_ops.handle_memory_copy_to(
        MemoryCopyToInput(from_org="o", from_namespace="A", memory_key="k", to_namespace="B")
    )


async def _register() -> Any:
    return await namespace.handle_namespace_register(
        NamespaceRegisterInput(org="o", namespace="B", owner_email="x@example.com")
    )


async def _restore() -> Any:
    return await namespace_snapshot.handle_namespace_restore(
        NamespaceRestoreInput(org="o", target_namespace="B", payload=_snapshot())
    )


async def _erasure() -> Any:
    return await erasure.handle_erasure_request(
        ErasureRequestInput(org="o", scope_type="namespace", scope_value="B", requester="r")
    )


async def _fork_read() -> Any:
    return await procedure_evolve._assert_can_read_namespace("o", "C")


WRITES = [_cross_promote, _copy_to, _register, _restore, _erasure]


@pytest.mark.asyncio
@pytest.mark.parametrize("call", WRITES, ids=lambda f: f.__name__)
@pytest.mark.parametrize("auth", REFUSED, ids=["scoped-viewer", "other-tenant"])
async def test_refused_with_nothing_written(
    world: dict[str, Any], call: Any, auth: AuthInfo
) -> None:
    set_current_auth(auth)
    with pytest.raises((AuthInsufficientError, HTTPException)) as refused:
        await call()
    if isinstance(refused.value, HTTPException):
        assert refused.value.status_code == 403
    assert world["writes"] == []
    assert world["ch"].inserts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("auth", REFUSED, ids=["scoped-no-entry", "other-tenant"])
async def test_fork_source_read_refused(world: dict[str, Any], auth: AuthInfo) -> None:
    # SCOPED names no role on C; a grant for its subject must not widen it.
    set_current_auth(auth)
    with pytest.raises(AuthInsufficientError):
        await _fork_read()


@pytest.mark.asyncio
async def test_a_grant_still_admits_a_flat_token(world: dict[str, Any]) -> None:
    # A flat token still falls back to the grant.
    set_current_auth(GRANTED)
    assert await _fork_read() == "alice"
    with pytest.raises(AssertionError, match="reached the saga"):
        await _erasure()


@pytest.mark.asyncio
async def test_the_scoped_entry_admits_where_it_reaches(world: dict[str, Any]) -> None:
    # ADMIN on A by ns_roles; viewer on B whatever the flat roles say.
    set_current_auth(SCOPED)
    assert await namespace._is_admin(SCOPED, "o", "A", SCOPED.subject)
    assert not await namespace._is_admin(SCOPED, "o", "B", SCOPED.subject)
