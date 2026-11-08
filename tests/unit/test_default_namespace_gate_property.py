"""The MCP default and the readable list agree with the gate, over random cases.

For every kind of token (flat human, scoped, flat service account that
inherits, delegated service account with named or `*` claims, delegated
human), random claims (`*` included), write claims, roles up to platform
admin, the token's org (its own, `*`, another), grants for the subject and
the creator (the person's under a spelling in another case), restricted
flags, the restricted kill switch, an unreadable directory, a saved pick, an
active or disabled creator and the owner of the person's personal namespace:
the default an unaddressed call lands in is the first candidate
`authz.check_authorized` lets the token WRITE, else the first it may READ,
else "" when it may read none of them. The candidates, in order: a delegated
token's claim named before `*` (where it was made to save), the saved pick and
the claims (the home's own first), then the home's live grants ranked and the
fallback (the personal namespace, or a delegated token's
reach). With no default, the readable list is exactly the named claims the
gate lets it read. Both sides run against the same fakes."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass, field
from typing import Any

import pytest

from server.auth import authz
from server.auth.directory import ClassificationUnavailable
from server.auth.jwt import AuthInfo
from server.auth.personal_namespace import derive_personal_slug, personal_candidates
from server.identity.app.directory import IdentityDirectory
from server.operations.base import Role

ORG = "o"
# Configured code namespaces: (classification, org_readable).
CODE = {
    "code-open": ("standard", True),
    "code-private": ("standard", False),
    "code-secret": ("restricted", False),
}
PERSON, BOT, OTHER = "carol", "sa:bot", "dave"
SLUG = derive_personal_slug(PERSON)
# The name provisioning takes when someone else owns SLUG.
FALLBACK = personal_candidates(PERSON)[1]
NAMESPACES = ("carol", "team", "docs", "secret", "board", "qa-plane", SLUG, *CODE)
CASES = 5000
SEED = 119
KINDS = (
    "flat human",
    "scoped",
    "inheriting sa",
    "delegated named",
    "delegated *",
    "delegated human",
)
_ROLES = (Role.VIEWER, Role.WRITER, Role.ADMIN, Role.ORG_ADMIN, Role.PLATFORM_ADMIN)
_ROLE_NAMES = {1: "viewer", 2: "writer", 3: "admin"}
_ROLE_BY_RANK = {1: Role.VIEWER, 2: Role.WRITER, 3: Role.ADMIN}


@dataclass(frozen=True)
class _CodeEntry:
    namespace: str
    classification: str
    org_readable: bool


@dataclass
class World:
    """One random case: grant ranks per (subject, namespace) (0: no longer
    live), the restricted set, the namespace kinds, the person's saved pick,
    whether the bot inherits, whether the creator's account is active, who
    owns the personal namespace, the kill switch and a directory outage."""

    grants: dict[tuple[str, str], int] = field(default_factory=dict)
    restricted: frozenset[str] = frozenset()
    kinds: dict[str, str] = field(default_factory=dict)
    saved: str = ""
    inherits: bool = False
    active: bool = True
    owner: str = ""
    serve: bool = True
    directory_down: bool = False


WORLD = World()


def _same(a: str, b: str) -> bool:
    return a.lower() == b.lower()


def _rank(subject: str, namespace: str) -> int | None:
    ranks = [r for (s, ns), r in WORLD.grants.items() if ns == namespace and _same(s, subject)]
    return max(ranks) if ranks else None


def _ranked_rows(subjects: list[str]) -> list[dict[str, str]]:
    """Live grants of `subjects`, by role tier, then name (the fake's recency)."""
    live = {
        (r, ns)
        for (s, ns), r in WORLD.grants.items()
        if r > 0 and any(_same(s, sub) for sub in subjects)
    }
    return [{"namespace": ns, "role": _ROLE_NAMES[r]} for r, ns in sorted(live, reverse=True)]


@pytest.fixture()
def _fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    import server.auth.aliases as aliases
    import server.auth.context as context
    import server.auth.delegation as delegation
    import server.auth.personal_namespace as personal_namespace
    import server.auth.service_accounts as service_accounts
    import server.code.domain.index.repos as repos
    import server.core.ch as chmod
    import server.core.grant_cache as grant_cache
    import server.identity.app.caller_defaults as caller_defaults
    import server.identity.app.namespace_classification as nc
    import server.identity.app.namespace_kinds as nk
    from server.settings import settings

    async def _read(org: str, subject: str) -> grant_cache.GrantMap:
        ranks = {ns: r for (s, ns), r in WORLD.grants.items() if _same(s, subject)}
        return grant_cache.GrantMap(ranks=ranks)

    async def _grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
        return _ROLE_BY_RANK.get(_rank(subject, namespace) or 0)

    async def _inheriting(org: str, subject: str, **_: Any) -> str | None:
        return PERSON if subject == BOT and WORLD.inherits else None

    async def _active(org: str, creator: str) -> bool:
        return WORLD.active

    async def _restricted_set(org: str) -> frozenset[str]:
        if WORLD.directory_down:
            raise ClassificationUnavailable("down")
        return WORLD.restricted

    async def _kinds(org: str) -> dict[str, str]:
        return dict(WORLD.kinds)

    async def _aliases(org: str, subject: str, *, ch: Any = None) -> list[str]:
        return [subject]

    async def _saved(org: str, subject: str, *, ch: Any = None) -> str:
        return WORLD.saved if subject == PERSON else ""

    async def _ranked(org: str, subs: list[str], *, ch: Any = None) -> list[dict[str, str]]:
        return _ranked_rows(subs)

    async def _owner(ch: Any, org: str, namespace: str) -> str | None:
        return (WORLD.owner or None) if namespace == SLUG else None

    async def _in_use(self: Any, org: str, namespace: str, **_: Any) -> bool:
        return _in_use_by_grant(namespace)

    async def _granted(org: str, subjects: list[str]) -> set[str]:
        return {str(r["namespace"]) for r in _ranked_rows(subjects)}

    def _code_entry(org: str, namespace: str) -> _CodeEntry | None:
        if org != ORG or namespace not in CODE:
            return None
        return _CodeEntry(namespace, *CODE[namespace])

    monkeypatch.setattr(repos, "config_entry", _code_entry)
    monkeypatch.setattr(grant_cache, "_read", _read)
    monkeypatch.setattr(authz, "grant_role_for", _grant_role_for)
    monkeypatch.setattr(service_accounts, "inheriting_creator", _inheriting)
    monkeypatch.setattr(delegation, "creator_account_active", _active)
    monkeypatch.setattr(nc, "restricted_set", _restricted_set)
    monkeypatch.setattr(nk, "kind_map", _kinds)
    monkeypatch.setattr(aliases, "subject_aliases", _aliases)
    monkeypatch.setattr(caller_defaults, "saved_default", _saved)
    monkeypatch.setattr(caller_defaults, "ranked_grants", _ranked)
    monkeypatch.setattr(personal_namespace, "namespace_owner", _owner)
    monkeypatch.setattr(IdentityDirectory, "in_use", _in_use)
    monkeypatch.setattr(context, "_granted_namespaces", _granted)
    monkeypatch.setattr(chmod, "get_client", lambda: None)
    monkeypatch.setattr(settings, "identity_provider", "trust-network")
    monkeypatch.setattr(settings, "restricted_serve", True)


def _random_world(rng: random.Random) -> World:
    # The person's grants are sometimes written under a spelling in another case.
    spelling = rng.choice((PERSON, PERSON, PERSON.capitalize()))
    grants = {}
    for subject in (spelling, BOT):
        for ns in NAMESPACES:
            rank = rng.choice((None, None, 0, 1, 2, 3))
            if rank is not None:
                grants[(subject, ns)] = rank
    if rng.random() < 0.3:
        # A person with no live grant is the one given a personal namespace.
        grants = {key: rank for key, rank in grants.items() if key[0] == BOT or not rank}
    owner = rng.choice(("", "", PERSON, OTHER))
    # A namespace nobody owns has no directory row to restrict it.
    restricted = {ns for ns in NAMESPACES if rng.random() < 0.25 and (owner or ns != SLUG)}
    return World(
        grants=grants,
        restricted=frozenset(restricted),
        kinds={
            "qa-plane": "qa",
            **dict.fromkeys(CODE, "code"),
            **({"docs": "qa"} if rng.random() < 0.2 else {}),
        },
        saved=rng.choice(("", *NAMESPACES)),
        inherits=rng.random() < 0.5,
        active=rng.random() < 0.85,
        owner=owner,
        serve=rng.random() < 0.9,
        directory_down=rng.random() < 0.05,
    )


def _random_token(rng: random.Random, kind: str) -> AuthInfo:
    claims = rng.sample(NAMESPACES, rng.randint(0, 4))
    if kind == "delegated *" or rng.random() < 0.15:
        claims = ["*", *claims[: rng.randint(0, 2)]]
    named = [ns for ns in claims if ns != "*"]
    writes = rng.sample(named, rng.randint(0, len(named))) if rng.random() < 0.3 else []
    token: dict[str, Any] = {
        "org": rng.choice((ORG, ORG, ORG, "*", "other")),
        "namespaces": tuple(claims),
        "roles": tuple(rng.sample(_ROLES, rng.randint(1, 2))),
        "write_namespaces": tuple(writes),
        "subject": BOT,
        "is_service_account": True,
    }
    if kind in ("flat human", "scoped", "delegated human"):
        token.update(subject=PERSON, is_service_account=False)
    if kind == "scoped":
        token["roles"] = ()
        token["ns_roles"] = tuple((ns, rng.choice(("viewer", "writer", "admin"))) for ns in named)
    if kind.startswith("delegated"):
        token["creator"] = PERSON
    return AuthInfo(**token)


async def _admits(token: AuthInfo, role: Role, namespace: str) -> bool:
    try:
        await authz.check_authorized(token, role, ORG, namespace)
    except PermissionError:
        return False
    return True


def _provisioned(token: AuthInfo) -> str:
    """What `gates.authorize` gives before the gate: a person of this org with
    no live grant, under any spelling, gets the personal namespace when nobody
    owns or uses it yet, else its fallback name when someone else does
    (`ensure_personal_namespace_cached`); "" otherwise."""
    live = any(r > 0 for (s, _ns), r in WORLD.grants.items() if _same(s, token.subject))
    if token.is_service_account or token.org != ORG or live:
        return ""
    if not WORLD.owner:  # unowned, but a live grant there puts it in use: the fallback
        return FALLBACK if _in_use_by_grant(SLUG) else SLUG
    return FALLBACK if WORLD.owner == OTHER and token.subject == PERSON else ""


def _in_use_by_grant(namespace: str) -> bool:
    """A live grant on a name with no row puts it in use (`IdentityDirectory.in_use`)."""
    return any(r > 0 for (_s, ns), r in WORLD.grants.items() if ns == namespace)


def _provision(token: AuthInfo) -> None:
    name = _provisioned(token)
    if name:
        WORLD.grants[(token.subject, name)] = 3
        WORLD.owner = WORLD.owner or PERSON


def _owner_first(names: list[str], subject: str) -> list[str]:
    login = subject.lower().split("@", 1)[0]
    return sorted(names, key=lambda ns: ns.lower() != login)


def _candidates(token: AuthInfo) -> tuple[list[str], list[str]]:
    """The names a default may come from, in order, as two passes: (the
    minted home, the saved pick and the claims, the ranked grants and the fallback)."""
    from server.identity.app.caller_home import is_memory_home

    home = token.creator or token.subject
    named = [ns for ns in token.namespaces if ns != "*"]
    subjects = [home]
    if not token.creator and token.subject == BOT and WORLD.inherits:
        subjects.append(PERSON)
    ranked = [str(r["namespace"]) for r in _ranked_rows(subjects)]
    if token.creator:
        reach = sorted(ns for ns in NAMESPACES if (_rank(PERSON, ns) or 0) > 0)
        fallback = named + (reach if "*" in token.namespaces else [])
    else:
        fallback = [_provisioned(token) or derive_personal_slug(token.subject)]
    saved = WORLD.saved if home == PERSON else ""
    star_home = "*" in token.namespaces and token.namespaces[0] != "*"
    minted = named[0] if token.creator and star_home else ""
    early = [minted, saved, *_owner_first(named, home)]
    late = _owner_first(ranked, home) + fallback

    def _memory(names: list[str]) -> list[str]:
        return [ns for ns in dict.fromkeys(names) if ns and is_memory_home(WORLD.kinds, ns)]

    return _memory(early), _memory(late)


async def _first(token: AuthInfo, names: list[str], role: Role) -> str:
    for ns in names:
        if await _admits(token, role, ns):
            return ns
    return ""


async def _expected_default(token: AuthInfo, early: list[str], late: list[str]) -> str:
    return (
        await _first(token, early, Role.WRITER)
        or await _first(token, late, Role.WRITER)
        or await _first(token, list(dict.fromkeys(early + late)), Role.VIEWER)
    )


async def _resolve_violation(token: AuthInfo) -> str:
    """What `resolve_my_namespaces` (the search fan-out) lists that the gate
    refuses, or, for a flat token, leaves out of its claims and live grants."""
    from server.auth import context

    context.set_current_auth(token)
    try:
        listed = await context.resolve_my_namespaces(ORG)
    finally:
        context.set_current_auth(None)
    refused = [ns for ns in listed if not await _admits(token, Role.VIEWER, ns)]
    if refused:
        return f"resolve lists {refused}, which the gate refuses"
    if token.creator or token.ns_roles:
        return ""
    subjects = [token.subject, *([PERSON] if token.subject == BOT and WORLD.inherits else [])]
    reach = [*token.namespaces, *(str(r["namespace"]) for r in _ranked_rows(subjects))]
    missed = [
        ns
        for ns in dict.fromkeys(reach)
        if ns != "*" and ns not in listed and await _admits(token, Role.VIEWER, ns)
    ]
    return f"resolve leaves out {missed}, which the gate admits" if missed else ""


async def _violation(token: AuthInfo) -> str:
    """What the case gets wrong, "" when the default and the lists follow the gate."""
    from server.identity.app.caller_home import default_and_readable

    problem = await _resolve_violation(token)
    if problem:
        return problem
    early, late = _candidates(token)
    default, readable = await default_and_readable(token, ORG)
    _provision(token)
    expected = await _expected_default(token, early, late)
    if default != expected:
        return f"default {default!r}, the gate's first is {expected!r} of {early} + {late}"
    if default:
        return ""
    named = list(dict.fromkeys(ns for ns in token.namespaces if ns != "*"))
    admitted = [ns for ns in named if await _admits(token, Role.VIEWER, ns)]
    if readable != admitted:
        return f"readable {readable} != admitted {admitted}"
    return ""


async def _run_cases() -> list[str]:
    import server.core.grant_cache as grant_cache
    from server.settings import settings

    global WORLD
    rng = random.Random(SEED)
    failures = []
    for i in range(CASES):
        WORLD = _random_world(rng)
        settings.restricted_serve = WORLD.serve
        token = _random_token(rng, KINDS[i % len(KINDS)])
        grant_cache.clear()
        problem = await _violation(token)
        if problem:
            failures.append(f"case {i} {KINDS[i % len(KINDS)]}: {problem}; {token} {WORLD}")
    return failures


def test_the_default_and_the_readable_list_follow_the_gate(_fakes: None) -> None:
    failures = asyncio.run(_run_cases())
    assert not failures, f"{len(failures)} of {CASES}:\n" + "\n".join(failures[:5])


def test_the_property_fails_a_default_that_is_always_empty(
    _fakes: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.identity.app.caller_home as caller_home

    async def _nothing(*args: Any, **kwargs: Any) -> str:
        return ""

    monkeypatch.setattr(caller_home, "_pick_default", _nothing)
    assert len(asyncio.run(_run_cases())) > CASES // 4


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


def _person(*namespaces: str) -> AuthInfo:
    return AuthInfo(
        subject=PERSON,
        org=ORG,
        namespaces=namespaces,
        roles=(Role.WRITER,),
        is_service_account=False,
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
    assert _case(world, token) == ("team", [])
    assert _case(world, _delegated("code-open", "code-private")) == ("", ["code-open"])
    assert asyncio.run(_admits(token, Role.VIEWER, "code-open"))


def test_a_grant_under_another_case_counts(_fakes: None) -> None:
    assert _case(World(grants={("Carol", "team"): 2}), _person()) == ("team", [])


def test_a_revoked_personal_namespace_is_not_the_default(_fakes: None) -> None:
    # carol's only row is her own revoked grant there: provisioning adds nothing.
    world = World(grants={(PERSON, SLUG): 0}, owner=PERSON)
    assert _case(world, _person()) == ("", [])
    # A row nobody's namespace backs: provisioning creates it, with a new grant.
    assert _case(World(grants={(PERSON, SLUG): 0}), _person()) == (SLUG, [])


def test_a_personal_namespace_someone_else_owns_gives_way_to_the_fallback_name(
    _fakes: None,
) -> None:
    assert _case(World(owner=OTHER), _person()) == (FALLBACK, [])
    assert _case(World(), _person()) == (SLUG, [])
