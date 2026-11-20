"""The namespaces the MCP transport tells a token to name are exactly the
named claims the gate lets it read: `readable_named_claims` and
`authz.check_authorized(VIEWER, ns)` agree for every kind of token. And the
default an unaddressed call lands in is one the gate lets it write, whenever
it may write any of its claims."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.auth import authz
from server.auth.context import readable_named_claims
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "o"
RESTRICTED = frozenset({"secret"})

# (subject, namespace) -> live grant rank; 0 is a grant row no longer live.
GRANTS = {
    ("alice", "team"): 2,
    ("alice", "secret"): 1,
    ("alice", "docs"): 1,
    ("alice", "left"): 0,
    ("alice", "gone"): 0,
    ("sa:cli", "team"): 0,
    ("sa:inherits", "team"): 0,
    ("sa:inherits", "solo"): 0,
}


@pytest.fixture(autouse=True)
def _grants(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.delegation as delegation
    import server.auth.service_accounts as service_accounts
    import server.core.grant_cache as grant_cache
    import server.identity.app.namespace_classification as nc

    async def _read(org: str, subject: str) -> grant_cache.GrantMap:
        return grant_cache.GrantMap(ranks={ns: r for (s, ns), r in GRANTS.items() if s == subject})

    async def _grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
        rank = GRANTS.get((subject, namespace), 0)
        return {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}.get(rank)

    async def _inheriting_creator(org: str, subject: str, **_: Any) -> str | None:
        return "alice" if subject == "sa:inherits" else None

    async def _active(org: str, creator: str) -> bool:
        return True

    async def _restricted_set(org: str) -> frozenset[str]:
        return RESTRICTED

    monkeypatch.setattr(grant_cache, "_read", _read)
    monkeypatch.setattr(authz, "grant_role_for", _grant_role_for)
    monkeypatch.setattr(service_accounts, "inheriting_creator", _inheriting_creator)
    monkeypatch.setattr(delegation, "creator_account_active", _active)
    monkeypatch.setattr(nc, "restricted_set", _restricted_set)


class _NoRows:
    """No saved default and no ranked grants: the default comes from the claims."""

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> Any:
        return None

    async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> list[Any]:
        return []


@pytest.fixture()
def _default_inputs(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.aliases as aliases
    import server.core.ch as chmod
    import server.identity.app.namespace_kinds as nk
    from server.settings import settings

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    async def _kinds(org: str) -> dict[str, str]:
        return {}

    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(nk, "kind_map", _kinds)
    monkeypatch.setattr(chmod, "get_client", lambda: _NoRows())
    monkeypatch.setattr(settings, "identity_provider", "trust-network")


def _token(subject: str, namespaces: tuple[str, ...], **kw: Any) -> AuthInfo:
    base: dict[str, Any] = {"org": ORG, "roles": (Role.WRITER,), "is_service_account": False}
    return AuthInfo(subject=subject, namespaces=namespaces, **{**base, **kw})


TOKENS = {
    "flat human": _token("alice", ("team", "home", "gone")),
    "scoped human": _token(
        "alice", ("team", "gone"), ns_roles=(("team", "viewer"), ("gone", "writer"))
    ),
    "delegated, own grant revoked, creator holds it": _token(
        "sa:cli", ("team",), creator="alice", is_service_account=True
    ),
    "delegated, creator lost it": _token(
        "sa:cli", ("left", "team"), creator="alice", is_service_account=True
    ),
    "inheriting service account, own claim capped": _token(
        "sa:inherits", ("team", "solo"), is_service_account=True
    ),
    "restricted claims": _token("alice", ("secret", "team")),
    "restricted claim without a grant": _token("bob", ("secret", "home")),
    "delegated restricted claim": _token(
        "sa:cli", ("secret", "team"), creator="alice", is_service_account=True
    ),
    "delegated, creator only reads the first claim": _token(
        "sa:cli", ("docs", "team"), creator="alice", is_service_account=True
    ),
    "token of another org": _token("alice", ("team", "home"), org="other"),
    "org-wide token": _token("alice", ("secret", "team"), org="*"),
}


async def _gate_admits(token: AuthInfo, role: Role, namespace: str, org: str = ORG) -> bool:
    try:
        await authz.check_authorized(token, role, org, namespace)
    except PermissionError:
        return False
    return True


async def _gate_reads(token: AuthInfo, namespace: str) -> bool:
    return await _gate_admits(token, Role.VIEWER, namespace)


@pytest.mark.parametrize("kind", list(TOKENS))
def test_the_named_claims_are_the_ones_the_gate_lets_the_token_read(kind: str) -> None:
    token = TOKENS[kind]

    async def _both() -> tuple[list[str], list[str]]:
        admitted = [ns for ns in token.namespaces if await _gate_reads(token, ns)]
        return await readable_named_claims(token, ORG), admitted

    readable, admitted = asyncio.run(_both())
    assert readable == admitted


@pytest.mark.parametrize("kind", list(TOKENS))
def test_the_default_is_one_the_gate_lets_the_token_write(_default_inputs, kind: str) -> None:
    from server.identity.app.caller_home import caller_org, default_and_readable

    token = TOKENS[kind]
    org = caller_org(token) or ORG

    async def _check() -> tuple[str, bool, bool]:
        default, _ = await default_and_readable(token, org)
        writes_somewhere = any(
            [await _gate_admits(token, Role.WRITER, ns, org) for ns in token.namespaces]
        )
        writes_default = bool(default) and await _gate_admits(token, Role.WRITER, default, org)
        return default, writes_somewhere, writes_default

    default, writes_somewhere, writes_default = asyncio.run(_check())
    if writes_somewhere:
        assert writes_default, default
    elif default:  # a token that writes nowhere still reads its default
        assert asyncio.run(_gate_admits(token, Role.VIEWER, default, org)), default


def test_a_delegated_default_skips_a_claim_its_creator_only_reads(_default_inputs) -> None:
    # alice reads `docs` but writes `team`: the write home is `team`.
    from server.identity.app.caller_home import default_namespace

    token = TOKENS["delegated, creator only reads the first claim"]
    assert asyncio.run(default_namespace(token, ORG)) == "team"


def test_the_cases_cover_both_outcomes() -> None:
    # Guard against a fixture that admits (or refuses) everything.
    async def _outcomes() -> set[bool]:
        return {await _gate_reads(t, ns) for t in TOKENS.values() for ns in t.namespaces}

    assert asyncio.run(_outcomes()) == {True, False}


def test_no_token_names_nothing() -> None:
    assert asyncio.run(readable_named_claims(None, ORG)) == []


def _saved_and_ranked(
    monkeypatch: pytest.MonkeyPatch, saved: str, rows: dict[str, list[str]] | None = None
) -> None:
    """alice's saved pick in the namespace picker, and each subject's live grants as the
    ranking lists them."""
    import server.identity.app.caller_defaults as caller_defaults

    async def _saved(org: str, subject: str, *, ch: Any = None) -> str:
        return saved if subject == "alice" else ""

    async def _ranked(org: str, subs: list[str], *, ch: Any = None) -> list[dict[str, str]]:
        return [{"namespace": ns, "role": "writer"} for s in subs for ns in (rows or {}).get(s, [])]

    monkeypatch.setattr(caller_defaults, "saved_default", _saved)
    monkeypatch.setattr(caller_defaults, "ranked_grants", _ranked)


def _default(token: AuthInfo) -> str:
    from server.identity.app.caller_home import default_namespace

    return asyncio.run(default_namespace(token, ORG))


def test_a_saved_pick_the_subject_writes_but_the_creator_only_reads_is_no_default(
    _default_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The gate decides a delegated token on its creator: alice only reads `docs`.
    monkeypatch.setitem(GRANTS, ("sa:cli", "docs"), 2)
    _saved_and_ranked(monkeypatch, "docs")
    assert _default(TOKENS["delegated, creator only reads the first claim"]) == "team"


def test_a_saved_restricted_pick_the_token_cannot_write_leaves_the_writable_claim(
    _default_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    # alice writes `secret`, but the token's own subject holds no grant there.
    monkeypatch.setitem(GRANTS, ("alice", "secret"), 2)
    _saved_and_ranked(monkeypatch, "secret")
    assert _default(TOKENS["delegated restricted claim"]) == "team"


def test_a_wildcard_delegated_default_ignores_the_subjects_own_grants(
    _default_inputs, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(GRANTS, ("bot", "botspace"), 3)
    _saved_and_ranked(monkeypatch, "", {"bot": ["botspace"], "alice": ["team"]})
    token = _token("bot", ("*",), creator="alice")
    assert _default(token) == "team"
    asyncio.run(authz.check_authorized(token, Role.WRITER, ORG, "team"))


def test_a_flat_service_account_reads_its_claims_through_its_creator(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.auth.service_accounts as service_accounts

    calls: list[str] = []

    async def _counted(org: str, subject: str, **_: Any) -> str | None:
        calls.append(subject)
        return "alice"

    monkeypatch.setattr(service_accounts, "inheriting_creator", _counted)
    monkeypatch.setitem(GRANTS, ("sa:inherits", "left"), 0)
    token = _token("sa:inherits", ("team", "solo", "left"), is_service_account=True)
    assert asyncio.run(readable_named_claims(token, ORG)) == ["team"]
    assert set(calls) == {"sa:inherits"}
