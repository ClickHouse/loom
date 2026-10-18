"""The MCP default and the readable list agree with the gate, over random cases.

For every kind of token (flat human, scoped, flat service account that
inherits, delegated with named claims, delegated `*`), random claims, grants
for the subject and the creator, restricted flags, a saved pick and an
active or disabled creator: the default an unaddressed call lands in is one
`authz.check_authorized` lets the token WRITE whenever it may write some
memory namespace, else one it may READ, else ""; and the readable list is
exactly the named claims the gate lets it read, configured code namespaces
(org-readable, private, restricted) included. Both sides run against the
same fakes."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.auth import authz
from server.auth.jwt import AuthInfo
from server.operations.base import Role

ORG = "o"
# Configured code namespaces: (classification, org_readable).
CODE = {
    "code-open": ("standard", True),
    "code-private": ("standard", False),
    "code-secret": ("restricted", False),
}
NAMESPACES = ("carol", "team", "docs", "secret", "board", "qa-plane", *CODE)
PERSON, BOT = "carol", "sa:bot"
CASES = 5000
SEED = 118
KINDS = ("flat human", "scoped", "inheriting sa", "delegated named", "delegated *")
_ROLES = (Role.VIEWER, Role.WRITER, Role.ADMIN)
_ROLE_NAMES = {1: "viewer", 2: "writer", 3: "admin"}


@dataclass(frozen=True)
class _CodeEntry:
    namespace: str
    classification: str
    org_readable: bool


@dataclass
class World:
    """One random case: grant ranks per (subject, namespace) (0: no longer
    live), the restricted set, the namespace kinds, the person's saved pick,
    whether the bot inherits and whether the creator's account is active."""

    grants: dict[tuple[str, str], int] = field(default_factory=dict)
    restricted: frozenset[str] = frozenset()
    kinds: dict[str, str] = field(default_factory=dict)
    saved: str = ""
    inherits: bool = False
    active: bool = True


WORLD = World()


@pytest.fixture()
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.aliases as aliases
    import server.auth.delegation as delegation
    import server.auth.service_accounts as service_accounts
    import server.code.domain.index.repos as repos
    import server.core.ch as chmod
    import server.core.grant_cache as grant_cache
    import server.identity.app.caller_defaults as caller_defaults
    import server.identity.app.namespace_classification as nc
    import server.identity.app.namespace_kinds as nk
    from server.settings import settings

    async def _read(org: str, subject: str) -> grant_cache.GrantMap:
        ranks = {ns: r for (s, ns), r in WORLD.grants.items() if s == subject}
        return grant_cache.GrantMap(ranks=ranks)

    async def _grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
        return {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}.get(
            WORLD.grants.get((subject, namespace), 0)
        )

    async def _inheriting(org: str, subject: str, **_: Any) -> str | None:
        return PERSON if subject == BOT and WORLD.inherits else None

    async def _active(org: str, creator: str) -> bool:
        return WORLD.active

    async def _restricted_set(org: str) -> frozenset[str]:
        return WORLD.restricted

    async def _kinds(org: str) -> dict[str, str]:
        return dict(WORLD.kinds)

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    async def _saved(org: str, subject: str, *, ch: Any = None) -> str:
        return WORLD.saved if subject == PERSON else ""

    async def _ranked(org: str, subs: list[str], *, ch: Any = None) -> list[dict[str, str]]:
        live = [(r, ns) for (s, ns), r in WORLD.grants.items() if s in subs and r > 0]
        return [{"namespace": ns, "role": _ROLE_NAMES[r]} for r, ns in sorted(live, reverse=True)]

    def _code_entry(org: str, namespace: str) -> _CodeEntry | None:
        if org != ORG or namespace not in CODE:
            return None
        return _CodeEntry(namespace, *CODE[namespace])

    monkeypatch.setattr(repos, "repo_for_namespace", _code_entry)
    monkeypatch.setattr(grant_cache, "_read", _read)
    monkeypatch.setattr(authz, "grant_role_for", _grant_role_for)
    monkeypatch.setattr(service_accounts, "inheriting_creator", _inheriting)
    monkeypatch.setattr(delegation, "creator_account_active", _active)
    monkeypatch.setattr(nc, "restricted_set", _restricted_set)
    monkeypatch.setattr(nk, "kind_map", _kinds)
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(caller_defaults, "saved_default", _saved)
    monkeypatch.setattr(caller_defaults, "ranked_grants", _ranked)
    monkeypatch.setattr(chmod, "get_client", lambda: None)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")


def _random_world(rng: random.Random) -> World:
    grants = {}
    for subject in (PERSON, BOT):
        for ns in NAMESPACES:
            rank = rng.choice((None, None, 0, 1, 2, 3))
            if rank is not None:
                grants[(subject, ns)] = rank
    return World(
        grants=grants,
        restricted=frozenset(ns for ns in NAMESPACES if rng.random() < 0.25),
        kinds={
            "qa-plane": "qa",
            **dict.fromkeys(CODE, "code"),
            **({"docs": "qa"} if rng.random() < 0.2 else {}),
        },
        saved=rng.choice(("", *NAMESPACES)),
        inherits=rng.random() < 0.5,
        active=rng.random() < 0.85,
    )


def _random_token(rng: random.Random, kind: str) -> AuthInfo:
    claims = tuple(rng.sample(NAMESPACES, rng.randint(0, 4)))
    roles = (rng.choice(_ROLES),)
    if kind == "flat human":
        return AuthInfo(
            subject=PERSON, org=ORG, namespaces=claims, roles=roles, is_service_account=False
        )
    if kind == "scoped":
        ns_roles = tuple((ns, rng.choice(("viewer", "writer", "admin"))) for ns in claims)
        return AuthInfo(
            subject=PERSON,
            org=ORG,
            namespaces=claims,
            roles=(),
            is_service_account=False,
            ns_roles=ns_roles,
        )
    if kind == "inheriting sa":
        return AuthInfo(
            subject=BOT, org=ORG, namespaces=claims, roles=roles, is_service_account=True
        )
    if kind == "delegated *":
        claims = ("*", *claims[: rng.randint(0, 2)])
    return AuthInfo(
        subject=BOT,
        org=ORG,
        namespaces=claims,
        roles=roles,
        is_service_account=True,
        creator=PERSON,
    )


async def _admits(token: AuthInfo, role: Role, namespace: str) -> bool:
    try:
        await authz.check_authorized(token, role, ORG, namespace)
    except PermissionError:
        return False
    return True


def _provision(token: AuthInfo) -> None:
    """What `gates.authorize` does before the gate: a person with no live
    grant is given a personal namespace (`ensure_personal_namespace_cached`)."""
    from server.auth.personal_namespace import derive_personal_slug

    live = any(r > 0 for (s, _ns), r in WORLD.grants.items() if s == token.subject)
    if not token.is_service_account and not live:
        WORLD.grants[(token.subject, derive_personal_slug(token.subject))] = 3


async def _violation(token: AuthInfo) -> str:
    """What the case gets wrong, "" when the default and the list follow the gate."""
    from server.identity.app.caller_home import default_and_readable, is_memory_home

    default, readable = await default_and_readable(token, ORG)
    _provision(token)
    named = list(dict.fromkeys(ns for ns in token.namespaces if ns != "*"))
    admitted = [ns for ns in named if await _admits(token, Role.VIEWER, ns)]
    if readable != admitted:
        return f"readable {readable} != admitted {admitted}"
    if not default:
        return ""
    memory = [ns for ns in NAMESPACES if is_memory_home(WORLD.kinds, ns)]
    writes_some = [ns for ns in memory if await _admits(token, Role.WRITER, ns)]
    role = Role.WRITER if writes_some else Role.VIEWER
    if not is_memory_home(WORLD.kinds, default) or not await _admits(token, role, default):
        return f"default {default!r} not {role.name}-admitted (writable {writes_some})"
    return ""


async def _run_cases() -> list[str]:
    import server.core.grant_cache as grant_cache

    global WORLD
    rng = random.Random(SEED)
    failures = []
    for i in range(CASES):
        WORLD = _random_world(rng)
        token = _random_token(rng, KINDS[i % len(KINDS)])
        grant_cache.clear()
        problem = await _violation(token)
        if problem:
            failures.append(f"case {i} {KINDS[i % len(KINDS)]}: {problem}; {token} {WORLD}")
    return failures


def test_the_default_and_the_readable_list_follow_the_gate(_fakes: None) -> None:
    failures = asyncio.run(_run_cases())
    assert not failures, f"{len(failures)} of {CASES}:\n" + "\n".join(failures[:5])


def _case(world: World, token: AuthInfo) -> tuple[str, list[str]]:
    import server.core.grant_cache as grant_cache
    from server.identity.app.caller_home import default_and_readable

    global WORLD
    WORLD = world
    grant_cache.clear()
    return asyncio.run(default_and_readable(token, ORG))


def _delegated(*namespaces: str) -> AuthInfo:
    return AuthInfo(
        subject=BOT,
        org=ORG,
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=True,
        creator=PERSON,
    )


def test_a_wildcard_token_leaves_a_saved_restricted_pick_once_its_own_grant_is_revoked(
    _fakes: None,
) -> None:
    # carol writes `secret` and saved it; the token's own direct grant there is revoked.
    world = World(
        grants={(PERSON, "secret"): 2, (PERSON, "team"): 2, (BOT, "secret"): 0},
        restricted=frozenset({"secret"}),
        saved="secret",
    )
    assert _case(world, _delegated("*", "secret")) == ("team", [])


def test_a_delegated_token_reads_an_org_readable_code_namespace_without_a_creator_grant(
    _fakes: None,
) -> None:
    world = World(grants={(PERSON, "team"): 2}, kinds=dict.fromkeys(CODE, "code"))
    token = _delegated("code-open", "code-private", "team")
    assert _case(world, token) == ("team", ["code-open", "team"])
    assert asyncio.run(_admits(token, Role.VIEWER, "code-open"))
