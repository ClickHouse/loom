"""Read scope: a request narrows, a token caps, and the
result is a user/session predicate on every retrieval leg.

Pure unit: no ClickHouse. Covers the resolver, the WHERE builder, the JWT
claim round-trip and the op inputs' wire fields.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from server.auth.jwt import READ_SCOPES, AuthInfo, mint_token, validate_read_scope, verify_token
from server.core.errors import AuthInsufficientError, IdentityIncompleteError
from server.identity.domain.read_scope import (
    _NARROWNESS,
    effective_read_scope,
    narrower,
    resolve_read_filters,
)
from server.memory.domain.search_sql import _build_where
from server.memory.domain.search_types import SearchQuery

_SECRET = "unit-secret-unit-secret-unit-secret-32"


def _auth(**kw) -> AuthInfo:
    base = dict(
        subject="sa:bot",
        org="acme",
        namespaces=("shared",),
        roles=(),
        is_service_account=True,
    )
    base.update(kw)
    return AuthInfo(**base)


def _q(**kw) -> SearchQuery:
    base = dict(
        org="o",
        namespace="n",
        scope="private",
        agent="*",
        embedding_model="m",
        query_vec=[0.0],
        top_k=5,
    )
    base.update(kw)
    return SearchQuery(**base)


# --------------------------------------------------------------------------
# Resolver
# --------------------------------------------------------------------------


def test_narrower_orders_scopes():
    assert narrower("namespace", "own_user") == "own_user"
    assert narrower("own_session", "own_user") == "own_session"
    assert narrower("namespace", "namespace") == "namespace"
    assert narrower("bogus", "own_user") == "own_user"


def test_namespace_scope_without_claim_is_a_no_op():
    rf = resolve_read_filters(
        requested="namespace",
        user_id=None,
        session_id=None,
        auth=None,
    )
    assert (rf.scope, rf.user_id, rf.session_id) == ("namespace", None, None)


def test_namespace_scope_honours_explicit_body_filters():
    rf = resolve_read_filters(
        requested="namespace",
        user_id="u1",
        session_id="s1",
        auth=_auth(),
    )
    assert (rf.user_id, rf.session_id) == ("u1", "s1")


def test_sentinel_identity_means_unset():
    rf = resolve_read_filters(
        requested="namespace",
        user_id="-",
        session_id="-",
        auth=None,
    )
    assert (rf.user_id, rf.session_id) == (None, None)


def test_request_narrows_to_own_user():
    rf = resolve_read_filters(
        requested="own_user",
        user_id="u1",
        session_id=None,
        auth=_auth(),
    )
    assert rf.scope == "own_user" and rf.user_id == "u1"


def test_own_user_without_user_id_is_identity_incomplete():
    with pytest.raises(IdentityIncompleteError):
        resolve_read_filters(
            requested="own_user",
            user_id=None,
            session_id=None,
            auth=_auth(),
        )


def test_own_session_requires_both_identities():
    with pytest.raises(IdentityIncompleteError):
        resolve_read_filters(
            requested="own_session",
            user_id="u1",
            session_id="-",
            auth=None,
        )
    rf = resolve_read_filters(
        requested="own_session",
        user_id="u1",
        session_id="s9",
        auth=None,
    )
    assert (rf.scope, rf.user_id, rf.session_id) == ("own_session", "u1", "s9")


def test_token_claim_is_a_ceiling_the_request_cannot_widen():
    auth = _auth(read_scope="own_user")
    assert effective_read_scope("namespace", auth) == "own_user"
    assert effective_read_scope("own_session", auth) == "own_session"
    with pytest.raises(IdentityIncompleteError):
        resolve_read_filters(
            requested="namespace",
            user_id=None,
            session_id=None,
            auth=auth,
        )
    rf = resolve_read_filters(
        requested="namespace",
        user_id="u2",
        session_id=None,
        auth=auth,
    )
    assert rf.scope == "own_user" and rf.user_id == "u2"


def test_pinned_user_fills_an_absent_body_user_and_narrows():
    auth = _auth(read_user_id="alice")
    rf = resolve_read_filters(
        requested="namespace",
        user_id=None,
        session_id=None,
        auth=auth,
    )
    assert (rf.scope, rf.user_id) == ("own_user", "alice")


def test_pinned_user_refuses_a_body_naming_another_user():
    auth = _auth(read_user_id="alice")
    with pytest.raises(AuthInsufficientError):
        resolve_read_filters(
            requested="namespace",
            user_id="bob",
            session_id=None,
            auth=auth,
        )


def test_validate_read_scope_rejects_unknown_values():
    assert validate_read_scope(" own_user ") == "own_user"
    with pytest.raises(ValueError):
        validate_read_scope("everything")


# --------------------------------------------------------------------------
# WHERE builder
# --------------------------------------------------------------------------


def test_where_has_no_session_predicate_by_default():
    where, params = _build_where(_q())
    assert not any("session_id" in w for w in where)
    assert "session_id" not in params


def test_where_binds_session_predicate_when_set():
    where, params = _build_where(_q(session_id="s1", user_id="u1"))
    assert "e.session_id = {session_id:String}" in where
    assert "e.user_id = {user_id:String}" in where
    assert params["session_id"] == "s1" and params["user_id"] == "u1"


# --------------------------------------------------------------------------
# JWT claims
# --------------------------------------------------------------------------


def test_read_scope_claims_round_trip():
    tok = mint_token(
        "sa:bot",
        "acme",
        namespaces=["shared"],
        roles=["viewer"],
        read_scope="own_user",
        read_user_id="alice",
        secret=_SECRET,
    )
    info = verify_token(tok, secret=_SECRET)
    assert info.read_scope == "own_user"
    assert info.read_user_id == "alice"


def test_legacy_token_has_empty_read_scope():
    tok = mint_token("sa:bot", "acme", namespaces=["shared"], roles=["viewer"], secret=_SECRET)
    info = verify_token(tok, secret=_SECRET)
    assert info.read_scope == "" and info.read_user_id == ""
    assert effective_read_scope("namespace", info) == "namespace"


def test_mint_rejects_unknown_read_scope():
    with pytest.raises(ValueError):
        mint_token(
            "sa:bot",
            "acme",
            namespaces=["shared"],
            roles=["viewer"],
            read_scope="everything",
            secret=_SECRET,
        )


# --------------------------------------------------------------------------
# Wire fields
# --------------------------------------------------------------------------


def test_search_ask_pack_inputs_carry_read_scope():
    from server.memory.api.models_ask import MemoryAskInput
    from server.memory.api.models_context import ContextPackInput
    from server.memory.api.models_search import MemorySearchInput

    s = MemorySearchInput(org="o", namespace="n", query="q")
    assert s.read_scope == "namespace" and s.session_id is None
    s2 = MemorySearchInput(
        org="o",
        namespace="n",
        query="q",
        read_scope="own_session",
        user_id="u",
        session_id="s",
    )
    assert (s2.read_scope, s2.session_id) == ("own_session", "s")
    a = MemoryAskInput(org="o", namespace="n", agent="*", query="q", read_scope="own_user")
    assert a.read_scope == "own_user"
    p = ContextPackInput(org="o", namespace="n", agent="a", read_scope="own_user")
    assert p.read_scope == "own_user"
    with pytest.raises(ValidationError):
        MemorySearchInput(org="o", namespace="n", query="q", read_scope="all")


def test_search_handler_applies_scope_from_token(monkeypatch):
    from server.auth.context import current_auth, set_current_auth
    from server.identity.domain.read_scope import pin_read_filters
    from server.memory.api.models_search import MemorySearchInput

    def _apply_read_scope(inp):
        return pin_read_filters(inp, current_auth())[0]

    set_current_auth(_auth(read_scope="own_user"))
    try:
        inp = MemorySearchInput(org="o", namespace="n", query="q", user_id="u1")
        out = _apply_read_scope(inp)
        assert out.read_scope == "own_user" and out.user_id == "u1"
        with pytest.raises(IdentityIncompleteError):
            _apply_read_scope(MemorySearchInput(org="o", namespace="n", query="q"))
    finally:
        set_current_auth(None)
    # No token, namespace scope: the input object is returned untouched.
    inp = MemorySearchInput(org="o", namespace="n", query="q")
    assert _apply_read_scope(inp) is inp


def test_the_claim_values_and_the_narrowness_order_are_one_set() -> None:
    # The token validator and the resolver read the same values, widest first.
    assert tuple(sorted(_NARROWNESS, key=_NARROWNESS.__getitem__)) == READ_SCOPES
