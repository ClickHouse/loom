"""Where a call that names no namespace lands for a caller: its home.

The MCP transport fills it into unaddressed calls (`server/transport/mcp_identity.py`)
and the Setup page shows it for the token it mints (`me_setup.py`), so both ask this
module and cannot disagree. The inputs are the token's claims, the home subject's saved
dashboard pick and live grants (`caller_defaults`), and the namespace kinds.
"""

from __future__ import annotations

from typing import Any

from server.auth.authz import claim_cap, names_namespace
from server.auth.context import readable_named_claims
from server.auth.personal_namespace import derive_personal_slug
from server.identity.app import caller_defaults
from server.operations.base import Role, grant_rank


def home_namespace(accessible: list[str], subject: str, fallback: str = "") -> str:
    """The namespace an unaddressed call addresses first: the one named after
    the caller (`alice` for subject `alice` or `alice@…`); else `fallback`
    when it is in the list (the caller's saved default); else the first of the
    list. Used both for unaddressed searches and for the default a write lands
    in, so a teammate's namespace that sorts first never outranks the
    caller's own."""
    if not accessible:
        return ""
    login = str(subject or "").strip().lower().split("@", 1)[0]
    if login:
        for ns in accessible:
            if str(ns).lower() == login:
                return ns
    if fallback and fallback in accessible:
        return fallback
    return accessible[0]


def home_subject(auth: Any) -> str:
    """Whose home an unaddressed call follows: a delegated token's signed
    `creator` (the person the gate checks its grants against), else its own
    subject. Bots, operator-minted and self-signup tokens carry no creator."""
    if auth is None:
        return ""
    return str(getattr(auth, "creator", "") or getattr(auth, "subject", "") or "")


def pick_default_namespace(
    claimed: list[str],
    kinds: dict[str, str],
    can_write: Any,
    owner_login: str,
    owner_default: str = "",
) -> str:
    """The default a call with no namespace lands in: a MEMORY-kind namespace
    the token can WRITE, the owner's own first. A service-account token minted
    from the Setup tab claims its owner's namespace plus viewer reads on code
    and QA indexes; ranking grant rows alone would see only the viewer rows
    (the claim carries the writable one) and land an unaddressed memory.set in
    a code index, where it fails the role check. "" when nothing qualifies."""
    memory_ns = [n for n in claimed if str(kinds.get(n, "memory") or "memory") in ("memory", "")]
    writable = [n for n in memory_ns if can_write(n)]
    return home_namespace(writable or memory_ns, owner_login, owner_default)


def is_memory_home(kinds: dict[str, str], namespace: str) -> bool:
    """Whether an UNADDRESSED call may land in `namespace` — a memory-kind
    namespace only. A tracker/QA, code or any other plane is
    reached by naming it; never as a default. Unknown kind (not in the
    directory) counts as memory, as the directory's own default does."""
    return str(kinds.get(namespace, "memory") or "memory") == "memory"


def memory_home_from_grants(rows: list[dict[str, Any]], kinds: dict[str, str], subject: str) -> str:
    """The grant ranking's pick among memory-kind namespaces only (role tier
    then recency, as ranked), the caller's own first, so an admin grant on a
    QA plane never outranks the caller's writer grant on their own namespace
    and draws unaddressed writes into it."""
    ordered = [str(r.get("namespace") or "") for r in rows]
    memory = [n for n in ordered if n and is_memory_home(kinds, n)]
    return home_namespace(memory, subject) if memory else ""


# The grant roles that make a namespace a write default.
_WRITE_ROLES = frozenset({"writer", "admin"})


def _scoped(auth_info: Any) -> bool:
    try:
        return bool(auth_info is not None and auth_info.has_ns_roles())
    except Exception:  # noqa: BLE001 — an odd auth object is not scoped
        return False


def token_can_write(auth_info: Any, org: str, namespace: str) -> bool:
    """Whether THIS token may write `namespace`. A scoped token's `ns_roles` entry is
    authoritative (viewer there is not a write default) inside its `ns_write` claim; a
    token with an explicit write scope (`ns_write`) or a delegated one (`creator`: its
    claim is the ceiling, `delegation.ceiling_role`) answers by its claim; any other
    token writes wherever its grants do, so the grant role decides (the JWT-only
    `can_write` does not see grants)."""
    if auth_info is None:
        return True
    try:
        if _scoped(auth_info):
            role = auth_info.effective_role(org, namespace)
            if role is None or role < Role.WRITER:
                return False
            return not auth_info.write_namespaces or bool(auth_info.can_write(org, namespace))
        if getattr(auth_info, "write_namespaces", None) or getattr(auth_info, "creator", ""):
            return bool(auth_info.can_write(org, namespace))
        return True
    except Exception:  # noqa: BLE001 — an odd auth object writes nowhere
        return False


def saved_pick_ok(
    saved: str,
    grant_role: str,
    kinds: dict[str, str],
    auth_info: Any,
    org: str,
    claimed: list[str],
) -> bool:
    """The dashboard switcher pick is honoured as the default only when it is a
    memory plane, the caller holds a WRITE grant there (a viewer pick is where
    the user was looking, not where writes go), and a scoped token claims it
    (its claim list is authoritative)."""
    if not saved or not is_memory_home(kinds, saved):
        return False
    if _scoped(auth_info) and saved not in claimed:
        return False
    if str(grant_role or "").lower() not in _WRITE_ROLES:
        return False
    return token_can_write(auth_info, org, saved)


def ranked_grant_default(
    rows: list[dict[str, Any]],
    kinds: dict[str, str],
    subject: str,
    auth_info: Any,
    org: str,
    claimed: list[str],
) -> str:
    """The grant ranking's pick. A scoped token ranks only the namespaces it
    claims, in claim order; a delegated token that names its namespaces (no
    `*`) ranks only those, since the gate reaches its creator's grants only
    through the claim. Writable memory namespaces first, the caller's own
    ahead; the read-only ones only when nothing is writable, so a viewer-only
    caller still reads its planes."""
    if _scoped(auth_info):
        order = {n: i for i, n in enumerate(claimed)}
        rows = sorted(
            (r for r in rows if str(r.get("namespace") or "") in order),
            key=lambda r: order[str(r.get("namespace") or "")],
        )
    elif getattr(auth_info, "creator", ""):
        rows = [r for r in rows if auth_info.can_access(org, str(r.get("namespace") or ""))]
    writable = [
        r
        for r in rows
        if str(r.get("role") or "").lower() in _WRITE_ROLES
        and token_can_write(auth_info, org, str(r.get("namespace") or ""))
    ]
    return memory_home_from_grants(writable, kinds, subject) or memory_home_from_grants(
        list(rows), kinds, subject
    )


async def _kinds_or_empty(org: str) -> dict[str, str]:
    try:
        from server.identity.app.namespace_kinds import kind_map

        return dict(await kind_map(org))
    except Exception:  # noqa: BLE001 — fail-open: unknown kinds count as memory
        return {}


async def _restricted_off_limits(auth: Any, org: str) -> frozenset[str]:
    """Restricted namespaces the gate refuses this token on
    (`authz._check_restricted`): all of them for a token of another org (or
    org `*`), else those it does not name. None of them is a default."""
    if auth is None:
        return frozenset()
    try:
        from server.auth.directory import directory

        restricted = await directory().restricted_set(org)
        if auth.org != org:
            return frozenset(restricted)
        return frozenset(ns for ns in restricted if not names_namespace(auth, org, ns))
    except Exception:  # noqa: BLE001 — an unreadable directory: the gate still decides
        return frozenset()


async def claimed_home_default(
    auth_info: Any,
    org: str,
    claimed: list[str],
    kinds: dict[str, str],
    home: str,
    owner_default: str = "",
) -> str:
    """`pick_default_namespace` over live inputs: the token's own write test
    and live claim caps, with the namespace kinds and the home subject's login
    and saved default the caller read. Fail-open to "" (the grant ranking then
    decides)."""
    if not claimed:
        return ""
    try:
        # A claim capped below writer there is not a write home.
        caps = {ns: await claim_cap(auth_info, org, ns) for ns in claimed}

        def _can_write(ns: str) -> bool:
            cap = caps.get(ns)
            if cap is not None and cap < grant_rank("writer"):
                return False
            if _scoped(auth_info):  # a scoped viewer claim is not writable
                return token_can_write(auth_info, org, ns)
            try:
                return bool(auth_info.can_write(org, ns))
            except Exception:  # noqa: BLE001 — an odd auth object writes nowhere
                return False

        return pick_default_namespace(claimed, kinds, _can_write, home, owner_default)
    except Exception:  # noqa: BLE001 — fail open to the grant ranking
        return ""


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
    """The namespace a call from `auth` that names none lands in, and the
    claims it can read (`readable_named_claims`) the default was picked from.

    Single-user mode serves exactly ONE namespace (LOOM_DEFAULT_NAMESPACE): the
    SPA pins its identity to it and the boot-time bootstrap provisions only it,
    so the default is that name, unless the token claims one. Any other mode
    follows the home subject: its saved switcher pick, then the token's own
    writable claim, then its grants, ranked; else the token's first claim or
    the home's personal namespace. "" when a delegated token reaches none of
    them: its calls must name a namespace."""
    from server.settings import settings

    try:
        claimed = await readable_named_claims(auth, org)
    except Exception:  # noqa: BLE001 — defaults fail open; the gate still decides access
        claimed = [n for n in (getattr(auth, "namespaces", None) or ()) if n and n != "*"]
    if settings.effective_identity_provider() == "single-user":
        return (claimed[0] if claimed else settings.default_namespace), claimed
    home = home_subject(auth) or "you"
    try:
        return await _multi_user_default(auth, org, claimed, home), claimed
    except Exception:  # noqa: BLE001 — default namespace falls back to claim or personal slug
        fallback = claimed[0] if claimed else derive_personal_slug(home)
        return _reachable(auth, claimed, fallback), claimed


def _reachable(auth: Any, claimed: list[str], namespace: str) -> str:
    """`namespace`, or "" when a delegated token that names its namespaces
    cannot read it: such a token reads only the claims its creator still
    holds (`claimed`), so a default outside them would only be refused. With
    no default, a call must name its namespace."""
    if not getattr(auth, "creator", "") or "*" in auth.namespaces:
        return namespace
    return namespace if namespace in claimed else ""


async def _multi_user_default(auth: Any, org: str, claimed: list[str], home: str) -> str:
    from server.auth.aliases import subject_aliases
    from server.core.ch import get_client

    ch = get_client()
    subject = str(getattr(auth, "subject", "") or "you")
    subs = await subject_aliases(org, subject, ch=ch)
    if home != subject:
        subs += await subject_aliases(org, home, ch=ch)
    kinds = await _kinds_or_empty(org)  # only a memory plane is a home
    off_limits = await _restricted_off_limits(auth, org)
    saved = await caller_defaults.saved_default(org, home, ch=ch)
    namespace = await _saved_pick(auth, org, saved, subs, kinds, claimed, off_limits, ch)
    if not namespace and auth is not None:
        # The token's own writable memory-kind claim, owner first, beats a
        # viewer grant on an index.
        namespace = await claimed_home_default(auth, org, claimed, kinds, home, saved)
    if not namespace:
        # Rank grants by role tier first, recency second: recency alone ranked
        # a later viewer grant above the user's own admin namespace.
        rows = await caller_defaults.ranked_grants(org, subs, ch=ch)
        rows = [r for r in rows or [] if str(r.get("namespace") or "") not in off_limits]
        namespace = ranked_grant_default(rows, kinds, home, auth, org, claimed)
    namespace = namespace or (claimed[0] if claimed else derive_personal_slug(home))
    if not is_memory_home(kinds, namespace):  # never a shared plane by default
        namespace = derive_personal_slug(home)
    return _reachable(auth, claimed, namespace)


async def _saved_pick(
    auth: Any,
    org: str,
    saved: str,
    subs: list[str],
    kinds: dict[str, str],
    claimed: list[str],
    off_limits: frozenset[str],
    ch: Any,
) -> str:
    """The home subject's dashboard switcher pick, when it may be the default:
    a memory plane (a QA/code pick is where the user was LOOKING), not a
    restricted namespace the token does not name, and a write grant there now
    (a stale pick whose grant was revoked must not keep coming back)."""
    if not saved or saved in off_limits or not is_memory_home(kinds, saved):
        return ""
    role = await caller_defaults.live_role_on(org, saved, subs, ch=ch)
    return saved if saved_pick_ok(saved, role, kinds, auth, org, claimed) else ""


__all__ = [
    "caller_org",
    "claimed_home_default",
    "default_namespace",
    "home_namespace",
    "home_subject",
    "is_memory_home",
    "memory_home_from_grants",
    "pick_default_namespace",
    "ranked_grant_default",
    "saved_pick_ok",
    "token_can_write",
]
