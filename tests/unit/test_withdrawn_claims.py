"""A token's namespace claim ends with the grant it was minted on: revoking
the grant withdraws the claim from every token minted before the revocation,
while a claim never backed by a grant and a token minted later keep theirs."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from typing import Any

import pytest

import server.auth.context as ctx
import server.auth.revocation as rev
from server.auth.authz import check_authorized, withdrawn_claims
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "acme"
MINTED = 1_800_000_000


class _Grants:
    """Answers the withdrawn-claim read with the namespaces it was told the
    subject holds only revoked grants on; counts the reads."""

    def __init__(self) -> None:
        self.revoked_only: list[str] = []
        self.reads = 0
        self.fail = False

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[dict]:
        if "last_revoked" not in sql:
            return []
        self.reads += 1
        if self.fail:
            raise RuntimeError("replica away")
        asked = (params or {}).get("nss", [])
        return [{"namespace": ns} for ns in self.revoked_only if ns in asked]

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> None:
        return None


@pytest.fixture
def grants(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Grants]:
    ch = _Grants()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(rev, "_grants_revoked", {})
    yield ch
    ctx.set_current_auth(None)


def _revoked_at(monkeypatch: pytest.MonkeyPatch, namespace: str, at: int) -> None:
    monkeypatch.setattr(rev, "_grants_revoked", {**rev._grants_revoked, (ORG, namespace): at})


def _flat(*namespaces: str) -> AuthInfo:
    return AuthInfo(
        subject="alice",
        org=ORG,
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=False,
        issued_at=MINTED,
    )


def _scoped(namespace: str) -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org=ORG,
        namespaces=(namespace,),
        roles=(),
        is_service_account=True,
        ns_roles=((namespace, "writer"),),
        issued_at=MINTED,
    )


def _admits(auth: AuthInfo, namespace: str) -> bool:
    try:
        asyncio.run(check_authorized(auth, Role.VIEWER, ORG, namespace))
    except PermissionError:
        return False
    return True


def test_a_claim_is_read_without_io_until_a_revocation_could_touch_it(grants: _Grants) -> None:
    assert _admits(_flat("team"), "team")
    assert grants.reads == 0


def test_revoking_the_grant_withdraws_the_claim_of_an_older_token(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revoked_at(monkeypatch, "team", MINTED + 60)
    grants.revoked_only = ["team"]
    assert not _admits(_flat("team"), "team")
    assert not _admits(_scoped("team"), "team")


def test_a_token_minted_after_the_revocation_keeps_its_claim(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revoked_at(monkeypatch, "team", MINTED - 60)
    grants.revoked_only = ["team"]
    assert _admits(_flat("team"), "team")
    assert grants.reads == 0


def test_someone_elses_revocation_leaves_the_claim(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The namespace had a grant revoked, but not one of this subject's (or the
    # subject still holds a live one): the read finds nothing to withdraw.
    _revoked_at(monkeypatch, "team", MINTED + 60)
    assert _admits(_flat("team"), "team")
    assert grants.reads == 1


def test_a_failed_read_withdraws_the_claim(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revoked_at(monkeypatch, "team", MINTED + 60)
    grants.fail = True
    assert not _admits(_flat("team"), "team")


def test_a_wildcard_claim_names_no_namespace_to_withdraw(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revoked_at(monkeypatch, "team", MINTED + 60)
    grants.revoked_only = ["team"]
    assert asyncio.run(withdrawn_claims(_flat("*"), ORG, ["team"])) == set()


def test_the_callers_namespaces_leave_out_a_withdrawn_claim(
    grants: _Grants, monkeypatch: pytest.MonkeyPatch
) -> None:
    _revoked_at(monkeypatch, "team", MINTED + 60)
    grants.revoked_only = ["team"]
    ctx.set_current_auth(_flat("team", "notes"))
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == ["notes"]
    ctx.set_current_auth(_scoped("team"))
    assert asyncio.run(ctx.resolve_my_namespaces(ORG)) == []


def test_the_refresh_reads_the_last_grant_revocation_per_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _CH:
        async def query_all(self, sql: str, *a: object, **k: object) -> list[dict[str, object]]:
            if "namespace_grants" in sql:
                return [{"org": ORG, "namespace": "team", "last_revoked": MINTED + 60}]
            return []

    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    monkeypatch.setattr(rev, "_grants_revoked", {})
    asyncio.run(rev.refresh())
    assert rev.grant_revoked_since(ORG, "team", MINTED)
    assert not rev.grant_revoked_since(ORG, "team", MINTED + 61)
    assert not rev.grant_revoked_since(ORG, "notes", MINTED)
