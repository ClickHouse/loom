"""memory.cross_promote reads its source by an org-wide id, but the caller's
ADMIN is checked on `from_namespace` only. A source row that lives in any other
namespace must read as not found — never copied into the target, and answered
the same way as an id that does not exist."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.errors import NotFoundError
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryCrossPromoteInput
from server.memory.app import copy_ops as copy_ops
from server.memory.app import ops as ops
from server.memory.store import MemoryRow
from server.operations.base import Role

_MID = "11111111-1111-1111-1111-111111111111"


def _row(namespace: str) -> MemoryRow:
    return MemoryRow(
        memory_id=_MID,
        org="o",
        namespace=namespace,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="the secret lives in C",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        expires_at=None,
    )


@pytest.fixture
def wired(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    inserted: list[Any] = []

    async def _insert(rows: list[Any], **kw: Any) -> None:
        inserted.extend(rows)

    async def _noop(**kw: Any) -> None:
        return None

    monkeypatch.setattr(memory_repo, "insert_memory_versions", _insert)
    monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _noop)
    monkeypatch.setattr("server.observability.audit.emit", _noop)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())

    async def _plain(org: str, namespace: str) -> bool:
        return False

    monkeypatch.setattr("server.auth.authz.may_be_restricted", _plain)
    set_current_auth(
        AuthInfo(
            subject="alice",
            org="o",
            namespaces=("A", "B"),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    yield inserted
    set_current_auth(None)


def _source(monkeypatch: pytest.MonkeyPatch, row: MemoryRow) -> None:
    async def _by_id(
        memory_id: str, *, org: str, namespace: str | None = None, **kw: Any
    ) -> MemoryRow | None:
        found = memory_id == row.memory_id and org == row.org
        return row if found and namespace in (None, row.namespace) else None

    monkeypatch.setattr(memory_repo, "get_by_id", _by_id)


def _promote(**over: Any) -> MemoryCrossPromoteInput:
    body = {"from_org": "o", "from_namespace": "A", "from_memory_id": _MID, "to_namespace": "B"}
    body.update(over)
    return MemoryCrossPromoteInput(**body)


@pytest.mark.asyncio
async def test_source_in_an_unauthorized_namespace_reads_as_not_found(
    monkeypatch: pytest.MonkeyPatch, wired: list[Any]
) -> None:
    _source(monkeypatch, _row("C"))
    with pytest.raises(NotFoundError) as foreign:
        await copy_ops.handle_memory_cross_promote(_promote())
    assert wired == []
    with pytest.raises(NotFoundError) as missing:
        await copy_ops.handle_memory_cross_promote(
            _promote(from_memory_id="22222222-2222-2222-2222-222222222222")
        )
    # Same answer shape either way: the refusal does not confirm the id exists.
    assert "C" not in str(foreign.value)
    assert str(foreign.value).replace(_MID, "<id>") == str(missing.value).replace(
        "22222222-2222-2222-2222-222222222222", "<id>"
    )


@pytest.mark.asyncio
async def test_source_in_the_authorized_namespace_is_copied(
    monkeypatch: pytest.MonkeyPatch, wired: list[Any]
) -> None:
    _source(monkeypatch, _row("A"))
    out = await copy_ops.handle_memory_cross_promote(_promote())
    assert out.promoted and out.target_namespace == "B"
    assert len(wired) == 1 and wired[0]["namespace"] == "B"
    assert wired[0]["cross_promoted_from_namespace"] == "A"


def _as_rival_org_admin() -> None:
    """An org admin of another org, with the wildcard namespace claim."""
    set_current_auth(
        AuthInfo(
            subject="rival-admin",
            org="rival",
            namespaces=("*",),
            roles=(Role.ORG_ADMIN,),
            is_service_account=False,
        )
    )


@pytest.mark.asyncio
async def test_another_orgs_admin_cannot_cross_promote(
    monkeypatch: pytest.MonkeyPatch, wired: list[Any]
) -> None:
    """The transport admits the call (the input names `from_org`, not `org`),
    so the handler's ADMIN check is the org boundary."""
    from server.core.errors import AuthInsufficientError

    _source(monkeypatch, _row("A"))
    _as_rival_org_admin()
    with pytest.raises(AuthInsufficientError):
        await copy_ops.handle_memory_cross_promote(_promote())
    assert wired == []


@pytest.mark.asyncio
async def test_another_orgs_admin_cannot_copy_into_or_out_of_an_org(
    monkeypatch: pytest.MonkeyPatch, wired: list[Any]
) -> None:
    from server.core.errors import AuthInsufficientError
    from server.memory.api.models_lifecycle import MemoryCopyToInput

    looked_up: list[Any] = []

    async def _by_key(identity: Any, key: str, **kw: Any) -> MemoryRow | None:
        looked_up.append(key)
        return _row("A")

    monkeypatch.setattr(memory_repo, "get_by_key", _by_key)
    _as_rival_org_admin()
    body = {"from_org": "o", "from_namespace": "A", "memory_key": "k", "to_namespace": "B"}
    with pytest.raises(AuthInsufficientError):
        await copy_ops.handle_memory_copy_to(MemoryCopyToInput(**body))
    assert wired == [] and looked_up == []
