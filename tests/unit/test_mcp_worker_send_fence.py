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


@pytest.mark.asyncio
@pytest.mark.parametrize("revoked", [False, True])
async def test_large_live_body_fits_the_publication_budget_without_repeat_lookups(
    monkeypatch, revoked
):
    from types import SimpleNamespace

    from starlette.responses import Response

    from server.core import request_tail
    from server.knowledge.app import mcp_worker_protected as protected
    from server.knowledge.app import sources

    body = b"x" * (16 * 1024 * 1024)
    sent = 0
    elapsed_ms = 0
    locked_checks = 0
    unlocked_checks = 0
    transactions = 0
    source_locked = False

    @asynccontextmanager
    async def lease(org, namespace, *, wait_s):
        nonlocal source_locked
        assert (org, namespace) == (_SCOPE.org, _SCOPE.namespace)
        source_locked = True
        try:
            yield
        finally:
            source_locked = False

    async def check():
        nonlocal elapsed_ms, locked_checks, unlocked_checks
        # Simulate the elapsed budget without waiting for a wall clock.
        elapsed_ms += 20
        if elapsed_ms >= 2000:
            raise TimeoutError
        if source_locked:
            locked_checks += 1
        else:
            unlocked_checks += 1
        if revoked and sent:
            raise WorkerReadRefused("denied")

    async def fence(scope, deliver, deadline):
        nonlocal elapsed_ms, transactions
        assert scope == _SCOPE and source_locked
        elapsed_ms += 10
        if elapsed_ms >= 2000:
            raise TimeoutError
        transactions += 1
        await deliver()

    async def live_read(*, resolve, worker, retain, deadline):
        await check()  # The read already verifies current authority before retention.
        await retain(_SCOPE, check, deadline)

    async def app(scope, receive, send):
        await protected.protected_live_read(
            SimpleNamespace(org=_SCOPE.org),
            SimpleNamespace(
                fence_delivery=fence,
                source=None,
                connection=None,
                enrollment=None,
                worker=None,
                worker_approval=None,
            ),
            deadline=asyncio.get_running_loop().time() + 5,
        )
        await Response(body)(scope, receive, send)

    async def send(message):
        nonlocal sent
        assert source_locked
        sent += len(message.get("body", b""))

    auth = SimpleNamespace(org=_SCOPE.org)
    monkeypatch.setattr(protected, "current_auth", lambda: auth)
    monkeypatch.setattr(protected, "_require_context", lambda org, auth: None)
    monkeypatch.setattr(protected, "read_selected_live", live_read)
    monkeypatch.setattr(protected.request_work, "mark_mirrored", lambda: True)
    monkeypatch.setattr(protected.session_admission, "require", AsyncMock())
    monkeypatch.setattr(sources, "sources_lease", lease)
    response = request_tail.RequestTailMiddleware(app)({"type": "http"}, None, send)
    if revoked:
        with pytest.raises(request_tail.RequiredTailError):
            await response
        assert 0 < sent < len(body)
        assert locked_checks == transactions + 1
    else:
        await response
        assert sent == len(body)
        assert locked_checks == transactions
    assert unlocked_checks == 1
