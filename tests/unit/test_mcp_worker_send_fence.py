import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from server.knowledge.app import mcp_worker_pg_directory as directory
from server.knowledge.app.mcp_worker_read import WorkerReadRefused, WorkerReadScope
from server.knowledge.domain.mcp_worker_permissions import ReaderConnection

_WORKER, _SOURCE, _ALICE, _BOB = uuid4(), uuid4(), uuid4(), uuid4()
_PROFILE = "a" * 64
_SCOPE = WorkerReadScope(
    _WORKER,
    "org",
    "team",
    _SOURCE,
    9,
    "bob",
    _BOB,
    4,
    _PROFILE,
    2,
    "urn:resource:selected",
    3,
    ReaderConnection("alice", _WORKER, _ALICE, 5, _PROFILE),
)


@pytest.fixture
def authority(monkeypatch):
    conn = object()
    open_transaction = False

    @asynccontextmanager
    async def transaction(key):
        nonlocal open_transaction
        assert key == directory.approvals.ApprovalKey("org", "team", _WORKER, _PROFILE)
        open_transaction = True
        try:
            yield conn
        finally:
            open_transaction = False

    async def deliver():
        assert open_transaction

    approve = AsyncMock(return_value=directory.approvals.WorkerApproval(2, 1, True, True))
    link = AsyncMock(return_value=directory.readers.Link(9, _BOB, 4, 3, True))
    account = AsyncMock(
        side_effect=[
            (_PROFILE, directory.accounts.AccountState(5, True, "realm")),
            (_PROFILE, directory.accounts.AccountState(4, True, "realm")),
        ]
    )
    monkeypatch.setattr(directory.approvals, "scope_transaction", transaction)
    monkeypatch.setattr(directory.approvals, "current_on", approve)
    monkeypatch.setattr(directory.readers, "current_on", link)
    monkeypatch.setattr(directory.accounts, "reader_account_on", account)
    return conn, approve, link, account, AsyncMock(side_effect=deliver)


@pytest.mark.asyncio
async def test_protected_send_uses_current_locked_approval_link_and_exact_accounts(authority):
    conn, approve, link, account, deliver = authority
    await directory.fence_live_delivery(_SCOPE, deliver, asyncio.get_running_loop().time() + 2)
    approve.assert_awaited_once_with(
        conn, directory.approvals.ApprovalKey("org", "team", _WORKER, _PROFILE), lock=True
    )
    link.assert_awaited_once_with(
        conn, directory.readers.LinkKey("org", "team", _SOURCE, "bob"), lock=True
    )
    assert [c.args for c in account.await_args_list] == [
        (conn, "org", "alice", _WORKER, _ALICE),
        (conn, "org", "bob", _WORKER, _BOB),
    ]
    assert all(c.kwargs == {"lock": True} for c in account.await_args_list)
    deliver.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", ["approval", "link", "account", "realm"])
async def test_final_send_is_refused_after_prior_authority_changes(authority, revoked):
    _, approve, link, account, deliver = authority
    if revoked == "approval":
        approve.return_value = replace(approve.return_value, generation=3)
    elif revoked == "link":
        link.return_value = replace(link.return_value, active=False)
    else:
        account.side_effect = [
            (_PROFILE, directory.accounts.AccountState(5, revoked != "account", "realm")),
            (_PROFILE, directory.accounts.AccountState(4, True, "different realm")),
        ]
    with pytest.raises(WorkerReadRefused, match="^denied$"):
        await directory.fence_live_delivery(_SCOPE, deliver, asyncio.get_running_loop().time() + 2)
    deliver.assert_not_awaited()
