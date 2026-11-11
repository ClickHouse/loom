"""Where a call that names no namespace lands for a caller: its home.

The MCP transport fills it into unaddressed calls (`server/transport/mcp_identity.py`)
and the token mint names it for the token it makes (`me_service_accounts.py`), so both ask this
module and cannot disagree. The inputs are the token's claims, the home subject's saved
dashboard pick and live grants (`caller_defaults`), and the namespace kinds. Every
candidate is checked against what the gate would admit for this token, worked out once
per call (`_gate_view`).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from server.auth.context import delegated_reach, named_claims, readable_named_claims
from server.auth.personal_namespace import derive_personal_slug
from server.auth.sharing import is_collection
from server.core.errors import BackendUnavailableError, WrongCellError
from server.identity.app import caller_defaults
from server.operations.base import Role

if TYPE_CHECKING:
    from server.auth.authz import CachedGate

log = logging.getLogger(__name__)

# (namespace, role) -> whether the gate admits this token there at that role.
Admits = Callable[[str, Role], Awaitable[bool]]


def home_namespace(accessible: list[str], subject: str, fallback: str = "") -> str:
    """The namespace an unaddressed call addresses first: the one named after
    the caller (`alice` for subject `alice` or `alice@…`); else `fallback`
    when it is in the list (the caller's saved default); else the first of the
    list. Used both for unaddressed searches and for the default a write lands
    in, so a teammate's namespace that sorts first never outranks the
    caller's own."""
    if not accessible:
        return ""
    login = _login(subject)
    if login:
        for ns in accessible:
            if str(ns).lower() == login:
                return ns
    if fallback and fallback in accessible:
        return fallback
    return accessible[0]


def _login(subject: str) -> str:
    return str(subject or "").strip().lower().split("@", 1)[0]


def _owner_first(names: list[str], subject: str) -> list[str]:
    """`names` in order, the one named after `subject` moved to the front."""
    login = _login(subject)
    return sorted(names, key=lambda ns: str(ns).lower() != login) if login else list(names)


def home_subject(auth: Any) -> str:
    """Whose home an unaddressed call follows: a delegated token's signed
    `creator` (the person the gate checks its grants against), else its own
    subject. Bots, operator-minted and self-signup tokens carry no creator."""
    if auth is None:
        return ""
    return str(getattr(auth, "creator", "") or getattr(auth, "subject", "") or "")


def is_memory_home(kinds: dict[str, str], namespace: str) -> bool:
    """Whether an UNADDRESSED call may land in `namespace` — a memory-kind
    namespace only. A tracker/QA, code or any other plane is
    reached by naming it; never as a default. Unknown kind (not in the
    directory) counts as memory, as the directory's own default does."""
    return str(kinds.get(namespace, "memory") or "memory") == "memory"


async def _kinds_or_empty(org: str) -> dict[str, str]:
    try:
        from server.identity.app.namespace_kinds import kind_map

        return dict(await kind_map(org))
    except Exception:  # noqa: BLE001 — fail-open: unknown kinds count as memory
        return {}


def caller_org(auth: Any) -> str | None:
    """The org a caller acts in by default: its token's (the self-signup org for
    a `*` token), or the single-user org with no auth; None when no caller can
    be identified."""
    from server.settings import settings

    if auth is not None:
        if not auth.subject:
            return None
        if auth.org and auth.org != "*":
            return str(auth.org)
        return settings.self_signup_org
    if settings.effective_identity_provider() == "single-user":
        return settings.self_signup_org
    return None


async def default_namespace(auth: Any, org: str) -> str:
    """The namespace a call from `auth` that names none lands in
    (`default_and_readable`)."""
    return (await default_and_readable(auth, org))[0]


async def default_and_readable(auth: Any, org: str) -> tuple[str, list[str]]:
    """The namespace a call from `auth` that names none lands in, and, when
    there is none, the named claims the gate lets it read (the names such a
    call must pick from; [] once a default is found, as nobody lists them).

    Single-user mode serves exactly ONE namespace (LOOM_DEFAULT_NAMESPACE): the
    SPA pins its identity to it and the boot-time bootstrap provisions only it,
    so the default is that name, unless the token claims one. Any other mode
    takes the first memory namespace the gate lets the token WRITE, else the
    first it lets it READ, else "" (its calls must name a namespace), looking
    at a list token's recorded `saves_to` and its list alone; for any other
    token at the namespace a delegated `*` token was made to save to, the home
    subject's saved switcher pick, then the token's claims, then the home's
    grants, ranked (`_pick_default`).

    Fails closed: a cached grant map ClickHouse cannot read reaches the
    transport as BackendUnavailableError (a retryable 503); any other failure
    is logged and answers no default and nothing readable."""
    from server.auth.authz import CachedGate
    from server.settings import settings

    home = home_subject(auth) or "you"
    try:
        if settings.effective_identity_provider() == "single-user":
            claimed = await readable_named_claims(auth, org)
            return (claimed[0] if claimed else settings.default_namespace), claimed
        gate = None
        if auth is not None:
            gate = CachedGate(auth, org, provisioned=await _provisioned(auth, org))
        admits, extra = await _gate_view(auth, org, home, gate)
        named = [] if auth is None else named_claims(auth)
        default = await _pick_default(auth, org, home, named, admits, extra)
        return default, [] if default else await readable_named_claims(auth, org, gate)
    except BackendUnavailableError:
        raise
    except WrongCellError:
        # The token's org is placed in another cell: it has no default here, and a call
        # in it answers WRONG_CELL before reaching this (`mcp_call._another_cells_org`).
        return "", []
    except Exception:  # a bug, logged: no default, so the call names its namespace
        log.warning("no default namespace for %s in %s", home, org, exc_info=True)
        return "", []


async def _pick_default(
    auth: Any, org: str, home: str, claimed: list[str], admits: Admits, extra: list[str]
) -> str:
    """The first memory namespace `admits` lets the token WRITE, else the
    first it may READ, else "". A token minted with a `saves_to` claim asks
    only that namespace, then its own list in order: its owner's saved pick
    never moves it, and the gate admits it nowhere else. Any other token, in
    order: the namespace a delegated `*` token was made to save to
    (`minted_home`), the home's saved switcher pick, the token's claims (the
    home's own first), the home's grants ranked by role tier then recency (its
    own first), then `extra`. Every step asks the same `admits`, so no step
    can pick what another would refuse. A collection is shared reading: a
    delegated token never saves there unasked, so it is no WRITER step's pick,
    and at the READ step one counts only while the token may not write it (as
    at mint, `token_saves_to`). A person's own sign-in may work in one."""
    from server.core.ch import get_client

    kinds = await _kinds_or_empty(org)
    delegated = bool(auth and auth.creator)

    def memory(names: list[str]) -> list[str]:
        return [ns for ns in dict.fromkeys(names) if ns and is_memory_home(kinds, ns)]

    def writable_homes(names: list[str]) -> list[str]:
        return [ns for ns in names if not (delegated and is_collection(ns))]

    async def read_home(names: list[str]) -> str:
        for ns in names:
            if not await admits(ns, Role.VIEWER):
                continue
            if delegated and is_collection(ns) and await admits(ns, Role.WRITER):
                continue  # a default there would take its writes
            return ns
        return ""

    recorded = auth.saves_to if auth else ""
    if recorded:
        # A token with the claim lists no `*` (`delegate`, `token_saves_to`).
        own = memory([recorded, *auth.namespaces])
        return await _first_admitted(writable_homes(own), admits, Role.WRITER) or await read_home(
            own
        )
    ch = get_client()
    saved = await caller_defaults.saved_default(org, home, ch=ch)
    early = memory([minted_home(auth), saved, *_owner_first(claimed, home)])
    pick = await _first_admitted(writable_homes(early), admits, Role.WRITER)
    if pick:
        return pick
    rows = await caller_defaults.ranked_grants(org, await _ranked_subjects(auth, org, home), ch=ch)
    late = memory(_owner_first([str(r.get("namespace") or "") for r in rows or []], home) + extra)
    return await _first_admitted(writable_homes(late), admits, Role.WRITER) or await read_home(
        early + late
    )


def minted_home(auth: Any) -> str:
    """Where a delegated `*` token was made to save: the namespace its claim
    names before `*` (as `service_accounts.create` mints a token with a home,
    and `admin/token.mint` when asked for that order). "" for any other token:
    a bare `*` follows its owner's saved pick, and a list token saves where its
    `saves_to` claim says (`_pick_default`). A `*` token that may no longer
    write its home falls back through the saved pick; a list token with the
    claim does not. Rolling upgrades: docs/architecture/rolling-upgrades.md,
    rule 35."""
    if auth is None or not auth.creator:  # auth off, or not a delegated token
        return ""
    return star_home(list(auth.namespaces))


def star_home(namespaces: list[str]) -> str:
    """The namespace a `*` claim names before `*`, "" for none: where such a token
    saves when it may write there, also once a narrower parent turns the `*` into a
    list. A mint refuses a collection there (`token_saves_to.saves_to_target`)."""
    return namespaces[0] if "*" in namespaces and namespaces[0] != "*" else ""


async def _first_admitted(names: list[str], admits: Admits, role: Role) -> str:
    for ns in names:
        if await admits(ns, role):
            return ns
    return ""


async def _ranked_subjects(auth: Any, org: str, home: str) -> list[str]:
    """Whose grants rank the default: the home's spellings, plus the creator
    a service account reads through (`service_accounts.inheriting_creator`)."""
    from server.auth.aliases import subject_aliases
    from server.auth.service_accounts import inheriting_creator

    subjects = await subject_aliases(org, home)
    creator = None if getattr(auth, "creator", "") else await inheriting_creator(org, home)
    if creator:
        subjects += await subject_aliases(org, creator)
    return subjects


async def _gate_view(
    auth: Any, org: str, home: str, gate: CachedGate | None
) -> tuple[Admits, list[str]]:
    """What the gate admits this token on, worked out once for the call
    (`gate`, which every step asks), and the namespaces to fall back to: a
    delegated token's readable reach (`delegated_reach`), else its personal
    namespace. No token (auth off): the gate admits anything."""
    if auth is None or gate is None:

        async def _anything(ns: str, role: Role) -> bool:
            return True

        return _anything, [derive_personal_slug(home)]
    if not auth.creator:
        return gate.admits, [gate.provisioned or derive_personal_slug(auth.subject)]
    writable, readable = await delegated_reach(auth, org, gate)

    async def _reached(ns: str, role: Role) -> bool:
        return ns in (writable if role.value >= Role.WRITER.value else readable)

    return _reached, readable


async def _provisioned(auth: Any, org: str) -> str:
    """The personal namespace `gates.authorize` gives this person, with an admin grant,
    before the gate runs (`ensure_personal_namespace_cached`): a person of this org
    with no live grant gets the first of its names nobody owns and nothing uses
    (`personal_candidates`; a person who owns one, their grant there revoked, is
    given nothing), and only while this process has not already resolved the person,
    which it does once (`provisioning_pending`); "" otherwise. A delegated token
    counts it only when its creator is that person, whose grants the gate reads."""
    if auth.is_service_account or auth.org != org:
        return ""
    if auth.creator and auth.creator.lower() != auth.subject.lower():
        return ""
    from server.auth.directory import directory
    from server.auth.personal_namespace import (
        namespace_owner,
        personal_candidates,
        provisioning_pending,
    )
    from server.core.ch import get_client
    from server.core.grant_cache import grant_map

    if not provisioning_pending(auth.org, auth.subject):
        return ""
    if any((await grant_map(org, auth.subject)).ranks.values()):
        return ""
    for name in personal_candidates(auth.subject):
        owner = await namespace_owner(get_client(), org, name)
        if owner is None and not await directory().in_use(org, name):
            return name
        if owner == auth.subject:
            return ""  # theirs, with the grant revoked: nothing is given
    return ""


__all__ = [
    "caller_org",
    "default_and_readable",
    "default_namespace",
    "home_namespace",
    "home_subject",
    "is_memory_home",
    "minted_home",
    "star_home",
]
