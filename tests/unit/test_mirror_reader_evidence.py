"""Mirrored readers use directory evidence rather than subject-shaped addresses."""

from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from server.auth import reader_identity, sso_links


def account(**changes):
    return {
        "namespace": "home",
        "owner_email": "lee",
        "contact_email": "lee@example.com",
        "password_hash": "",
        "hosted_domain": "example.com",
        "issuer": "https://idp.example",
        **changes,
    }


@pytest.fixture
def directory(monkeypatch):
    ch = SimpleNamespace(query_all=AsyncMock(return_value=[account()]))
    monkeypatch.setattr(reader_identity.settings, "oidc_issuer", "https://idp.example/")
    monkeypatch.setattr(reader_identity.settings, "oauth_login_provider", "oidc")
    return ch


@pytest.mark.asyncio
async def test_verified_rows_prove_email_and_signed_domain(directory):
    actual = await reader_identity.resolve_reader("acme", "lee", ch=directory)
    assert actual == reader_identity.ReaderIdentity(
        "lee", frozenset({"lee@example.com"}), frozenset({"example.com"})
    )
    sql = directory.query_all.call_args.args[0]
    assert "loom.namespaces FINAL" in sql and "loom.sso_links FINAL" in sql
    assert "{org:String}" in sql and "contact_email != ''" in sql
    assert "password_hash = '' OR" in sql
    assert directory.query_all.call_args.kwargs["params"] == {
        "org": "acme",
        "sub": "lee",
        "owner": "lee",
    }


@pytest.mark.asyncio
async def test_bare_email_subject_is_not_evidence(directory):
    directory.query_all.return_value = []
    actual = await reader_identity.resolve_reader("acme", "victim@example.com", ch=directory)
    assert actual == reader_identity.ReaderIdentity("victim@example.com", frozenset(), frozenset())


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes",
    [
        {"password_hash": "hash"},
        {"hosted_domain": ""},
        {"hosted_domain": "*.example.com"},
        {"issuer": "https://other.example"},
        {"issuer": ""},
    ],
)
async def test_email_proof_does_not_infer_domain(directory, changes):
    directory.query_all.return_value = [account(**changes)]
    actual = await reader_identity.resolve_reader("acme", "lee", ch=directory)
    assert actual.emails == frozenset({"lee@example.com"})
    assert actual.domains == frozenset()


@pytest.mark.asyncio
async def test_multiple_owners_and_excess_rows_fail_closed(directory):
    directory.query_all.return_value = [account(), account(owner_email="other")]
    assert await reader_identity.resolve_reader("acme", "lee@example.com", ch=directory) is None
    directory.query_all.return_value = [account()] * 129
    assert await reader_identity.resolve_reader("acme", "lee", ch=directory) is None


@pytest.mark.parametrize(
    "claim", [None, "", "*.example.com", "a@b.com", "example.com.", "a..b", "-a.com", 1, []]
)
def test_bad_hosted_domain_is_empty(claim):
    assert sso_links.hosted_domain(claim) == ""


def test_signed_hosted_domain_normalization():
    assert sso_links.hosted_domain("Example.COM") == "example.com"


@pytest.mark.asyncio
async def test_attestation_rereads_under_account_lock(monkeypatch):
    locked = set()
    calls = []

    @asynccontextmanager
    async def account_change(org, ns):
        locked.add((org, ns))
        try:
            yield
        finally:
            locked.remove((org, ns))

    async def query_all(sql, params):
        calls.append(sql)
        assert "LIMIT" not in sql  # missing claims must clear every account
        if len(calls) == 1:
            assert not locked
        else:
            assert locked == {("acme", "home")}
            assert "owner_email = {owner:String}" in sql
            assert "password_hash = ''" in sql and "active = 1" in sql
        return [{"namespace": "home"}]

    ch = SimpleNamespace(query_all=query_all)
    write = AsyncMock()
    monkeypatch.setattr(sso_links, "get_client", lambda: ch)
    monkeypatch.setattr(sso_links, "_write", write)
    monkeypatch.setattr("server.auth.grant_claims.account_change", account_change)
    await sso_links.attest_domain(
        "lee@example.com", "acme", "lee", {"iss": "https://idp.example", "hd": "Example.COM"}
    )
    write.assert_awaited_once_with(
        "lee@example.com",
        [("acme", "home")],
        "lee",
        linked=1,
        hosted_domain="example.com",
        issuer="https://idp.example",
        ch=ch,
    )
    assert not locked


@pytest.mark.asyncio
async def test_release_between_discovery_and_lock_writes_nothing(monkeypatch):
    @asynccontextmanager
    async def account_change(*args):
        yield

    ch = SimpleNamespace(query_all=AsyncMock(side_effect=[[{"namespace": "home"}], []]))
    write = AsyncMock()
    monkeypatch.setattr(sso_links, "get_client", lambda: ch)
    monkeypatch.setattr(sso_links, "_write", write)
    monkeypatch.setattr("server.auth.grant_claims.account_change", account_change)
    await sso_links.attest_domain(
        "lee@example.com", "acme", "lee", {"iss": "https://idp.example", "hd": "example.com"}
    )
    write.assert_not_awaited()


@pytest.mark.asyncio
async def test_password_owner_keeps_identity_without_verified_contact(directory):
    directory.query_all.return_value = []
    assert await reader_identity.resolve_reader(
        "acme", "lee", ch=directory
    ) == reader_identity.ReaderIdentity("lee", frozenset(), frozenset())


@pytest.mark.asyncio
async def test_email_subject_collects_canonical_owners_other_verified_contacts(directory):
    directory.query_all.side_effect = [
        [{"owner_email": "lee", "contact_email": "lee@example.com"}],
        [account(), account(namespace="other", contact_email="other@example.com")],
    ]
    actual = await reader_identity.resolve_reader("acme", "lee@example.com", ch=directory)
    assert actual.emails == frozenset({"lee@example.com", "other@example.com"})
    assert directory.query_all.call_args.kwargs["params"]["owner"] == "lee"


@pytest.mark.asyncio
@pytest.mark.parametrize("deactivated", [False, True])
async def test_callback_evidence_failure_revokes_and_never_starts_session(
    monkeypatch, auth_attempts, deactivated
):
    import time

    from starlette.responses import Response

    from server.auth import oauth, oidc
    from tests.unit.oauth_store_fake import MemoryOAuthStore

    pending = {
        "kind": "dashboard",
        "nonce": "n",
        "code_verifier": "v",
        "state": "s",
        "exp": time.time() + 600,
    }

    class Request:
        client = None
        query_params = {"code": "c", "state": "s"}
        cookies = {oauth._state_cookie("s"): oauth.seal_pending(pending)}

    evidence = AsyncMock(side_effect=RuntimeError("private backend exception"))
    session = AsyncMock(return_value=Response(status_code=302))
    revoke = AsyncMock()
    monkeypatch.setattr(oauth, "store", MemoryOAuthStore())
    monkeypatch.setattr(
        oauth,
        "_exchange_at_idp",
        AsyncMock(
            return_value=(
                {"refresh_token": "upstream"},
                {
                    "email": "lee@example.com",
                    "email_verified": True,
                    "iss": "https://idp.example",
                    "hd": "example.com",
                },
            )
        ),
    )
    monkeypatch.setattr(
        oauth, "_account_for", AsyncMock(return_value=("lee", "acme", ["home"], ["writer"]))
    )
    monkeypatch.setattr(
        oauth,
        "_refused_sign_in",
        AsyncMock(return_value=Response(status_code=403) if deactivated else None),
    )
    monkeypatch.setattr(oauth, "_dashboard_signed_in", session)
    monkeypatch.setattr(sso_links, "attest_domain", evidence)
    monkeypatch.setattr(oidc, "revoke", revoke)
    response = await oauth.oidc_callback(Request())
    session.assert_not_awaited()
    if deactivated:
        assert response.status_code == 403
        evidence.assert_not_awaited()
    else:
        assert response.status_code == 503
        assert b"private backend exception" not in response.body
        assert b"again" in response.body.lower()
        revoke.assert_awaited_once_with("upstream")


@pytest.mark.asyncio
async def test_cancelled_attestation_does_not_become_restart_response(monkeypatch):
    import asyncio

    from server.auth import oauth, oidc

    monkeypatch.setattr(oauth, "_refused_sign_in", AsyncMock(return_value=None))
    monkeypatch.setattr(sso_links, "attest_domain", AsyncMock(side_effect=asyncio.CancelledError))
    revoke = AsyncMock()
    monkeypatch.setattr(oidc, "revoke", revoke)
    with pytest.raises(asyncio.CancelledError):
        await oauth._signed_in_as(
            {"kind": "dashboard"},
            ("lee", "acme", ["home"], ["writer"]),
            "upstream",
            "lee@example.com",
            verified_claims={"iss": "https://idp.example"},
        )
    revoke.assert_not_awaited()
