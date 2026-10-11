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
from typing import TYPE_CHECKING, Any

from server.auth.directory import ClassificationUnavailable, directory
from server.core.grant_sql import GRANT_RANK_SQL, LIVE_GRANT

# Re-export from operations.base so there's one canonical Role definition.
from server.operations.base import GRANT_ROLES, Role, grant_rank

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo
    from server.operations.base import Operation

log = logging.getLogger(__name__)


# The roles a namespace grant can hold. Org- and platform-wide roles are never
# grantable per namespace: an unknown grant role maps to no role at all.
_ROLE_NAME_TO_ENUM: dict[str, Role] = {name: Role[name.upper()] for name in GRANT_ROLES}


def grant_role(name: str | None) -> Role | None:
    """The Role a grant row's (or an `ns_roles` entry's) role name confers;
    None for anything that is not a grant role."""
    return _ROLE_NAME_TO_ENUM.get((name or "").strip().lower())


_UNKNOWN_GRANT: Any = object()


def may_act_in_org(auth: AuthInfo, org: str) -> bool:
    """Whether `auth` may act in `org` at all: a token of that org, an
    org-wide (`*`) token, or a platform admin. The one cross-org rule — the
    transports and every handler's same-org check ask this; a restricted
    namespace still refuses every other org (`_check_restricted`)."""
    return auth.org in ("*", org) or auth.has_role(Role.PLATFORM_ADMIN)


async def check_authorized(
    auth: AuthInfo | None,
    required_role: Role,
    org: str | None = None,
    namespace: str | None = None,
    *,
    deployment_scoped: bool = False,
    row_scoped: bool = False,
    known_grant: Role | None | Any = _UNKNOWN_GRANT,
) -> None:
    """Raise PermissionError if `auth` lacks `required_role` or access to `(org,
    namespace)`. Consults `loom.namespace_grants` as a fallback when the JWT role alone
    is insufficient. No-op when `auth is None` (LOOM_AUTH_REQUIRED=false mode).
    `deployment_scoped`: the op reads the deployment's own facts and no namespace, so a
    scoped token is gated on its best per-namespace role instead of being refused as
    IDENTITY_INCOMPLETE. `row_scoped`: the op is addressed by an id and its handler
    checks the row's own namespace, so a scoped token passes here on its best role in
    `org` and is decided there."""
    from server.auth.jwt import AuthInfo  # circular import sidestep

    if auth is None:
        return

    if not isinstance(auth, AuthInfo):  # defensive
        raise PermissionError("invalid auth principal")

    if auth.creator:
        await _check_creator_active(auth, org)

    # A scoped token carries its authority in ns_roles keyed by
    # (org, namespace). A request that omits either leaves it unaddressable:
    # answer 400 IDENTITY_INCOMPLETE, not a misleading 403.
    if auth.has_ns_roles() and (org is None or namespace is None):
        # Both admissions below gate on the token's best role anywhere; the
        # org the op reads must still be the token's own.
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

    if auth.creator and org is not None and namespace is not None:
        await _check_delegated(auth, required_role, org, namespace)
        return

    # A restricted namespace is decided here and nowhere
    # below — none of the widening rules that follow may apply to it.
    if org is not None and namespace is not None and await _is_restricted(org, namespace):
        await _check_restricted(auth, required_role, org, namespace)
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
        and await _is_org_readable_code_namespace(org, namespace)
    ):
        return

    # Scoped token (carries an `ns_roles` map): the per-namespace role is
    # AUTHORITATIVE and was capped to the minter's live grant at mint, so it
    # cannot over-grant (this closes the flat-role write-escalation). For a
    # namespace-scoped op we authorize purely against effective_role and do
    # NOT fall through to grant lookup — the token was deliberately scoped
    # (and possibly downgraded), so it is its own ceiling. Non-namespace
    # (org-level) ops fall through to the flat-role check below.
    if auth.has_ns_roles() and org is not None and namespace is not None:
        eff = auth.effective_role(org, namespace)
        if eff is not None and eff.value >= required_role.value:
            return
        # On a repository the join IS the read. A scoped
        # token's map caps what it writes and which memory it reads; a code
        # index is read-only reference data a maintainer admits people to by
        # grant, so a reader grant — the subject's own, or the creator's for
        # a service account — widens READS of a configured code namespace and
        # nothing else. Without it, every agent token minted before a person
        # joined a repository would need a re-mint to follow them in.
        if not (required_role.value <= Role.VIEWER.value and _is_code_namespace(org, namespace)):
            raise _refused(
                f"scoped token: {required_role.name} required on {org}/{namespace}; "
                f"token grants {eff.name if eff else 'no access'} there",
                org,
                namespace,
            )

    # Reads check the readable set; writes (WRITER+) check the narrower
    # writable set, so a token can read an inherited parent namespace
    # without being able to write into it (absent a writer grant below).
    is_write = required_role.value >= Role.WRITER.value
    if org is not None and namespace is not None:
        ns_covered_by_jwt = (
            auth.can_write(org, namespace) if is_write else auth.can_access(org, namespace)
        )
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
    grant_role = (
        known_grant
        if known_grant is not _UNKNOWN_GRANT
        else await _lookup_grant(org, namespace, auth.subject)
    )
    if grant_role is not None and grant_role.value >= required_role.value:
        return

    # Be specific so the failing gate is visible to debug clients.
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


async def _check_creator_active(auth: AuthInfo, org: str | None) -> None:
    """A delegated token stops when its creator's account is disabled."""
    from server.auth.delegation import creator_account_active

    account_org = org or auth.org
    if not await creator_account_active(account_org, auth.creator):
        raise PermissionError(f"the account that created this token is not active in {account_org}")


async def _check_delegated(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> None:
    """The namespace gate for a delegated token (it carries `creator`). Its
    own claim is the ceiling and its creator's live grant must also reach the
    role, so the token never outlasts or outgrows what the creator holds now.
    The subject's own grants do not widen it. A restricted namespace also
    needs its two-factor check; an org-readable code namespace is read by
    anyone in the org; on any other code namespace the creator's grant alone
    admits a read (the join is the read)."""
    from server.auth.delegation import ceiling_role

    if not may_act_in_org(auth, org):
        raise PermissionError(
            f"principal {auth.subject!r} (org {auth.org!r}) cannot act in org {org!r}"
        )
    refusal = _refused(
        f"principal {auth.subject!r} cannot access {org}/{namespace}", org, namespace
    )
    is_read = required_role.value <= Role.VIEWER.value
    if await _is_restricted(org, namespace):
        await _check_restricted(auth, required_role, org, namespace)
    elif is_read and auth.org == org and await _is_org_readable_code_namespace(org, namespace):
        return
    elif not (is_read and _is_code_namespace(org, namespace)):
        ceiling = ceiling_role(auth, org, namespace, write=not is_read)
        if ceiling is None or ceiling.value < required_role.value:
            raise refusal
    grant = await _grant_role_for(org, namespace, auth.creator)
    if grant is None or grant.value < required_role.value:
        raise refusal


async def _is_restricted(org: str, namespace: str) -> bool:
    """A directory that cannot be read denies: the caller
    treats True as "run the restricted check", and the restricted check
    itself re-raises the outage as a refusal."""
    try:
        return await directory().is_restricted(org, namespace)
    except ClassificationUnavailable:
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


async def _check_restricted(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> None:
    """The two-factor gate for restricted namespaces. Raises PermissionError unless:

    1. the token belongs to `org` (no cross-org wildcard);
    2. the token names `namespace` (claim or ns_roles; no wildcard);
    3. the token's own role there — the scoped `ns_roles` entry, else the
       flat `roles` — reaches `required_role`;
    4. a live DIRECT grant for the token's subject reaches `required_role`
       (no service-account → creator inheritance).

    The refusal text is the generic "cannot access" line so a caller learns
    nothing about whether the namespace exists or why it was refused.
    `LOOM_RESTRICTED_SERVE=false` refuses everyone (kill switch: the plane
    keeps ingesting, nobody reads).
    """
    from server.settings import settings

    refusal = PermissionError(f"principal {auth.subject!r} cannot access {org}/{namespace}")
    if not settings.restricted_serve:
        raise refusal
    try:
        await directory().is_restricted(org, namespace)
    except ClassificationUnavailable as e:
        raise refusal from e
    if auth.org != org:
        raise refusal
    if not names_namespace(auth, org, namespace):
        raise refusal
    own = auth.effective_role(org, namespace) if auth.has_ns_roles() else None
    if own is None and not auth.has_ns_roles():
        own = max(auth.roles, default=None)
    if own is None or own.value < required_role.value:
        raise refusal
    grant = await _grant_role_for(org, namespace, auth.subject)
    if grant is None or grant.value < required_role.value:
        raise refusal


async def restricted_allows(auth: AuthInfo, required_role: Role, org: str, namespace: str) -> bool:
    """`_check_restricted` as a predicate, for the resolvers that build a
    caller's namespace list (server/auth/context.py)."""
    try:
        await _check_restricted(auth, required_role, org, namespace)
    except PermissionError:
        return False
    return True


async def _grant_role_for(org: str, namespace: str, subject: str) -> Role | None:
    """Granted role for exactly (org, namespace, subject), or None. Pending
    grants are excluded: an unapproved writer-request must not elevate."""
    from server.auth.aliases import subject_aliases
    from server.core.ch import get_client

    try:
        # The person's login handle and their email are one
        # principal — a grant under either spelling counts; the highest wins
        subjects = await subject_aliases(org, subject)
        row = await get_client().query_one(
            """
            SELECT role FROM loom.namespace_grants FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND subject IN {subs:Array(String)} AND {live}
            ORDER BY {rank} DESC
            LIMIT 1
            """.replace("{live}", LIVE_GRANT).replace("{rank}", GRANT_RANK_SQL),
            params={"org": org, "ns": namespace, "subs": subjects},
        )
    except Exception as e:  # noqa: BLE001 — CH glitch treated as no grant (closed)
        # Don't fail auth open on a CH glitch — treat as no grant.
        log.warning("namespace_grants lookup failed for %s/%s/%s: %s", org, namespace, subject, e)
        return None
    if row is None:
        return None
    return grant_role(row.get("role"))


_JOIN_HINT = (
    " This is a repository namespace: join it from /me → Repositories, "
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


async def _is_org_readable_code_namespace(org: str, namespace: str) -> bool:
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
    role = await _grant_role_for(org, namespace, subject)
    if role is None:
        from server.auth.service_accounts import inheriting_creator

        creator = await inheriting_creator(org, subject)
        if creator is not None:
            role = await _grant_role_for(org, namespace, creator)
    return role


def scope_flags(op: Operation) -> dict[str, bool]:
    """The op's two scoped-token gate flags, as `check_authorized` keywords —
    both transports pass them the same way."""
    return {"deployment_scoped": op.deployment_scoped, "row_scoped": op.row_scoped}


async def row_in_scope(org: str, namespace: str, required_role: Role = Role.VIEWER) -> bool:
    """Second gate for ops addressed by an org-wide id (memory_id, …): the
    transport could not bind a namespace, so the handler asks after it has
    the row. True when the current principal may act on (org, namespace) at
    `required_role`; True with no auth context (auth disabled); False on any
    refusal so the caller can hide the row (`found=False`) rather than 403 —
    a 403 would confirm the id exists."""
    from server.auth.context import current_auth
    from server.core.errors import IdentityIncompleteError

    auth = current_auth()
    if auth is None:
        return True
    try:
        await check_authorized(auth, required_role=required_role, org=org, namespace=namespace)
    except (PermissionError, IdentityIncompleteError):
        return False
    return True


__all__ = [
    "GRANT_ROLES",
    "Role",
    "check_authorized",
    "grant_rank",
    "grant_role",
    "names_namespace",
    "restricted_allows",
    "row_in_scope",
    "scope_flags",
]
