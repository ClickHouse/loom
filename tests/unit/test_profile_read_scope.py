"""The profile section is bounded by the read scope: under
own_user / own_session only this user's sections and the namespace-level ones
render."""

from __future__ import annotations

from server.memory.api.models_context import ContextPackInput
from server.memory.api.models_profile import ProfileRenderInput
from server.memory.app.profile import profile_user_clause


def test_namespace_scope_adds_no_user_predicate():
    clause, params = profile_user_clause("namespace", "u1", "s1")
    assert clause == "" and params == {}


def test_own_user_keeps_this_user_and_namespace_level_sections():
    clause, params = profile_user_clause("own_user", "u1", None)
    assert "user_id = {uid:String}" in clause and "user_id = '-'" in clause
    assert "session_id" not in clause
    assert params == {"uid": "u1"}


def test_own_session_also_narrows_on_session():
    clause, params = profile_user_clause("own_session", "u1", "s9")
    assert "(session_id = {sid:String} OR session_id = '-')" in clause
    assert params == {"uid": "u1", "sid": "s9"}


def test_missing_user_means_no_predicate_the_resolver_refused_earlier():
    # resolve_read_filters raises before this point when own_user has no
    # user; the helper itself stays a no-op so a caller cannot half-apply it.
    assert profile_user_clause("own_user", None, None) == ("", {})


def test_render_and_pack_inputs_carry_read_scope():
    r = ProfileRenderInput(org="o", namespace="n", agent="a")
    assert r.read_scope == "namespace"
    r2 = ProfileRenderInput(org="o", namespace="n", agent="a", read_scope="own_user")
    assert r2.read_scope == "own_user"
    p = ContextPackInput(org="o", namespace="n", agent="a", read_scope="own_session")
    assert p.read_scope == "own_session"
