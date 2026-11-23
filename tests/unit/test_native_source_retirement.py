"""Legacy connection metadata can be fenced and erased without native execution."""

from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.knowledge.app import connected_accounts as accounts
from server.knowledge.domain.connected_accounts import ConnectedAccount
from server.operations.base import Role


@pytest.fixture
def no_native_execution(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        httpx.AsyncClient, "send", AsyncMock(side_effect=AssertionError("provider HTTP forbidden"))
    )
    monkeypatch.setattr(
        accounts, "unseal", Mock(side_effect=AssertionError("credential decryption forbidden"))
    )


@pytest.mark.asyncio
async def test_personal_owner_is_canonical_and_checks_every_token_cutoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    auth = AuthInfo(
        subject="verified-alias",
        org="org-one",
        namespaces=("selected",),
        roles=(Role.WRITER,),
        is_service_account=False,
        session_id="session-one",
        token_id="token-one",
        issued_at=200,
        parent_iats=(100,),
        parents=("parent-one",),
    )
    canonical = AsyncMock(return_value="canonical-owner")
    standing = AsyncMock()
    monkeypatch.setattr(accounts.aliases, "canonical_subject", canonical)
    monkeypatch.setattr(accounts, "_standing", standing)
    assert await accounts.own_subject(auth) == ("org-one", "canonical-owner")
    canonical.assert_awaited_once_with("org-one", "verified-alias")
    standing.assert_awaited_once_with(
        "org-one",
        "canonical-owner",
        100,
        ("verified-alias", "token-one", "session-one", "parent-one"),
    )
    for changed in ({"is_service_account": True}, {"reads_as_creator": True}, {"org": "*"}):
        with pytest.raises(AuthInsufficientError):
            await accounts.own_subject(replace(auth, **changed))
    assert canonical.await_count == standing.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["identity", "inactive", "scim", "cutoff"])
async def test_owner_standing_refuses_offboarding_without_native_execution(
    monkeypatch: pytest.MonkeyPatch, no_native_execution: None, failure: str
) -> None:
    identity = SimpleNamespace(subject="owner-one", emails={"owner@example.test"})
    monkeypatch.setattr(type(accounts.settings), "auth_actually_required", lambda self: True)
    monkeypatch.setattr(
        accounts,
        "resolve_reader",
        AsyncMock(return_value=None if failure == "identity" else identity),
    )
    monkeypatch.setattr(
        accounts.aliases, "verified_spellings", AsyncMock(return_value=["owner-one", "old-alias"])
    )
    monkeypatch.setattr(
        accounts.store, "account_active", AsyncMock(return_value=failure != "inactive")
    )
    monkeypatch.setattr(accounts.scim, "refuses_subject", AsyncMock(return_value=failure == "scim"))
    monkeypatch.setattr(
        accounts.revocation,
        "cutoffs",
        AsyncMock(return_value={"old-alias": None} if failure == "cutoff" else {}),
    )
    with pytest.raises(accounts.OwnerUnavailable):
        await accounts._standing("org-one", "owner-one", 200, ("session-one",))
    if failure == "cutoff":
        accounts.revocation.cutoffs.assert_awaited_once_with(
            ["owner-one", "old-alias", "session-one"]
        )


@pytest.mark.asyncio
async def test_owner_standing_refuses_unconfigured_auth_before_directory_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity = AsyncMock()
    monkeypatch.setattr(type(accounts.settings), "auth_actually_required", lambda self: False)
    monkeypatch.setattr(accounts, "resolve_reader", identity)
    with pytest.raises(accounts.ValidationError, match="the connected account is unavailable"):
        await accounts._standing("org-one", "owner-one", 200)
    identity.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["gdrive", "notion", "linear", "mcp:" + "a" * 32, "retired-kind"])
async def test_local_erasure_uses_exact_stored_key_without_opening_credentials(
    monkeypatch: pytest.MonkeyPatch, no_native_execution: None, kind: str
) -> None:
    from server.erasure.app import connected_accounts as erasure
    from server.knowledge.app import sources

    account = ConnectedAccount(
        "org-one",
        "canonical-owner",
        kind,
        "b" * 32 + "/0" + "c" * 31,
        disconnected_at=datetime.now(UTC),
    )
    held: list[tuple[str, str]] = []

    @asynccontextmanager
    async def lease(org: str, namespace: str):
        held.append((org, namespace))
        try:
            yield
        finally:
            held.pop()

    async def erase_source(*key: str) -> None:
        assert held == [("org-one", "selected")]
        assert key == ("org-one", "selected", "canonical-owner", kind)

    namespaces = AsyncMock(return_value=["selected"])
    erase = AsyncMock()
    source_census, account_census = AsyncMock(), AsyncMock()
    monkeypatch.setattr(accounts, "get_client", object)
    monkeypatch.setattr(accounts.claims, "get", AsyncMock(return_value=account.connection_id))
    monkeypatch.setattr(accounts.source_store, "connected_owner_namespaces", namespaces)
    monkeypatch.setattr(accounts.source_store, "erase_connected_owner", erase_source)
    monkeypatch.setattr(accounts.store, "erase", erase)
    monkeypatch.setattr(sources, "sources_lease", lease)
    monkeypatch.setattr(erasure, "require_sources_erased", source_census)
    monkeypatch.setattr(erasure, "require_erased", account_census)
    await accounts._erase_locked(account, keep_marker=False)
    namespaces.assert_awaited_once_with("org-one", "canonical-owner", kind)
    erase.assert_awaited_once_with("org-one", ["canonical-owner"], kind=kind)
    source_census.assert_awaited_once_with("org-one", "selected", "canonical-owner", kind)
    account_census.assert_awaited_once_with(
        "org-one", "canonical-owner", kind, keep_connection=None
    )
    assert not held


@pytest.mark.asyncio
async def test_erasure_refuses_unknown_fence_before_removing_any_raw_version(
    monkeypatch: pytest.MonkeyPatch, no_native_execution: None
) -> None:
    account = ConnectedAccount("org-one", "owner-one", "retired-kind", "b" * 32 + "/0" + "c" * 31)
    erase = AsyncMock()
    monkeypatch.setattr(accounts, "get_client", object)
    monkeypatch.setattr(accounts.claims, "get", AsyncMock(return_value=None))
    monkeypatch.setattr(accounts.store, "erase", erase)
    with pytest.raises(accounts.ValidationError, match="the connected account is unavailable"):
        await accounts._erase_locked(account, keep_marker=False)
    erase.assert_not_awaited()
