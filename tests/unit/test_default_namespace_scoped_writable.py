"""An unaddressed MCP call's default is a namespace the token
can WRITE, and a scoped token's default is one it claims.

A pick saved in the dashboard's namespace picker on a namespace the subject only views, or
one a scoped token does not claim, must not become the default: every unaddressed
read would answer FORBIDDEN, or land on a viewer-only plane."""

from __future__ import annotations

import asyncio
import dataclasses
from typing import Any

import pytest

import server.transport.mcp_identity as mi
from server.auth.jwt import AuthInfo
from server.operations.base import Role

KINDS = {
    "jdoe": "memory",
    "core-qa": "memory",
    "team-alerts": "memory",
    "team-x": "memory",
    "team-y": "memory",
    "qa-clickhouse": "qa",
}


def _Auth(
    subject: str,
    namespaces: tuple[str, ...] = (),
    ns_roles: tuple[tuple[str, str], ...] = (),
    write_namespaces: tuple[str, ...] = (),
) -> AuthInfo:
    """A legacy token (no ns_roles) or a scoped one (ns_roles given)."""
    return AuthInfo(
        subject=subject,
        org="clickhouse",
        namespaces=namespaces,
        roles=() if ns_roles else (Role.WRITER,),
        is_service_account=False,
        ns_roles=ns_roles,
        write_namespaces=write_namespaces,
    )


class _CH:
    def __init__(self, saved: str | None, grants: dict[str, str]) -> None:
        self.saved, self.grants = saved, grants

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        if "user_state" in sql:
            return {"namespace": self.saved} if self.saved else None
        if "namespace_grants" in sql:
            ns = (params or {}).get("ns")
            return {"role": self.grants[ns]} if ns in self.grants else None
        return None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        tier = {"org_admin": 0, "admin": 1, "writer": 2}
        ordered = sorted(self.grants.items(), key=lambda kv: tier.get(kv[1], 3))
        return [{"namespace": n, "role": r} for n, r in ordered]


@pytest.fixture()
def wire(monkeypatch: pytest.MonkeyPatch):
    def _wire(ch: _CH) -> None:
        import server.auth.aliases as aliases
        import server.core.ch as chmod
        import server.identity.app.namespace_classification as nc
        import server.identity.app.namespace_kinds as nk

        async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
            return [subject]

        async def _kind_map(org: str) -> dict[str, str]:
            return dict(KINDS)

        import server.core.grant_cache as grant_cache

        ranks = {"viewer": 1, "writer": 2, "admin": 3}

        async def _grant_map(org: str, subject: str) -> grant_cache.GrantMap:
            return grant_cache.GrantMap(ranks={n: ranks[r] for n, r in ch.grants.items()})

        monkeypatch.setattr(chmod, "get_client", lambda: ch)
        monkeypatch.setattr(aliases, "subject_aliases", _aliases)
        monkeypatch.setattr(grant_cache, "_read", _grant_map)

        async def _none_restricted(org: str) -> frozenset[str]:
            return frozenset()

        monkeypatch.setattr(nk, "kind_map", _kind_map)
        monkeypatch.setattr(nc, "restricted_set", _none_restricted)
        from server.settings import settings

        monkeypatch.setattr(settings, "identity_provider", "trust-network")

    return _wire


def _ns(auth: _Auth) -> str:
    out = asyncio.run(mi._caller_defaults(auth, "claude-code"))
    assert out is not None
    return out["namespace"]


OWNER_GRANTS = {"core-qa": "admin", "team-alerts": "viewer", "jdoe": "admin"}


def test_owner_case_viewer_saved_pick_falls_through_to_home(wire) -> None:
    """The saved pick is viewer-only, so the home wins."""
    wire(_CH("team-alerts", OWNER_GRANTS))
    assert _ns(_Auth("jdoe", ("jdoe", "code-clickhouse", "qa-clickhouse"))) == "jdoe"


def test_scoped_token_ignores_a_saved_pick_it_does_not_claim(wire) -> None:
    wire(_CH("team-alerts", OWNER_GRANTS))
    token = _Auth("jdoe", ("core-qa",), ns_roles=(("core-qa", "writer"),))
    assert _ns(token) == "core-qa"


def test_scoped_viewer_token_defaults_inside_its_claims(wire) -> None:
    """A viewer-only scoped token still reads its own
    claim instead of a namespace it has no role on."""
    wire(_CH("team-alerts", OWNER_GRANTS))
    token = _Auth("jdoe", ("core-qa",), ns_roles=(("core-qa", "viewer"),))
    assert _ns(token) == "core-qa"


def test_scoped_token_honours_a_claimed_writable_saved_pick(wire) -> None:
    wire(_CH("team-y", {"team-x": "writer", "team-y": "writer", "jdoe": "admin"}))
    token = _Auth(
        "jdoe", ("team-x", "team-y"), ns_roles=(("team-x", "writer"), ("team-y", "writer"))
    )
    assert _ns(token) == "team-y"


def test_scoped_claim_order_is_authoritative(wire) -> None:
    """No usable saved pick: a scoped token's first writable claim wins, not
    the grant ranking's admin tier on a namespace it does not claim."""
    wire(_CH(None, {"jdoe": "admin", "team-y": "admin", "team-x": "writer"}))
    token = _Auth(
        "bob", ("team-x", "team-y"), ns_roles=(("team-x", "writer"), ("team-y", "viewer"))
    )
    assert _ns(token) == "team-x"


def test_home_beats_a_writable_team_namespace(wire) -> None:
    wire(_CH(None, {"team-x": "admin", "jdoe": "writer"}))
    assert _ns(_Auth("jdoe")) == "jdoe"


def test_viewer_saved_pick_prefers_a_writable_grant(wire) -> None:
    wire(_CH("team-alerts", {"team-alerts": "viewer", "team-x": "writer"}))
    assert _ns(_Auth("carol")) == "team-x"


def test_viewer_only_caller_still_gets_its_readable_plane(wire) -> None:
    wire(_CH(None, {"team-alerts": "viewer"}))
    assert _ns(_Auth("dave")) == "team-alerts"


def test_a_saved_plane_or_unclaimed_pick_is_no_default(wire) -> None:
    wire(_CH("qa-clickhouse", {"qa-clickhouse": "admin", "team-x": "writer"}))
    assert _ns(_Auth("z")) == "team-x"
    wire(_CH("team-y", {"team-x": "writer", "team-y": "writer"}))
    assert _ns(_Auth("z", ("team-x",), ns_roles=(("team-x", "writer"),))) == "team-x"


def test_a_non_home_default_is_named_and_home_is_silent() -> None:
    mi.note_default_namespace(True, "team-alerts", _Auth("jdoe"))
    assert mi.default_namespace_note() == {"namespace_defaulted": "team-alerts"}
    mi.note_default_namespace(True, "jdoe", _Auth("jdoe@example.com"))
    assert mi.default_namespace_note() == {}
    mi.note_default_namespace(False, "team-x", _Auth("jdoe"))  # explicit namespace
    assert mi.default_namespace_note() == {}


def test_an_agent_landing_in_its_own_namespace_is_not_away() -> None:
    mi.note_default_namespace(True, "ci-review", _Auth("sa:ci:review"))
    assert mi.default_namespace_note() == {}
    mi.note_default_namespace(True, "ci-other", _Auth("sa:ci:review"))
    assert mi.default_namespace_note() == {"namespace_defaulted": "ci-other"}
    # A token the agent delegated lands in the agent's namespace too.
    delegated = dataclasses.replace(_Auth("sa:review-tok-1a2b3c"), creator="sa:ci:review")
    mi.note_default_namespace(True, "ci-review", delegated)
    assert mi.default_namespace_note() == {}


def test_a_scoped_writer_outside_its_write_claim_is_not_the_default(wire) -> None:
    wire(_CH(None, {"team-x": "writer", "team-y": "writer"}))
    token = _Auth(
        "bob",
        ("team-y", "team-x"),
        ns_roles=(("team-y", "writer"), ("team-x", "writer")),
        write_namespaces=("team-x",),
    )
    assert _ns(token) == "team-x"
