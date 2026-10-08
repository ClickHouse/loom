"""Read-down inheritance: a token may read a parent namespace it inherits
but must not be able to write into it.

The asymmetry lives in two places:
  - AuthInfo.can_write (the writable scope, `ns_write` claim)
  - check_authorized, which gates WRITER+ on can_write instead of can_access

Hermetic: behavior is asserted against AuthInfo directly, and the
namespace-grant CH lookup is patched out, so no ClickHouse is required.
"""

from __future__ import annotations

import asyncio

import pytest

import server.auth.authz as authz
import server.auth.jwt as jwt_mod
from server.auth.authz import Role, check_authorized
from server.auth.jwt import AuthInfo, mint_token, verify_token


def _private_auth() -> AuthInfo:
    """A child bot that inherits read of a public namespace but only writes
    to a private one."""
    return AuthInfo(
        subject="clickgap-private",
        org="clickgap",
        namespaces=("clickgap-public", "clickgap-private"),
        write_namespaces=("clickgap-private",),
        roles=(Role.WRITER,),
        is_service_account=True,
    )


@pytest.fixture(autouse=True)
def _no_ch_grant_lookup(monkeypatch):
    """The fallback grant lookup hits ClickHouse; stub it to "no grant" so
    these tests stay unit-level. Tests that want a grant override it."""

    async def _none(org, namespace, subject):
        return None

    monkeypatch.setattr(authz, "_lookup_grant", _none)


def test_can_write_respects_scope_can_access_does_not():
    auth = _private_auth()
    # Reads the parent...
    assert auth.can_access("clickgap", "clickgap-public") is True
    assert auth.can_access("clickgap", "clickgap-private") is True
    # ...but only writes its own.
    assert auth.can_write("clickgap", "clickgap-private") is True
    assert auth.can_write("clickgap", "clickgap-public") is False


def test_legacy_auth_is_writable_wherever_readable():
    """No ns_write set → unchanged behavior (writable == readable)."""
    auth = AuthInfo(
        subject="legacy",
        org="clickgap",
        namespaces=("clickgap-public", "clickgap-private"),
        roles=(Role.WRITER,),
        is_service_account=True,
    )
    assert auth.write_namespaces == ()
    assert auth.can_write("clickgap", "clickgap-public") is True
    assert auth.can_write("clickgap", "clickgap-private") is True


def test_check_authorized_allows_read_of_inherited_parent():
    asyncio.run(check_authorized(_private_auth(), Role.VIEWER, "clickgap", "clickgap-public"))


def test_check_authorized_allows_write_to_own_namespace():
    asyncio.run(check_authorized(_private_auth(), Role.WRITER, "clickgap", "clickgap-private"))


def test_check_authorized_rejects_write_to_inherited_parent():
    with pytest.raises(PermissionError):
        asyncio.run(check_authorized(_private_auth(), Role.WRITER, "clickgap", "clickgap-public"))


def test_explicit_writer_grant_still_allows_write_up(monkeypatch):
    """An explicit WRITER grant on the parent overrides the ns_write scope —
    the deliberate, audited way to publish upward."""

    async def _writer_grant(org, namespace, subject):
        return Role.WRITER if namespace == "clickgap-public" else None

    monkeypatch.setattr(authz, "_lookup_grant", _writer_grant)
    asyncio.run(check_authorized(_private_auth(), Role.WRITER, "clickgap", "clickgap-public"))


def test_ns_write_claim_round_trips(monkeypatch):
    """mint_token/verify_token preserve the ns_write claim. Pin HS256 so the
    explicit-secret round-trip is independent of the deployment algorithm."""
    monkeypatch.setattr(jwt_mod.settings, "jwt_algorithm", "HS256")
    secret = "unit-test-secret-unit-test-secret"  # >=32 bytes for HS256
    tok = mint_token(
        "clickgap-private",
        "clickgap",
        ["clickgap-public", "clickgap-private"],
        ["writer"],
        write_namespaces=["clickgap-private"],
        secret=secret,
    )
    auth = verify_token(tok, secret=secret)
    assert auth.namespaces == ("clickgap-public", "clickgap-private")
    assert auth.write_namespaces == ("clickgap-private",)
