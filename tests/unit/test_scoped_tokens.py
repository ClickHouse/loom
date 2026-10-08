"""Scoped tokens: ordered `namespaces` claim + authoritative per-namespace
`ns_roles` map.

A scoped token (carries `ns_roles`) is restricted to exactly its listed
namespaces (no union of other grants), preserves their ORDER as read priority,
and authorizes per namespace from `ns_roles` — capped at the minter's grant, so
it cannot over-write a namespace it only reads. Legacy tokens (no `ns_roles`)
keep the old union + flat-role behavior. Pure unit: no live ClickHouse — the
scoped path in resolve_my_namespaces returns before touching CH.
"""

from __future__ import annotations

import asyncio

import pytest

from server.auth.authz import Role, check_authorized
from server.auth.context import resolve_my_namespaces, set_current_auth
from server.auth.jwt import AuthInfo, mint_token, verify_token

_SECRET = "unit-secret-unit-secret-unit-secret-32"


def _scoped(**ns_roles: str) -> AuthInfo:
    """A verified scoped token: namespaces = ns_roles keys in insertion order."""
    tok = mint_token(
        "sa:bot",
        "acme",
        namespaces=list(ns_roles.keys()),
        roles=[],
        ns_roles=dict(ns_roles),
        secret=_SECRET,
    )
    return verify_token(tok, secret=_SECRET)


# --------------------------------------------------------------------------
# JWT round-trip: order preserved, ns_roles parsed, effective_role resolves.
# --------------------------------------------------------------------------


def test_ns_roles_round_trip_preserves_order() -> None:
    info = _scoped(priv="admin", kb="viewer", team="writer")
    # namespaces claim keeps mint order = read priority.
    assert info.namespaces == ("priv", "kb", "team")
    assert info.has_ns_roles() is True
    assert info.effective_role("acme", "priv") == Role.ADMIN
    assert info.effective_role("acme", "kb") == Role.VIEWER
    assert info.effective_role("acme", "team") == Role.WRITER
    # Unlisted namespace / wrong org → no effective role.
    assert info.effective_role("acme", "other") is None
    assert info.effective_role("other-org", "priv") is None


def test_legacy_token_has_no_ns_roles() -> None:
    tok = mint_token("u", "acme", namespaces=["home"], roles=["admin"], secret=_SECRET)
    info = verify_token(tok, secret=_SECRET)
    assert info.has_ns_roles() is False
    assert info.effective_role("acme", "home") is None


# --------------------------------------------------------------------------
# check_authorized: per-namespace role governs; escalation + unlisted denied.
# --------------------------------------------------------------------------


async def _allowed(info: AuthInfo, role: Role, ns: str) -> bool:
    try:
        await check_authorized(info, role, "acme", ns)
        return True
    except PermissionError:
        return False


async def test_scoped_authz_respects_per_namespace_role() -> None:
    info = _scoped(priv="admin", kb="viewer")
    # admin namespace: read + write + admin all allowed.
    assert await _allowed(info, Role.VIEWER, "priv")
    assert await _allowed(info, Role.WRITER, "priv")
    assert await _allowed(info, Role.ADMIN, "priv")
    # viewer namespace: read allowed, write/admin denied (no escalation).
    assert await _allowed(info, Role.VIEWER, "kb")
    assert not await _allowed(info, Role.WRITER, "kb")
    assert not await _allowed(info, Role.ADMIN, "kb")
    # namespace not in the token: denied even for read.
    assert not await _allowed(info, Role.VIEWER, "other")


# --------------------------------------------------------------------------
# resolve_my_namespaces: scoped token = ordered claim verbatim, no union, and
# the only ClickHouse read is the 30 s-cached restricted-namespace snapshot
# — no grant lookup for a claim that names no restricted
# namespace.
# --------------------------------------------------------------------------


def _resolve(info: AuthInfo, monkeypatch: pytest.MonkeyPatch) -> list[str]:
    # If the scoped path ever touched CH beyond the classification snapshot
    # this would blow up — asserting the early return stays grant-free (and
    # order-preserving).
    def _boom() -> object:
        raise AssertionError("scoped resolve must not query ClickHouse")

    async def _nothing_restricted(org: str) -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr("server.core.ch.get_client", _boom)
    monkeypatch.setattr(
        "server.identity.app.namespace_classification.restricted_set", _nothing_restricted
    )
    set_current_auth(info)
    try:
        return asyncio.run(resolve_my_namespaces("acme"))
    finally:
        set_current_auth(None)


def test_scoped_resolve_is_ordered_and_restricted(monkeypatch: pytest.MonkeyPatch) -> None:
    info = _scoped(kb="viewer", priv="admin")  # deliberately kb-first
    # Verbatim claim order, no union of any other grants, no CH read.
    assert _resolve(info, monkeypatch) == ["kb", "priv"]


def test_scoped_resolve_reversed_order(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same two namespaces in the opposite priority order round-trips as-is
    # (proves the order is honored, not sorted/heuristic).
    info = _scoped(priv="admin", kb="viewer")
    assert _resolve(info, monkeypatch) == ["priv", "kb"]


def test_an_ns_roles_entry_above_admin_confers_nothing() -> None:
    """Every Loom mint path admits only viewer | writer | admin per namespace;
    a token carrying an org- or platform-wide role in `ns_roles` (a foreign
    minter, a hand-edited payload) gets no role there and no best role from it."""
    info = _scoped(priv="platform_admin", team="org_admin", kb="viewer")
    assert info.effective_role("acme", "priv") is None
    assert info.effective_role("acme", "team") is None
    assert info.effective_role("acme", "kb") == Role.VIEWER
    assert info.best_ns_role() == Role.VIEWER
    assert _scoped(priv="PLATFORM_ADMIN").best_ns_role() is None
