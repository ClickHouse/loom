"""A retried grant op that finds its change already written still completes a
lowering whose second cutoff never landed: the first attempt's rows may have
landed with that cutoff failing after them."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.core.errors import TooManySpellingsError
from server.identity.api.models import NamespaceCancelRequestInput, NamespaceLeaveInput
from server.identity.api.models_grants import (
    GrantsApproveInput,
    GrantsCreateInput,
    GrantsRevokeInput,
)
from server.identity.app import claim_release as cr
from server.identity.app import grant_writes
from server.identity.app import grants as grants_app
from server.identity.app import namespace_membership as membership
from server.identity.domain.grants import GrantState
from server.identity.store import grants as grants_repo

ORG = "acme"


@pytest.fixture
def rechecked(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, list[tuple[str, str]]]]:
    """The pairs each retry rechecked; the ones on "code-x-26-5" come back pending."""
    calls: list[tuple[str, list[tuple[str, str]]]] = []

    async def _recheck(org: str, pairs: list[tuple[str, str]]) -> list[tuple[str, str]]:
        calls.append((org, pairs))
        return [p for p in pairs if p[0] == "code-x-26-5"]

    async def _nothing(*_a: Any, **_k: Any) -> None:
        return None

    async def _canonical(org: str, subject: str, **_k: Any) -> str:
        return "alice" if subject == "alice@x.io" else subject

    monkeypatch.setattr(grant_writes, "recheck", _recheck)
    monkeypatch.setattr(grant_writes, "write_row", _nothing)
    monkeypatch.setattr(
        "server.identity.app.token_grants.cascade_revoke_to_created_tokens", _nothing
    )
    monkeypatch.setattr(grants_repo, "find_any_grant", _nothing)
    monkeypatch.setattr(grants_repo, "find_active_grant", _nothing)
    monkeypatch.setattr(grants_app, "_enforce_code_ceiling", _nothing)
    monkeypatch.setattr("server.auth.aliases.canonical_subject", _canonical)
    return calls


def _branches(monkeypatch: pytest.MonkeyPatch, branches: list[str]) -> list[tuple[str, str]]:
    cascaded: list[tuple[str, str]] = []

    async def _cascade(org: str, namespace: str, subject: str, *, retry: bool) -> None:
        assert retry
        cascaded.append((namespace, subject))

    monkeypatch.setattr(
        "server.code.app.index.maintainers.branch_namespaces", lambda org, ns: branches
    )
    monkeypatch.setattr(
        "server.identity.app.token_grants.cascade_revoke_to_created_tokens", _cascade
    )
    return cascaded


def test_a_retried_revoke_cascades_only_where_a_lowering_was_left_pending(
    rechecked: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    branches = ["code-x", "code-x-26-5", "code-x-26-6"]
    cascaded = _branches(monkeypatch, branches)
    revoke = GrantsRevokeInput(org=ORG, namespace="code-x", subject="alice@x.io")
    assert not asyncio.run(grants_app.handle_grants_revoke(revoke)).revoked
    assert rechecked == [(ORG, [(ns, s) for ns in branches for s in ("alice", "alice@x.io")])]
    # The cascade folds spellings itself, and 26-6 had no lowering pending.
    assert cascaded == [("code-x", "alice"), ("code-x-26-5", "alice")]


def test_a_retried_revoke_spares_a_branch_the_subject_still_holds(
    rechecked: list[Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _held_on_26_5(org: str, ns: str, subject: str, **_k: Any) -> GrantState | None:
        if ns != "code-x-26-5":
            return None
        return GrantState.from_row({"role": "admin", "status": "active", "granted_by": "ops"})

    cascaded = _branches(monkeypatch, ["code-x", "code-x-26-5"])
    monkeypatch.setattr(grants_repo, "find_any_grant", _held_on_26_5)
    asyncio.run(
        grants_app.handle_grants_revoke(
            GrantsRevokeInput(org=ORG, namespace="code-x", subject="alice")
        )
    )
    assert cascaded == [("code-x", "alice")]


@pytest.mark.parametrize(
    ("existing", "requested", "replace", "rechecks"),
    [
        # The same role again, with or without replace: perhaps a retry.
        ("writer", "writer", True, True),
        ("writer", "writer", False, True),
        # A raise is never cut, and a first grant has nothing to finish.
        ("viewer", "writer", True, False),
        (None, "writer", False, False),
    ],
)
def test_a_create_rechecks_only_when_it_repeats_the_role(
    rechecked: list[Any],
    monkeypatch: pytest.MonkeyPatch,
    existing: str | None,
    requested: str,
    replace: bool,
    rechecks: bool,
) -> None:
    async def _existing(*_a: Any, **_k: Any) -> GrantState | None:
        if existing is None:
            return None
        return GrantState.from_row({"role": existing, "status": "active", "granted_by": "ops"})

    monkeypatch.setattr(grants_repo, "find_active_grant", _existing)
    create = GrantsCreateInput(
        org=ORG,
        namespace="team",
        subject="bob",
        role=requested,  # type: ignore[arg-type]
        replace=replace,  # type: ignore[arg-type]
    )
    asyncio.run(grants_app.handle_grants_create(create))
    assert rechecked == ([(ORG, [("team", "bob")])] if rechecks else [])


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

    async def _recheck_logged(org: str, *, namespace: str) -> list[tuple[str, str]]:
        rechecked.append((org, namespace))
        return []

    monkeypatch.setattr(grant_writes, "recheck_logged", _recheck_logged)
    assert _release(_Registry(grants=[])).released
    # A pending request left behind still means the active grantees' tombstones landed.
    pending = [{"subject": "bob", "role": "viewer", "granted_by": "bob", "status": "pending"}]
    assert _release(_Registry(grants=pending)).released
    assert rechecked == [(ORG, "vault"), (ORG, "vault")]


def test_a_release_refused_after_its_tombstones_says_what_to_release_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _too_many(*_a: Any, **_k: Any) -> None:
        raise TooManySpellingsError("too many accounts", subject="bob", limit=1000)

    monkeypatch.setattr(grant_writes, "restamp", _too_many)
    live = [{"subject": "bob", "role": "writer", "granted_by": "alice", "status": "active"}]
    with pytest.raises(TooManySpellingsError, match="cannot release acme/vault"):
        _release(_Registry(grants=live))


def test_a_retried_token_grant_revoke_rechecks_the_subjects_logged_pairs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.identity.app import token_grants

    rechecked: list[tuple[str | None, str | None]] = []

    async def _recheck_logged(org: str, *, namespace: str | None, subject: str | None) -> list:
        rechecked.append((namespace, subject))
        return []

    class _NoLiveGrants:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr(grant_writes, "recheck_logged", _recheck_logged)
    first = token_grants.revoke_token_grants(ORG, "sa:bot-1", namespace="team", ch=_NoLiveGrants())
    assert asyncio.run(first) == 0 and rechecked == [], (
        "a first revoke with nothing live is a no-op"
    )
    again = token_grants.revoke_token_grants(
        ORG, "sa:bot-1", namespace="team", retry=True, ch=_NoLiveGrants()
    )
    assert asyncio.run(again) == 0 and rechecked == [("team", "sa:bot-1")]


def test_a_retried_approve_by_email_rechecks_both_spellings(rechecked: list[Any]) -> None:
    # Grant rows are stored under the login handle.
    approve = GrantsApproveInput(org=ORG, namespace="team", subject="alice@x.io")
    asyncio.run(grants_app.handle_grants_approve(approve))
    assert rechecked == [(ORG, [("team", "alice"), ("team", "alice@x.io")])]


@pytest.mark.grant_log_check
def test_the_grant_log_is_never_read_for_a_whole_org() -> None:
    with pytest.raises(ValueError, match="namespace or a subject"):
        asyncio.run(grants_repo.logged_pairs(ORG))
