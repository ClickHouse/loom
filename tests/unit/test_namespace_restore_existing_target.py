"""`namespace.restore` into a registered target — active or soft-deleted — applies
the same admin + restricted check as `namespace.register`, and reactivating a
soft-deleted target carries its row forward. `namespace.register` keeps the
row's `created_at` and `duplicate_threshold`."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import pytest

import server.auth.authz as authz
import server.identity.app.namespace_classification as nc
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError, ValidationError
from server.core.principals import personal_name
from server.identity.api.models import (
    NamespaceDeleteInput,
    NamespaceRegisterInput,
    NamespaceRestoreInput,
    NamespaceSnapshotOutput,
)
from server.identity.app import namespace as h
from server.identity.app import namespace_snapshot
from server.operations.base import Role

ORG, NS = "clickhouse", "ops-deleted"
BORN = datetime(2025, 1, 2, 3, 4, 5)


def _deleted_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "org": ORG,
        "namespace": NS,
        "owner_email": "owner@x",
        "description": "the original",
        "tags": ["qa"],
        "active": False,
        "kind": "memory",
        "classification": "standard",
        "visibility": "private",
        "join_policy": "closed",
        "contact_email": "owner@x",
        "password_hash": "hash",
        "duplicate_threshold": 0.93,
        "conversation_logging_enabled": False,
        "created_at": BORN,
        "updated_at": BORN,
    }
    row.update(over)
    return row


class _CH:
    def __init__(self, namespace_row: dict[str, Any] | None) -> None:
        self.namespace_row = namespace_row
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_one(self, sql: str, params: Any = None, **_: Any) -> dict[str, Any] | None:
        if "FROM loom.namespaces" in sql:
            row = self.namespace_row
            if row is None or ("active = true" in sql and not row["active"]):
                return None  # the filter ClickHouse would apply
            return dict(row)
        return None  # the restore idempotency probe

    async def query_all(self, sql: str, params: Any = None, **_: Any) -> list[dict[str, Any]]:
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], settings: Any = None) -> None:
        self.inserts.append((table, [dict(r) for r in rows]))

    def rows_for(self, table: str) -> list[dict[str, Any]]:
        return [r for t, rows in self.inserts if t == table for r in rows]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    from server.observability import audit as _audit

    grants: dict[tuple[str, str, str], Role] = {}

    async def _grant(org: str, ns: str, subject: str) -> Role | None:
        return grants.get((org, ns, subject))

    async def _no_audit(*_a: Any, **_k: Any) -> str:
        return ""

    monkeypatch.setattr(authz, "grant_role_for", _grant)
    monkeypatch.setattr(_audit, "emit", _no_audit)
    nc.reset()

    def _install(row: dict[str, Any] | None, caller: str, role: Role) -> _CH:
        ch = _CH(row)
        monkeypatch.setattr(h, "get_client", lambda: ch)
        monkeypatch.setattr(namespace_snapshot, "get_client", lambda: ch)
        monkeypatch.setattr(h, "resolve_subject", lambda *_a, **_k: caller)
        set_current_auth(
            AuthInfo(
                subject=caller,
                org=ORG,
                namespaces=("*",),
                roles=(role,),
                is_service_account=False,
            )
        )
        return ch

    yield _install, grants
    nc.reset()
    set_current_auth(None)


def _restore_input(target: str = NS) -> NamespaceRestoreInput:
    payload = NamespaceSnapshotOutput(
        snapshot_id="snap-1",
        org=ORG,
        source_namespace="elsewhere",
        created_at=datetime.now(UTC),
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
    return NamespaceRestoreInput(org=ORG, target_namespace=target, payload=payload)


@pytest.mark.asyncio
@pytest.mark.parametrize("target", [personal_name(ORG, "user:alice"), "col-team-notes"])
async def test_a_restore_into_a_collection_name_is_refused(wire, target: str) -> None:
    """A collection, an end user's personal one included, is reached only through its
    grants: a restore that found no row for the name would write a directory row, an
    admin grant and the payload's memories into it, as `namespace.register` may not."""
    install, _grants = wire
    ch = install(None, "mallory@x", Role.VIEWER)
    with pytest.raises(ValidationError):
        await namespace_snapshot.handle_namespace_restore(_restore_input(target))
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_a_viewer_cannot_restore_into_a_soft_deleted_namespace(wire) -> None:
    install, _grants = wire
    ch = install(_deleted_row(), "mallory@x", Role.VIEWER)
    with pytest.raises(AuthInsufficientError):
        await namespace_snapshot.handle_namespace_restore(_restore_input())
    assert ch.rows_for("loom.namespace_grants") == []
    assert ch.rows_for("loom.namespaces") == []


@pytest.mark.asyncio
async def test_register_refuses_the_same_viewer_the_same_way(wire) -> None:
    install, _grants = wire
    install(_deleted_row(), "mallory@x", Role.VIEWER)
    with pytest.raises(AuthInsufficientError):
        await h.handle_namespace_register(
            NamespaceRegisterInput(org=ORG, namespace=NS, owner_email="mallory@x")
        )


@pytest.mark.asyncio
async def test_a_restricted_soft_deleted_target_refuses_a_wildcard_admin(wire) -> None:
    install, _grants = wire
    nc._cache[ORG] = (1e12, frozenset({NS}))
    ch = install(_deleted_row(classification="restricted"), "ops@x", Role.ADMIN)
    with pytest.raises(AuthInsufficientError):
        await namespace_snapshot.handle_namespace_restore(_restore_input())
    assert ch.rows_for("loom.namespace_grants") == []


@pytest.mark.asyncio
async def test_an_admin_restore_reactivates_the_row_it_found(wire) -> None:
    install, grants = wire
    grants[(ORG, NS, "owner@x")] = Role.ADMIN
    old = _deleted_row(classification="restricted")
    nc._cache[ORG] = (1e12, frozenset({NS}))
    # a named claim plus a direct admin grant passes the restricted check
    ch = install(old, "owner@x", Role.ADMIN)
    set_current_auth(
        AuthInfo(
            subject="owner@x",
            org=ORG,
            namespaces=(NS,),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    out = await namespace_snapshot.handle_namespace_restore(_restore_input())
    assert out.restored is True
    (row,) = ch.rows_for("loom.namespaces")
    assert row["active"] is True and row["updated_at"] > BORN
    assert {k: v for k, v in row.items() if k not in ("active", "updated_at")} == {
        k: v for k, v in old.items() if k not in ("active", "updated_at")
    }
    assert row["classification"] == "restricted" and row["password_hash"] == "hash"


def test_register_keeps_created_at_and_duplicate_threshold() -> None:
    now = datetime(2026, 9, 28, 12, 0, 0)
    inp = NamespaceRegisterInput(org=ORG, namespace=NS, owner_email="owner@x")
    row = h.registry_row(inp, _deleted_row(active=True), now)
    assert row["created_at"] == BORN and row["updated_at"] == now
    assert row["duplicate_threshold"] == pytest.approx(0.93)
    fresh = h.registry_row(inp, None, now)
    assert fresh["created_at"] == now and fresh["duplicate_threshold"] is None


@pytest.mark.asyncio
async def test_a_non_admin_learns_nothing_from_the_idempotency_probe(wire) -> None:
    """The snapshot was already restored into the target: a caller who may not restore
    there is refused, not told `restored=False` — that answer would say the snapshot
    reached a namespace they do not administer."""
    install, _grants = wire
    ch = install(_deleted_row(active=True), "mallory@x", Role.VIEWER)
    probed: list[str] = []
    base = ch.query_one

    async def query_one(sql: str, params: Any = None, **kw: Any) -> dict[str, Any] | None:
        if "FROM loom.audit_log" in sql:
            probed.append(sql)
            return {"x": 1}
        return await base(sql, params, **kw)

    ch.query_one = query_one  # type: ignore[method-assign]
    with pytest.raises(AuthInsufficientError):
        await namespace_snapshot.handle_namespace_restore(_restore_input())
    assert probed == []


@pytest.mark.asyncio
async def test_the_auto_registered_owner_completes_a_registration_but_cannot_restore(
    wire,
) -> None:
    # A first write names its writer owner and grants nothing. Completing the
    # registration grants nothing either; a restore writes its payload's
    # grants and an admin grant for the caller, so it needs a real admin.
    from server.identity.app.namespace_autoregister import AUTO_REGISTER_TAG

    install, _grants = wire
    row = _deleted_row(active=True, owner_email="alice@x", tags=[AUTO_REGISTER_TAG])
    ch = install(row, "alice@x", Role.VIEWER)
    with pytest.raises(AuthInsufficientError):
        await namespace_snapshot.handle_namespace_restore(_restore_input())
    assert ch.rows_for("loom.namespace_grants") == []
    await h.handle_namespace_register(
        NamespaceRegisterInput(org=ORG, namespace=NS, owner_email="alice@x")
    )
    assert ch.rows_for("loom.namespace_grants") == []


def _released_on_claim(monkeypatch: pytest.MonkeyPatch, ch: _CH, *modules: Any) -> None:
    """A release that lands while the change waits for the account's claim: the
    row the change reads is the released one, never the one before it."""

    @asynccontextmanager
    async def released_meanwhile(org: str, namespace: str) -> AsyncIterator[None]:
        assert ch.namespace_row is not None
        ch.namespace_row.update(active=False, owner_email="", contact_email="", password_hash="")
        yield

    for module in modules:
        monkeypatch.setattr(module, "account_change", released_meanwhile)


@pytest.mark.asyncio
async def test_no_account_rewrite_puts_back_a_row_released_meanwhile(wire, monkeypatch) -> None:
    from server.core import ch as core_ch
    from server.identity.app import conversation_logging

    install, grants = wire
    grants[(ORG, NS, "ops@x")] = Role.ADMIN
    changes = [
        lambda: conversation_logging.set_conversation_logging(ORG, NS, True),
        lambda: namespace_snapshot.handle_namespace_restore(_restore_input()),
        lambda: h.handle_namespace_delete(NamespaceDeleteInput(org=ORG, namespace=NS)),
        lambda: h.handle_namespace_register(
            NamespaceRegisterInput(org=ORG, namespace=NS, owner_email="ops@x")
        ),
    ]
    for change in changes:
        ch = install(_deleted_row(active=True), "ops@x", Role.ADMIN)
        monkeypatch.setattr(core_ch, "get_client", lambda ch=ch: ch)
        _released_on_claim(monkeypatch, ch, h, namespace_snapshot, conversation_logging)
        await change()
        assert all(r["password_hash"] == "" for r in ch.rows_for("loom.namespaces"))
