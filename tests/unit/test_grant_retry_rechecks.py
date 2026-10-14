"""A retried grant op that finds its change already written still runs the
grant log check: the first attempt's row may have landed with its check failing
after it."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import TooManySpellingsError
from server.identity.api.models import NamespaceCancelRequestInput, NamespaceLeaveInput
from server.identity.api.models_grants import GrantsApproveInput, GrantsRevokeInput
from server.identity.app import claim_release as cr
from server.identity.app import grant_writes
from server.identity.app import grants as grants_app
from server.identity.app import namespace_membership as membership
from server.identity.store import grants as grants_repo

ORG = "acme"


@pytest.fixture
def rechecked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[tuple[str, str]]]]:
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    async def _recheck(org: str, pairs: list[tuple[str, str]]) -> None:
        calls.append((org, pairs))

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _canonical(org: str, subject: str, **_k: Any) -> str:
        return "alice" if subject == "alice@x.io" else subject

    monkeypatch.setattr(grant_writes, "recheck", _recheck)
    monkeypatch.setattr(grants_repo, "find_any_grant", _nothing)
    monkeypatch.setattr(grants_repo, "find_active_grant", _nothing)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _canonical)
    return calls


def test_a_retried_revoke_rechecks_the_canonical_subject_on_every_branch(
    rechecked: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "server.code.app.index.maintainers.branch_namespaces",
        lambda org, ns: ["code-x", "code-x-26-5"],
    )
    revoke = GrantsRevokeInput(org=ORG, namespace="code-x", subject="alice@x.io")
    assert not asyncio.run(grants_app.handle_grants_revoke(revoke)).revoked
    assert rechecked == [(ORG, [("code-x", "alice"), ("code-x-26-5", "alice")])]


def test_a_retried_approve_rechecks(rechecked: list[Any]) -> None:
    approve = GrantsApproveInput(org=ORG, namespace="team", subject="bob")
    asyncio.run(grants_app.handle_grants_approve(approve))
    assert rechecked == [(ORG, [("team", "bob")])]


def test_a_retried_leave_rechecks(rechecked: list[Any]) -> None:
    leave = NamespaceLeaveInput(org=ORG, namespace="team", subject="bob")
    assert not asyncio.run(membership.handle_namespace_leave(leave)).left
    assert rechecked == [(ORG, [("team", "bob")])]


def test_a_retried_cancel_request_rechecks(rechecked: list[Any]) -> None:
    cancel = NamespaceCancelRequestInput(org=ORG, namespace="team", subject="bob")
    assert not asyncio.run(membership.handle_namespace_cancel_request(cancel)).cancelled
    assert rechecked == [(ORG, [("team", "bob")])]


class _Registry:
    """A namespace row, and the live grants `grants` lists."""

    def __init__(self, grants: list[dict[str, Any]]) -> None:
        self.grants = grants

    async def query_one(self, sql: str, params: Any = None) -> dict[str, Any]:
        return {"org": ORG, "namespace": "vault", "owner_email": "alice", "active": True}

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        return self.grants

    async def insert(self, table: str, rows: list[dict[str, Any]], **_k: Any) -> None:
        return None


def _release(ch: _Registry) -> Any:
    return asyncio.run(
        cr.release_claim(org=ORG, namespace="vault", reason="", actor_subject="op", ch=ch)  # type: ignore[arg-type]
    )


def test_a_retried_release_rechecks_every_subject_the_log_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    rechecked: list[tuple[str, str]] = []

    async def _recheck_namespace(org: str, namespace: str) -> None:
        rechecked.append((org, namespace))

    monkeypatch.setattr(grant_writes, "recheck_namespace", _recheck_namespace)
    assert _release(_Registry(grants=[])).released
    assert rechecked == [(ORG, "vault")]


def test_a_release_refused_after_its_tombstones_says_what_to_release_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _too_many(*_a: Any, **_k: Any) -> None:
        raise TooManySpellingsError("too many accounts", subject="bob", limit=1000)

    monkeypatch.setattr(cr, "write_grant_cutoffs", _nothing)
    monkeypatch.setattr(grant_writes, "insert_rows", _too_many)
    live = [{"subject": "bob", "role": "writer", "granted_by": "alice", "status": "active"}]
    with pytest.raises(TooManySpellingsError, match="cannot release acme/vault"):
        _release(_Registry(grants=live))
