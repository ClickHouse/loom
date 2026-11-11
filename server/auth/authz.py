"""Role + namespace ACL authorization.

Hierarchy: VIEWER < WRITER < ADMIN < ORG_ADMIN < PLATFORM_ADMIN.

The JWT carries a global role for the namespaces it covers. A per-namespace
grant in `loom.namespace_grants` is additive: it elevates the principal above
their JWT role for one (org, namespace) but never demotes (the shared-datapack
"invite as Maintainer" workflow).

A namespace classified `restricted` is the one exception to
"additive": every default that widens a read — the org-readable code rule,
`visibility=public`, a wildcard claim, a service account inheriting its
creator's grant, a platform role — is switched off there, and a read needs
BOTH a token that names the namespace AND a live direct grant.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable, Iterable
from enum import Enum
from typing import TYPE_CHECKING

from server.auth import sharing
from server.auth.directory import ClassificationUnavailable, directory
from server.auth.fail_closed import (
    marks_unverified,
    note_answered_unverified,
    note_unreadable,
    unreadable,
)
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT
from server.core.principals import USER, principal_id

# Re-export from operations.base so there's one canonical Role definition.
from server.operations.base import GRANT_ROLES, Role, grant_rank, rank_role

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo
    from server.core.grant_cache import GrantMap
    from server.operations.base import Operation

log = logging.getLogger(__name__)


# The roles a namespace grant can hold. Org- and platform-wide roles are never
# grantable per namespace: an unknown grant role maps to no role at all.
_ROLE_NAME_TO_ENUM: dict[str, Role] = {name: Role[name.upper()] for name in GRANT_ROLES}


def grant_role(name: str | None) -> Role | None:
    """The Role a grant row's (or an `ns_roles` entry's) role name confers;
    None for anything that is not a grant role."""
    return _ROLE_NAME_TO_ENUM.get((name or "").strip().lower())


class _Unknown(Enum):
    """Not read yet: a caller passes no grant it already holds."""

    GRANT = "unknown"


_UNKNOWN_GRANT = _Unknown.GRANT


def may_act_in_org(auth: AuthInfo, org: str) -> bool:
    """Whether `auth` may act in `org` at all: a token of that org, an
    org-wide (`*`) token, or a platform admin. The one cross-org rule — the
    transports and every handler's same-org check ask this; a restricted
    namespace still refuses every other org (`_check_restricted`)."""
    return auth.org in ("*", org) or auth.has_role(Role.PLATFORM_ADMIN)


_ACTED_FOR_REVOKED = "the subject this request acts for is revoked or deprovisioned"
_ACTED_FOR_NO_BOT = "an sa: subject is acted for only while it names an active bot or its agent"


class ActedForRefused(PermissionError):
    """The subject a trusted caller acts for is refused (revoked, deprovisioned, or an
    `sa:` subject naming no active bot): a refusal of the subject, not of the caller's
    access to an index, so a transport adds no index reason to it."""


async def check_acted_for(auth: AuthInfo | None, org: str | None) -> None:
    """Refuse a trusted caller acting for a revoked or SCIM-deactivated subject under any
    spelling it holds, or for an `sa:` subject that is not an active bot or one of its
    agents: a token's own subject, whose grants are the token's and end with it however
    it ends (an expiry, a revoke by jti, a cutoff on its maker), none of which its grants
    record; docs/architecture/security.md, Impersonation."""
    from server.auth import machines, revocation
    from server.auth.gateway import is_end_user
    from server.core import grant_cache

    # An end user's token names the user, whose revocation verify checked.
    if auth is None or not auth.impersonated_by or is_end_user(auth):
        return
    spellings: set[str] = {auth.subject}
    for scope_org in {org or auth.org, auth.org} - {"", "*"}:
        if may_act_in_org(auth, scope_org):  # an org it cannot act in is refused after this
            spellings.update((await grant_cache.grant_map(scope_org, auth.subject)).held)
    folded = auth.subject.lower()  # bot names are lower-case; grants are read case-folded
    service_account = folded.startswith("sa:")
    bot = folded.removeprefix("sa:").partition(":")[0] if service_account else ""
    if service_account and not bot:
        raise ActedForRefused(_ACTED_FOR_NO_BOT)
    if bot:
        spellings.add(machines.subject(bot))  # a revoked bot refuses its agents, as their mint does
    if any(revocation.revoked_outright_any_case(s) for s in spellings):
        raise ActedForRefused(_ACTED_FOR_REVOKED)
    for email in {s.lower() for s in spellings if "@" in s}:
        if await _remembered(("scim", email), _scim_deactivated):
            raise ActedForRefused(_ACTED_FOR_REVOKED)
    if bot and await _remembered(("bot", bot), _bot_ended):
        raise ActedForRefused(_ACTED_FOR_NO_BOT)


# (kind, key) -> (read at, ended), kept grant_cache_ttl_seconds: a SCIM deactivation, or
# a bot created or disabled, by another process reaches this one within that, and one
# written here drops its entry (`forget_ended`); emptied past _ENDED_MAX.
_ENDED: dict[tuple[str, str], tuple[float, bool]] = {}
_forgets = 0  # forgets so far: a read that one overtook is not kept
_ENDED_MAX = 10_000  # not a setting: a memory bound on a memo the TTL already ages


def forget_ended(kind: str, key: str) -> None:
    """Drop the memo of a SCIM user ("scim", email) or bot ("bot", name) this process
    just wrote, so its own write takes effect here at once."""
    global _forgets
    _ENDED.pop((kind, key.lower()), None)
    _forgets += 1


async def _remembered(key: tuple[str, str], read: Callable[[str], Awaitable[bool]]) -> bool:
    from server.settings import settings

    now = time.monotonic()
    hit = _ENDED.get(key)
    if hit is not None and now - hit[0] < settings.grant_cache_ttl_seconds:
        return hit[1]
    forgets = _forgets
    ended = await read(key[1])
    if _forgets != forgets:
        return ended  # a write here overtook the read: the next check reads afresh
    if len(_ENDED) >= _ENDED_MAX:
        _ENDED.clear()
    _ENDED[key] = (now, ended)
    return ended


async def _scim_deactivated(email: str) -> bool:
    from server.auth import login, scim
    from server.core.errors import BackendUnavailableError, LoomServerError

    try:
        return await scim.deactivated(email)
    except login.SignInUnavailable as e:
        if isinstance(e.__cause__, LoomServerError):
            raise e.__cause__ from None  # its own code and Retry-After, as _bot_ended does
        raise BackendUnavailableError("cannot read whether SCIM deactivated the subject") from e


async def _bot_ended(name: str) -> bool:
    """Whether no active bot holds `name`: it is disabled, or no bot ever held it."""
    from server.auth import machines
    from server.core.errors import BackendUnavailableError, LoomServerError

    try:
        return await machines.get(name) is None
    except LoomServerError:
        raise
    except Exception as e:  # an unread bot row refuses, as the grant read does
        raise BackendUnavailableError("cannot read whether the bot is active") from e


@marks_unverified
async def check_authorized(
    auth: AuthInfo | None,
    required_role: Role,
    org: str | None = None,
    namespace: str | None = None,
    *,
    deployment_scoped: bool = False,
    row_scoped: bool = False,
    known_grant: Role | None | _Unknown = _UNKNOWN_GRANT,
    grants: GrantMap | None = None,
) -> None:
    """Raise PermissionError if `auth` lacks `required_role` or access to `(org,
    namespace)`. Consults `loom.namespace_grants` as a fallback when the JWT role alone
    is insufficient. No-op when `auth is None` (LOOM_AUTH_REQUIRED=false mode).
    `deployment_scoped`: the op reads the deployment's own facts and no namespace, so a
    scoped token is gated on its best per-namespace role instead of being refused as
    IDENTITY_INCOMPLETE. `row_scoped`: the op is addressed by an id and its handler
    checks the row's own namespace, so a scoped token passes here on its best role in
    `org` and is decided there. `known_grant` (the grant the gate would read, the
    creator's for a delegated token) and `grants` (the subject's cached grant map, for
    the restricted check): what a caller that already read them hands in."""
    from server.auth.jwt import AuthInfo  # circular import sidestep

    if auth is None:
        return

    if not isinstance(auth, AuthInfo):  # defensive
        raise PermissionError("invalid auth principal")

    # A cell serves only the orgs it holds: the routes outside the op registry
    # authorize here (the op gate checked before it got this far).
    from server.auth import tenancy

    await tenancy.ensure_held(org, auth)

    if auth.creator:
        await _check_creator(auth, required_role, org, namespace)

    # A scoped token carries its authority in ns_roles keyed by
    # (org, namespace). A request that omits either leaves it unaddressable:
    # answer 400 IDENTITY_INCOMPLETE, not a misleading 403.
    if auth.has_ns_roles() and (org is None or namespace is None):
        _check_scoped_without_target(auth, required_role, org, deployment_scoped, row_scoped)
        return

    # A collection is reached only through grants (server/auth/sharing.py):
    # no claim, org role or rule below applies to it.
    if org is not None and namespace is not None and sharing.is_collection(namespace):
        await sharing.check(auth, required_role, org, namespace)
        return

    if auth.creator and org is not None and namespace is not None:
        await _check_delegated(auth, required_role, org, namespace, known_grant, grants)
        return

    # A restricted namespace is decided here and nowhere
    # below — none of the widening rules that follow may apply to it.
    if org is not None and namespace is not None and await may_be_restricted(org, namespace):
        await _check_restricted(auth, required_role, org, namespace, grants)
        return

    # Code namespaces are org-shared reference data, not anyone's memory. When
    # the deployment says so (LOOM_CODE_ORG_READABLE / per-entry org_readable)
    # every authenticated principal of that org reads it with the token it
    # already holds — scoped or flat, no grant, no re-mint. Writes (refresh,
    # watermark repair) stay grant-driven. Otherwise code namespaces follow
    # the ordinary rules: a grant (joined) or a token naming the namespace.
    # The directory row's visibility never widens reads — `join_policy = open`
    # means joinable in one click, not readable unjoined.
    if (
        required_role.value <= Role.VIEWER.value
        and org is not None
        and namespace is not None
        and (auth.org == org or auth.has_role(Role.PLATFORM_ADMIN))
        # A service's end user is no member of the org's code.
        and not principal_id(auth.subject, USER)
        and await is_org_readable_code_namespace(org, namespace)
    ):
        return

    # A claim on a namespace where the subject holds a grant row is capped at
    # that grant, so a revoked or lowered grant ends every older token's claim.
    cap = None if org is None or namespace is None else await claim_cap(auth, org, namespace)
    capped = cap is not None

    # Scoped token (carries an `ns_roles` map): the per-namespace role is
    # AUTHORITATIVE and was capped to the minter's live grant at mint, so it
    # cannot over-grant (this closes the flat-role write-escalation). For a
    # namespace-scoped op we authorize purely against effective_role and do
    # NOT fall through to grant lookup — the token was deliberately scoped
    # (and possibly downgraded), so it is its own ceiling. Non-namespace
    # (org-level) ops fall through to the flat-role check below.
    if auth.has_ns_roles() and org is not None and namespace is not None:
        role = _scoped_role(auth, required_role, org, namespace, cap=cap)
        if role is not None and role.value >= required_role.value:
            return
        if not _code_namespace_read(required_role, org, namespace):
            raise _scoped_refusal(required_role, org, namespace, role, capped=capped)

    # Reads check the readable set; writes (WRITER+) check the narrower
    # writable set, so a token can read an inherited parent namespace
    # without being able to write into it (absent a writer grant below).
    is_write = required_role.value >= Role.WRITER.value
    if org is not None and namespace is not None:
        ns_covered_by_jwt = (
            auth.can_write(org, namespace) if is_write else auth.can_access(org, namespace)
        )
        if cap is not None:
            live = rank_role(cap)
            ns_covered_by_jwt = ns_covered_by_jwt and live is not None and live >= required_role
    else:
        ns_covered_by_jwt = False

    # A token acts in its own org unless it is org-wide. This runs before the
    # grant lookup: grants are keyed on (org, namespace, subject), and a subject
    # string is not unique across orgs (an org's admin mints any subject), so a
    # grant held in another org by the same name must not admit this token.
    # MCP refuses the same case up front (ORG_MISMATCH).
    if org is not None and not may_act_in_org(auth, org):
        raise PermissionError(
            f"principal {auth.subject!r} (org {auth.org!r}) cannot act in org {org!r}"
        )

    if auth.has_role(required_role) and (ns_covered_by_jwt or namespace is None):
        return

    # Grants are per-namespace; with no (org, namespace) to bind one to,
    # surface the original failure.
    if org is None or namespace is None:
        if not auth.has_role(required_role):
            raise PermissionError(
                f"role {required_role.name} required; principal has {[r.name for r in auth.roles]}"
            )
        raise PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")

    # A caller that already read the principal's
    # grants this request (a listing over many namespaces) hands the answer
    # in — `None` included — instead of paying one grants read per namespace.
    grant_role = await _grant_role(auth, org, namespace, cap, known_grant)
    if grant_role is not None and grant_role.value >= required_role.value:
        return

    # Be specific so the failing gate is visible to debug clients.
    if capped:
        raise _refused(_capped_message(org, namespace, required_role), org, namespace)
    if not ns_covered_by_jwt:
        raise _refused(
            f"principal {auth.subject!r} cannot access {org}/{namespace} "
            f"(JWT namespaces + no grant)",
            org,
            namespace,
        )
    raise _refused(
        f"role {required_role.name} required; "
        f"principal has {[r.name for r in auth.roles]} "
        f"(no per-namespace grant elevates)",
        org,
        namespace,
    )


def _check_scoped_without_target(
    auth: AuthInfo, required_role: Role, org: str | None, deployment_scoped: bool, row_scoped: bool
) -> None:
    """A scoped token on an op that names no (org, namespace): admitted on its
    best role anywhere for a row- or deployment-scoped op, else 400."""
    # Both admissions gate on the token's best role anywhere; the org the op
    # reads must still be the token's own.
    if (row_scoped or deployment_scoped) and org is not None and auth.org not in ("*", org):
        raise PermissionError(f"scoped token of org {auth.org!r} cannot act in org {org!r}")
    if deployment_scoped or row_scoped:
        best = auth.best_ns_role()
        if best is not None and best.value >= required_role.value:
            return
        raise PermissionError(
            f"scoped token: {required_role.name} required for a "
            f"{'row' if row_scoped else 'deployment'}-scoped op; "
            f"token's best namespace role is {best.name if best else 'none'}"
        )
    from server.core.errors import IdentityIncompleteError

    raise IdentityIncompleteError(
        "scoped token requires org and namespace in the request body "
        "(the token's authority is per-namespace and cannot be applied "
        "without knowing which namespace the request targets)"
    )


async def _grant_role(
    auth: AuthInfo, org: str, namespace: str, cap: int | None, known_grant: Role | None | _Unknown
) -> Role | None:
    """The grant the subject holds there. Where its claim is capped, the cap's
    read decides for the subject itself: it covers every spelling consistently,
    and no second read of its own grants may loosen it. A service account with
    none live there still reads through its creator, as the listings do."""
    if cap is not None:
        role = rank_role(cap)
        if role is None:
            from server.auth.service_accounts import inheriting_creator

            creator = await inheriting_creator(org, auth.subject)
            if creator is not None:
                role = await grant_role_for(org, namespace, creator)
        return role
    if known_grant is not _UNKNOWN_GRANT:
        return known_grant
    return await _lookup_grant(org, namespace, auth.subject)


async def _check_creator(
    auth: AuthInfo, required_role: Role, org: str | None, namespace: str | None
) -> None:
    """A delegated token stops when its creator's account is disabled. An op
    with no namespace has no grant to recheck, so a write there needs the
    creator to hold that role on some namespace of the org."""
    from server.auth.delegation import creator_account_active, creator_best_grant

    account_org = org or auth.org
    if not may_act_in_org(auth, account_org):
        raise PermissionError(
            f"principal {auth.subject!r} (org {auth.org!r}) cannot act in org {account_org!r}"
        )
    if not await creator_account_active(account_org, auth.creator):
        raise PermissionError(f"the account that created this token is not active in {account_org}")
    if namespace is None and required_role.value >= Role.WRITER.value:
        best = await creator_best_grant(account_org, auth.creator)
        if best is None or best.value < required_role.value:
            raise PermissionError(
                f"role {required_role.name} required; the account that created this token "
                f"holds {best.name if best else 'no grant'} in {account_org}"
            )


async def _check_delegated(
    auth: AuthInfo,
    required_role: Role,
    org: str,
    namespace: str,
    known_grant: Role | None | _Unknown = _UNKNOWN_GRANT,
    grants: GrantMap | None = None,
) -> None:
    """The namespace gate for a delegated token (it carries `creator`). Its
    own claim is the ceiling and its creator's live grant must also reach the
    role, so the token never outlasts or outgrows what the creator holds now.
    The subject's own grants do not widen it. A restricted namespace also
    needs its two-factor check; an org-readable code namespace is read by
    anyone in the org; on any other code namespace the creator's grant alone
    admits a read (the join is the read). `_check_creator` has checked the
    org. `known_grant` is the creator's grant and `grants` the subject's map, when
    the caller already read them."""
    from server.auth.delegation import ceiling_role

    refusal = _refused(
        f"principal {auth.subject!r} cannot access {org}/{namespace}", org, namespace
    )
    is_read = required_role.value <= Role.VIEWER.value
    if await may_be_restricted(org, namespace):
        await _check_restricted(auth, required_role, org, namespace, grants)
    elif is_read and auth.org == org and await is_org_readable_code_namespace(org, namespace):
        return
    elif not (is_read and _is_code_namespace(org, namespace)):
        ceiling = ceiling_role(auth, org, namespace, write=not is_read)
        if ceiling is None or ceiling.value < required_role.value:
            raise refusal
    grant = (
        known_grant
        if known_grant is not _UNKNOWN_GRANT
        else await _creator_grant(org, namespace, auth.creator)
    )
    if grant is None or grant.value < required_role.value:
        raise refusal


async def _creator_grant(org: str, namespace: str, creator: str) -> Role | None:
    """`grant_role_for` for a delegated token's creator, read once per request."""
    from server.auth.context import creator_grant_memo

    memo = creator_grant_memo.get()
    key = (org, namespace, creator)
    if memo is not None and key in memo:
        return memo[key]
    role = await grant_role_for(org, namespace, creator)
    if memo is not None and not unreadable():  # a failed read is retried, not remembered
        memo[key] = role
    return role


async def may_be_restricted(org: str, namespace: str) -> bool:
    """A directory that cannot be read denies: the caller
    treats True as "run the restricted check", and the restricted check
    itself re-raises the outage as a refusal."""
    try:
        return await directory().is_restricted(org, namespace)
    except ClassificationUnavailable:
        note_unreadable()
        return True


def names_namespace(auth: AuthInfo, org: str, namespace: str) -> bool:
    """True when the token names `namespace` literally — in its `namespaces`
    claim or its `ns_roles` map. A wildcard (`*`) never counts: a restricted
    namespace must be asked for by name at mint time."""
    if auth.org not in ("*", org):
        return False
    if namespace in auth.namespaces:
        return True
    return any(ns == namespace for ns, _role in auth.ns_roles)


def _scoped_role(
    auth: AuthInfo, required_role: Role, org: str, namespace: str, *, cap: int | None
) -> Role | None:
    """The role a scoped token holds on (org, namespace) for this op: its own,
    capped at the subject's live grant where it holds one. None for a write
    outside its `ns_write` claim (inherited by a delegated child)."""
    if (
        required_role.value >= Role.WRITER.value
        and auth.write_namespaces
        and not auth.can_write(org, namespace)
    ):
        return None
    role = auth.effective_role(org, namespace)
    if role is not None and cap is not None:
        live = rank_role(cap)
        role = None if live is None else min(role, live)
    return role


def _code_namespace_read(required_role: Role, org: str, namespace: str) -> bool:
    """A read of a configured code namespace, which a scoped token leaves to
    the grant check. On a repository the join IS the read: a code index is
    read-only reference data a maintainer admits people to by grant, so a
    reader grant (the subject's own, or the creator's for a service account)
    widens reads there and nothing else. Without it, every agent token minted
    before a person joined a repository would need a re-mint to follow them."""
    return required_role.value <= Role.VIEWER.value and _is_code_namespace(org, namespace)


def _scoped_refusal(
    required_role: Role, org: str, namespace: str, role: Role | None, *, capped: bool
) -> PermissionError:
    if capped:
        return _refused(_capped_message(org, namespace, required_role), org, namespace)
    return _refused(
        f"scoped token: {required_role.name} required on {org}/{namespace}; "
        f"token grants {role.name if role else 'no access'} there",
        org,
        namespace,
    )


def _capped_message(org: str, namespace: str, required_role: Role) -> str:
    return (
        f"this token's claim on {org}/{namespace} goes no further than the grant held "
        f"there, which does not allow {required_role.name}"
    )


async def claim_cap(auth: AuthInfo, org: str, namespace: str) -> int | None:
    """The live grant rank this token's claim on `namespace` is capped at (0:
    none live), where the token names it and its subject holds a grant row
    there of any status. None where the subject never held one (an operator's
    `loom mint-token`): the claim stands as minted."""
    from server.core.grant_cache import grant_map

    if not names_namespace(auth, org, namespace):
        return None
    return (await grant_map(org, auth.subject)).rank(namespace)


async def live_claims(auth: AuthInfo, org: str, namespaces: Iterable[str]) -> list[str]:
    """The named `namespaces` (no `*`), in order, that the claim still reaches:
    all but those capped at a grant that is no longer live."""
    return [ns for ns in namespaces if ns and ns != "*" and await claim_cap(auth, org, ns) != 0]


@marks_unverified
async def _check_restricted(
    auth: AuthInfo,
    required_role: Role,
    org: str,
    namespace: str,
    grants: GrantMap | None = None,
) -> None:
    """The two-factor gate for restricted namespaces. Raises PermissionError unless:

    1. the token belongs to `org` (no cross-org wildcard);
    2. the token names `namespace` (claim or ns_roles; no wildcard), and a
       write also needs it in the token's `ns_write` set when it has one;
    3. the token's own role there — the scoped `ns_roles` entry, else the
       flat `roles` — reaches `required_role`;
    4. a live DIRECT grant for the token's subject reaches `required_role`
       (no service-account → creator inheritance).

    The refusal text is the generic "cannot access" line so a caller learns
    nothing about whether the namespace exists or why it was refused.
    `LOOM_RESTRICTED_SERVE=false` refuses everyone (kill switch: the plane
    keeps ingesting, nobody reads). `grants`: the subject's cached grant map,
    when the caller already holds it, in place of a read of its own.
    """
    from server.settings import settings

    refusal = PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")
    if not settings.restricted_serve:
        raise refusal
    try:
        await directory().is_restricted(org, namespace)
    except ClassificationUnavailable as e:
        note_unreadable()
        raise refusal from e
    if auth.org != org:
        raise refusal
    if not names_namespace(auth, org, namespace):
        raise refusal
    if required_role.value >= Role.WRITER.value and not auth.can_write(org, namespace):
        raise refusal
    own = auth.effective_role(org, namespace) if auth.has_ns_roles() else None
    if own is None and not auth.has_ns_roles():
        own = max(auth.roles, default=None)
    if own is None or own.value < required_role.value:
        raise refusal
    if grants is not None:
        grant = rank_role(grants.rank(namespace) or 0)
    else:
        grant = await grant_role_for(org, namespace, auth.subject)
    if grant is None or grant.value < required_role.value:
        raise refusal


async def restricted_allows(
    auth: AuthInfo,
    required_role: Role,
    org: str,
    namespace: str,
    *,
    grants: GrantMap | None = None,
    hides_answer: bool = True,
) -> bool:
    """`_check_restricted` as a predicate. A refusal made because a read it
    needed failed marks the request the server's, as `row_in_scope` does
    (`hides_answer` there)."""
    try:
        await _check_restricted(auth, required_role, org, namespace, grants)
    except PermissionError as e:
        if hides_answer and getattr(e, "unverified", False):  # the server's failure
            note_answered_unverified()
        return False
    return True


async def grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
    """Granted role for (org, namespace, subject), or None. The subject matches
    whatever its case, as the cached grant maps match it (`grant_cache`).
    Pending grants are excluded: an unapproved writer-request must not elevate."""
    from server.auth.aliases import lowered, subject_aliases
    from server.core.ch import get_client

    try:
        # The person's login handle and their verified email are one principal —
        # a grant under either spelling, in any case, counts; the highest wins
        subjects = lowered(await subject_aliases(org, subject))
        row = await get_client().query_one(
            """
            SELECT role FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND lower(subject) IN {subs:Array(String)} AND {live}
            ORDER BY {rank} DESC
            LIMIT 1
            """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
            params={"org": org, "ns": namespace, "subs": subjects},
        )
    except Exception as e:  # noqa: BLE001 — CH glitch treated as no grant (closed)
        # Don't fail auth open on a CH glitch — treat as no grant.
        log.warning("namespace_grants lookup failed for %s/%s/%s: %s", org, namespace, subject, e)
        note_unreadable()
        return None
    if row is None:
        return None
    return grant_role(row.get("role"))


_JOIN_HINT = (
    " This is a repository namespace: join it from the dashboard's Repositories page, "
    "or call code.repos to see what you can join."
)


def _refused(message: str, org: str, namespace: str) -> PermissionError:
    """The refusal for a namespace-bound check. On a repository namespace the refusal
    also says how to get in: the join IS the read, so "no access" on a code namespace is
    an invitation the caller could not see. The kind comes from the in-memory repo
    config this path already consults (`_is_code_namespace`) — no directory read, no CH;
    the scoped-resolve path stays CH-free. The error class, and so the AUTH_INSUFFICIENT
    code the transport maps it to, is unchanged."""
    if _is_code_namespace(org, namespace):
        message += _JOIN_HINT
    return PermissionError(message)


def _is_code_namespace(org: str, namespace: str) -> bool:
    """True for a configured code-plane entry (no I/O)."""
    return directory().code_entry(org, namespace) is not None


async def is_org_readable_code_namespace(org: str, namespace: str) -> bool:
    """True for a configured code-plane entry the deployment marks
    org_readable (LOOM_CODE_ORG_READABLE or the entry's own flag). No I/O.

    The directory row's `visibility = public` does not widen reads: "open"
    means joinable, and reads need a grant or a token naming the namespace
    like every other row."""
    cfg = directory().code_entry(org, namespace)
    if cfg is None:
        return False
    # Belt and braces: a restricted entry is never org-readable, whatever
    # the flag says (the gate above already returned).
    return cfg.classification != "restricted" and cfg.org_readable


async def _lookup_grant(org: str, namespace: str, subject: str) -> Role | None:
    """Return the granted role for (org, namespace, subject), or None.

    A service account with no grant of its own reads through its creator's
    when its row says it inherits (`service_accounts.inheriting_creator`) —
    the rule every namespace listing applies, so the gate admits exactly
    what the listings include.
    """
    role = await grant_role_for(org, namespace, subject)
    if role is None:
        from server.auth.service_accounts import inheriting_creator

        creator = await inheriting_creator(org, subject)
        if creator is not None:
            role = await grant_role_for(org, namespace, creator)
    return role


class CachedGate:
    """`check_authorized` for one token in one org, for the listings and the
    default that must agree with the gate: the gate itself, handed the grant
    it falls back to from the cached grant maps (`server.core.grant_cache`)
    instead of a fresh read per namespace. Each map is read at most once per
    gate. (A claim capped at a revoked grant still has the gate read a
    service account's creator for itself.)

    `provisioned`: a personal namespace `gates.authorize` gives this person,
    with an admin grant, before the gate runs (`caller_home`); counted as
    that grant."""

    def __init__(self, auth: AuthInfo, org: str, provisioned: str = "") -> None:
        self.auth = auth
        self.org = org
        self.provisioned = provisioned
        self._maps: dict[str, GrantMap] = {}
        self._inherited: str | None | _Unknown = _UNKNOWN_GRANT

    async def grants(self, subject: str | None = None) -> GrantMap:
        """The grant map of `subject` (the token's own by default)."""
        from server.core.grant_cache import grant_map

        subject = subject or self.auth.subject
        if subject not in self._maps:
            self._maps[subject] = await grant_map(self.org, subject)
        return self._maps[subject]

    async def admits(self, namespace: str, required_role: Role) -> bool:
        known: Role | None
        if namespace and namespace == self.provisioned:
            known = Role.ADMIN
        elif self.auth.creator:
            known = rank_role((await self.grants(self.auth.creator)).rank(namespace) or 0)
        elif self.auth.has_ns_roles() and not _code_namespace_read(
            required_role, self.org, namespace
        ):
            known = None  # the gate never asks: a scoped token's role is its own
        else:
            known = await self._grant_role(namespace)
        try:
            await check_authorized(
                self.auth,
                required_role,
                self.org,
                namespace,
                known_grant=known,
                grants=await self.grants(),
            )
        except PermissionError:
            return False
        return True

    async def _grant_role(self, namespace: str) -> Role | None:
        """`_lookup_grant`: the subject's live grant, else the inheriting creator's."""
        from server.auth.service_accounts import inheriting_creator

        role = rank_role((await self.grants()).rank(namespace) or 0)
        if role is not None:
            return role
        if self._inherited is _UNKNOWN_GRANT:
            self._inherited = await inheriting_creator(self.org, self.auth.subject)
        if not self._inherited:
            return None
        return rank_role((await self.grants(self._inherited)).rank(namespace) or 0)


def scope_flags(op: Operation) -> dict[str, bool]:
    """The op's two scoped-token gate flags, as `check_authorized` keywords —
    both transports pass them the same way."""
    return {"deployment_scoped": op.deployment_scoped, "row_scoped": op.row_scoped}


async def row_in_scope(
    org: str, namespace: str, required_role: Role = Role.VIEWER, *, hides_answer: bool = True
) -> bool:
    """Second gate for ops addressed by an org-wide id (memory_id, …), asked
    once the handler has the row. True when the current principal may act on
    (org, namespace) at `required_role`, or with no auth context; False on
    any refusal, so the caller hides the row (`found=False`): a 403 would
    confirm the id exists. A refusal that could not be verified marks the
    request the server's (`fail_closed`); `hides_answer=False` when this
    refusal does not decide what the caller asked for."""
    verdict = await row_reach(org, namespace, required_role)
    if verdict is None and hides_answer:  # the server's failure
        note_answered_unverified()
    return bool(verdict)


async def row_reach(org: str, namespace: str, required_role: Role = Role.VIEWER) -> bool | None:
    """`row_in_scope`'s answer, None for a refusal that could not be verified."""
    from server.auth.context import current_auth
    from server.core.errors import IdentityIncompleteError

    auth = current_auth()
    if auth is None:
        return True
    try:
        await check_authorized(auth, required_role=required_role, org=org, namespace=namespace)
    except PermissionError as e:
        return None if getattr(e, "unverified", False) else False
    except IdentityIncompleteError:
        return False
    return True


__all__ = [
    "GRANT_ROLES",
    "CachedGate",
    "Role",
    "check_authorized",
    "claim_cap",
    "grant_rank",
    "grant_role",
    "grant_role_for",
    "live_claims",
    "may_be_restricted",
    "names_namespace",
    "restricted_allows",
    "row_in_scope",
    "row_reach",
    "scope_flags",
]
