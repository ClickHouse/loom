"""Per-request auth context for handlers.

A ContextVar carries the caller's identity instead of threading it through
every handler signature: set once in the transport, read by the handlers that
need it. In dev mode (`LOOM_AUTH_REQUIRED=false`) it stays None and handlers
fall back to an explicit `subject` in the input body.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING

from server.auth.directory import ClassificationUnavailable, directory
from server.core.ch import set_quota_key

if TYPE_CHECKING:
    from server.auth.fanout import Fanout
    from server.auth.jwt import AuthInfo
    from server.operations.base import Role

current_auth_var: ContextVar[AuthInfo | None] = ContextVar(
    "current_auth",
    default=None,
)


def current_auth() -> AuthInfo | None:
    return current_auth_var.get()


def current_caller() -> tuple[str, str | None]:
    """(subject, org) of the current principal; auth-off gives ("", None)."""
    auth = current_auth()
    if auth is None:
        return "", None
    return auth.subject or "", auth.org


def quota_key_for(auth: AuthInfo | None) -> str:
    """The ClickHouse quota key of a verified caller, `<org>:<subject>`; '' without one."""
    if auth is None:
        return ""
    return f"{auth.org}:{auth.subject}"


def set_current_auth(auth: AuthInfo | None) -> None:
    current_auth_var.set(auth)
    fanout_memo.set(None)
    creator_grant_memo.set({} if getattr(auth, "creator", "") else None)
    # every ClickHouse query of this request draws the caller's own quota bucket
    set_quota_key(quota_key_for(auth))


# The fan-out the current principal resolved (`server.auth.fanout`), kept for
# the rest of its request: MCP resolves it to address an unaddressed search,
# and the search handler reads it again. Binding a principal clears it.
fanout_memo: ContextVar[Fanout | None] = ContextVar("loom_fanout", default=None)

# A delegated token's creator grants, (org, namespace, creator) -> role, read
# once per request by `authz._check_delegated`. Binding a principal resets it.
creator_grant_memo: ContextVar[dict[tuple[str, str, str], Role | None] | None] = ContextVar(
    "loom_creator_grants", default=None
)


async def resolve_my_namespaces(org: str, subject: str | None = None) -> list[str]:
    """Namespaces the caller may access in `org`.

    Two modes:

    * **Scoped token** (carries an `ns_roles` claim): the JWT `namespaces`
      claim is AUTHORITATIVE and ORDERED — return it verbatim, in order, with
      NO union of other grants. This is what lets a token be limited to a
      chosen subset of the caller's namespaces, and the order is the read
      priority (honored by the search fan-out). The role per namespace was
      capped to the caller's live grant at mint time.
    * **Flat / wildcard token** (no `ns_roles`): union of the JWT
      `namespaces` claim and active `loom.namespace_grants` rows (grants
      widen), returned sorted.

    Either way a claimed namespace whose grant was revoked after the token
    was minted is left out (`authz.withdrawn_claims`).

    A delegated token (a `creator` claim) is neither: its claim narrowed to
    the creator's live grants (`_delegated_namespaces`).

    `subject` lets dev mode pass an explicit identity when no JWT is present.
    """
    from server.auth.authz import may_act_in_org, withdrawn_claims
    from server.auth.service_accounts import inheriting_creator

    auth = current_auth()

    # A token's claims and its subject's grants belong to its own org: a
    # subject string is not unique across orgs, so another org's grants under
    # the same name are not this caller's (the rule check_authorized applies
    # before any grant read).
    if auth is not None and not may_act_in_org(auth, org):
        return []

    # Delegated token: its claim, cut down to the creator's live grants.
    if auth is not None and auth.creator:
        return await _without_unreadable_restricted(org, await _delegated_namespaces(auth, org))

    # Scoped token: the ordered namespace claim is authoritative. Preserve
    # order (= read priority); do not union other grants (= subset restrict).
    if auth is not None and auth.has_ns_roles():
        claimed = [ns for ns in auth.namespaces if ns and ns != "*"]
        withdrawn = await withdrawn_claims(auth, org, claimed)
        return await _without_unreadable_restricted(
            org, [ns for ns in claimed if ns not in withdrawn]
        )

    jwt_namespaces: set[str] = set()
    resolved_subject: str | None = None
    if auth is not None and auth.subject:
        resolved_subject = auth.subject
        jwt_namespaces = {ns for ns in auth.namespaces if ns and ns != "*"}
        jwt_namespaces -= await withdrawn_claims(auth, org, sorted(jwt_namespaces))
    if not resolved_subject:
        resolved_subject = subject

    grant_namespaces: set[str] = set()
    if resolved_subject:
        from server.auth.aliases import subject_aliases

        # Grants under the person's other spelling count too, and a service
        # account reads through its creator's live grants when its row says
        # it inherits (the gate's rule, server/auth/service_accounts.py).
        subjects = await subject_aliases(org, resolved_subject)
        creator = await inheriting_creator(org, resolved_subject)
        if creator is not None:
            subjects = [*subjects, *await subject_aliases(org, creator)]
        grant_namespaces = await _granted_namespaces(org, subjects)

    return await _without_unreadable_restricted(org, sorted(jwt_namespaces | grant_namespaces))


async def _granted_namespaces(org: str, subjects: list[str]) -> set[str]:
    """Namespaces of `org` where any of `subjects` holds a live grant."""
    from server.core.ch import get_client
    from server.core.grant_sql import LIVE_GRANT

    rows = await get_client().query_all(
        """
        SELECT DISTINCT namespace
        FROM (SELECT * FROM loom.namespace_grants FINAL)
        WHERE org = {org:String} AND subject IN {subs:Array(String)} AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": subjects},
    )
    return {str(r["namespace"]) for r in rows if r.get("namespace")}


async def _delegated_namespaces(auth: AuthInfo, org: str) -> list[str]:
    """A delegated token's namespaces: where its creator holds a live grant
    now, inside the token's own claim (all of them under a flat `*` claim).
    Claim order is kept; nothing when the creator's account is disabled."""
    from server.auth.aliases import subject_aliases
    from server.auth.delegation import creator_account_active

    if not await creator_account_active(org, auth.creator):
        return []
    granted = await _granted_namespaces(org, await subject_aliases(org, auth.creator))
    if "*" in auth.namespaces and not auth.has_ns_roles():
        return sorted(granted)
    return [ns for ns in auth.namespaces if ns in granted]


async def _without_unreadable_restricted(org: str, namespaces: list[str]) -> list[str]:
    """A restricted namespace stays in a caller's list only
    when the caller passes the two-factor read check for it. Membership,
    grants, inheritance and wildcards put it in the list; this takes it out
    again. Order is preserved. With auth disabled nothing is removed."""
    if not namespaces:
        return namespaces
    from server.auth.authz import restricted_allows
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return namespaces
    try:
        restricted = await directory().restricted_set(org)
    except ClassificationUnavailable:
        # Fail closed: the set is unknown, so nothing that could be
        # restricted is served — every namespace is treated as restricted
        # and only the ones the caller passes for (which then also fail, as
        # the gate re-reads the directory) survive.
        restricted = frozenset(namespaces)
    out: list[str] = []
    for ns in namespaces:
        if ns in restricted or directory().config_classification(org, ns) == "restricted":
            if await restricted_allows(auth, Role.VIEWER, org, ns):
                out.append(ns)
            continue
        out.append(ns)
    return out


async def restricted_namespaces_hidden_from(org: str) -> list[str]:
    """Restricted namespaces of `org` the current caller may NOT read —
    the set an org-wide rollup must exclude even for an org admin. Empty with auth
    disabled."""
    from server.auth.authz import restricted_allows
    from server.operations.base import Role

    auth = current_auth()
    if auth is None:
        return []
    try:
        names = directory().restricted_code_namespaces(org)
        names |= set(await directory().restricted_set(org))
    except ClassificationUnavailable:
        # Cannot tell which are restricted: the rollup gets no filter it can
        # trust, so the caller must see nothing of the org's namespaces.
        return ["*"]
    hidden = [ns for ns in sorted(names) if not await restricted_allows(auth, Role.VIEWER, org, ns)]
    return hidden


@dataclass(frozen=True)
class OrgScopeFilter:
    """Resolved org-wide read scope for the current caller.

    ``accessible=False`` → non-admin caller in zero namespaces; return empty.
    Otherwise append ``where`` + ``params`` (both empty = see everything).
    """

    accessible: bool
    where: str
    params: dict[str, list[str]]


async def resolve_org_scope(org: str, *, column: str = "namespace") -> OrgScopeFilter:
    """WHERE fragment scoping an org-wide read to the namespaces the caller
    may see. Org/platform admins (and the auth-off path) get an empty filter;
    everyone else is constrained to `resolve_my_namespaces`, and a caller with
    no accessible namespaces yields ``accessible=False`` to short-circuit.

    Single source of truth for the privacy filter shared by the org-wide
    rollup ops — a bug here leaks one tenant's data to another. The
    exclusion (``ns_hidden``) and the inclusion (``ns_list``) bind different
    param names, so equal lists never read as the same scope.
    """
    from server.operations.base import Role

    auth = current_auth()
    if auth is None or not auth.subject:
        return OrgScopeFilter(accessible=True, where="", params={})
    # An org admin administers its own org; only a platform admin spans orgs.
    if auth.has_role(Role.PLATFORM_ADMIN) or (
        auth.has_role(Role.ORG_ADMIN) and auth.org in ("*", org)
    ):
        # An org admin sees everything EXCEPT the restricted
        # namespaces it holds no grant + claim for.
        hidden = await restricted_namespaces_hidden_from(org)
        if not hidden:
            return OrgScopeFilter(accessible=True, where="", params={})
        if hidden == ["*"]:
            return OrgScopeFilter(accessible=False, where="", params={})
        return OrgScopeFilter(
            accessible=True,
            where=f" AND {column} NOT IN {{ns_hidden:Array(String)}}",
            params={"ns_hidden": hidden},
        )
    ns_list = sorted(await resolve_my_namespaces(org))
    if not ns_list:
        return OrgScopeFilter(accessible=False, where="", params={})
    return OrgScopeFilter(
        accessible=True,
        where=f" AND {column} IN {{ns_list:Array(String)}}",
        params={"ns_list": ns_list},
    )


__all__ = [
    "OrgScopeFilter",
    "current_auth",
    "current_auth_var",
    "resolve_my_namespaces",
    "resolve_org_scope",
    "restricted_namespaces_hidden_from",
    "set_current_auth",
]
