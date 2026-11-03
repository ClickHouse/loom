"""Per-request auth context for handlers.

A ContextVar carries the caller's identity instead of threading it through
every handler signature: set once in the transport, read by the handlers that
need it. In dev mode (`LOOM_AUTH_REQUIRED=false`) it stays None and handlers
fall back to an explicit `subject` in the input body.
"""

from __future__ import annotations

from contextvars import ContextVar
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from server.auth.directory import ClassificationUnavailable, directory
from server.auth.fail_closed import note_answered_unverified, refused_unverified, reset
from server.core.ch import set_quota_key

if TYPE_CHECKING:
    from server.auth.authz import CachedGate
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

    A delegated token (a `creator` claim) is neither: its claim narrowed to
    the creator's live grants (`_delegated_namespaces`).

    Every candidate then stays only where the gate lets the token read it
    (`authz.CachedGate`), so the list never names what a read would refuse:
    a claim capped at a revoked grant (`authz.claim_cap`) drops out there.

    `subject` lets dev mode pass an explicit identity when no JWT is present.
    """
    from server.auth.authz import may_act_in_org
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
        return await _gate_readable(auth, org, await _delegated_namespaces(auth, org))

    # Scoped token: the ordered namespace claim is authoritative. Preserve
    # order (= read priority); do not union other grants (= subset restrict).
    if auth is not None and auth.has_ns_roles():
        return await _gate_readable(auth, org, named_claims(auth))

    jwt_namespaces: set[str] = set()
    resolved_subject: str | None = None
    if auth is not None and auth.subject:
        resolved_subject = auth.subject
        jwt_namespaces = set(named_claims(auth))
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

    candidates = jwt_namespaces | grant_namespaces
    if auth is None:
        return sorted(candidates)
    # Collections come from their own resolver (grants of every principal of
    # the request, inherited down the tree), already decided by the gate's rule.
    from server.auth import sharing

    shared = await sharing.readable(auth, org)
    others = sorted(ns for ns in candidates if not sharing.is_collection(ns))
    return [*await _gate_readable(auth, org, others), *shared]


def named_claims(auth: AuthInfo) -> list[str]:
    """The namespaces the token's claim names, in order, without `*`."""
    return list(dict.fromkeys(ns for ns in auth.namespaces if ns and ns != "*"))


async def _gate_readable(auth: AuthInfo | None, org: str, namespaces: list[str]) -> list[str]:
    """Those of `namespaces`, in order, the gate lets `auth` read; all of them
    with auth disabled."""
    from server.auth.authz import CachedGate
    from server.operations.base import Role

    if auth is None:
        return namespaces
    gate = CachedGate(auth, org)
    return [ns for ns in namespaces if await gate.admits(ns, Role.VIEWER)]


async def _granted_namespaces(org: str, subjects: list[str]) -> set[str]:
    """Namespaces of `org` where any of `subjects`, in any case, holds a live grant."""
    from server.core.ch import get_client
    from server.core.grant_sql import LIVE_GRANT

    rows = await get_client().query_all(
        """
        SELECT DISTINCT namespace
        FROM (SELECT * FROM loom.namespace_grants FINAL)
        WHERE org = {org:String} AND lower(subject) IN {subs:Array(String)} AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "subs": [s.lower() for s in subjects]},
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


async def readable_named_claims(
    auth: AuthInfo | None, org: str, gate: CachedGate | None = None
) -> list[str]:
    """The namespaces `auth` names (no `*`), in claim order, that the gate
    (`authz.check_authorized`) lets it read, asked through `gate` (a new
    `authz.CachedGate` when not given). Empty with no token. The MCP transport
    names these when a token has no default. Grants come from the cached grant
    maps, so a steady-state call reads nothing new."""
    from server.auth.authz import CachedGate, may_act_in_org
    from server.operations.base import Role

    if auth is None or not may_act_in_org(auth, org):
        return []
    gate = gate or CachedGate(auth, org)
    return [ns for ns in named_claims(auth) if await gate.admits(ns, Role.VIEWER)]


async def delegated_reach(
    auth: AuthInfo, org: str, gate: CachedGate | None = None
) -> tuple[list[str], list[str]]:
    """(writable, readable): the namespaces a delegated token may write and
    read by the gate's rules, asked through `gate` (a new `authz.CachedGate`
    when not given). The candidates are its named claims, in order, and under
    a `*` claim every namespace its creator holds a live grant on, sorted. The
    subject's own grants never widen it."""
    from server.auth.authz import CachedGate
    from server.operations.base import Role

    gate = gate or CachedGate(auth, org)
    candidates = named_claims(auth)
    if "*" in auth.namespaces:
        ranks = (await gate.grants(auth.creator)).ranks
        candidates += sorted(ns for ns, rank in ranks.items() if rank and ns not in candidates)
    readable = [ns for ns in candidates if await gate.admits(ns, Role.VIEWER)]
    writable = [ns for ns in readable if await gate.admits(ns, Role.WRITER)]
    return writable, readable


async def restricted_namespaces_hidden_from(org: str) -> list[str]:
    """Restricted namespaces of `org` the current caller may NOT read — the
    set an org-wide rollup must exclude even for an org admin (collections are
    left out by `collection_clause`). Empty with auth disabled; `["*"]` when it
    cannot be told."""
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
        # trust, so the caller must see nothing of the org's namespaces, and
        # the request that hid it all is the server's (fail_closed).
        note_answered_unverified()
        return ["*"]
    # A listing the caller did not scope: a namespace left out does not mark.
    return [
        ns
        for ns in sorted(names)
        if not await restricted_allows(auth, Role.VIEWER, org, ns, hides_answer=False)
    ]


async def collection_clause(org: str, column: str = "namespace") -> tuple[str, dict[str, Any]]:
    """A WHERE fragment that leaves out every collection of `org` the current
    caller cannot read: by prefix, so it names only the readable ones. Empty
    with auth disabled and for a platform admin, who read every collection."""
    from server.auth import sharing
    from server.operations.base import Role

    auth = current_auth()
    if auth is None or auth.has_role(Role.PLATFORM_ADMIN):
        return "", {}
    readable = await sharing.readable(auth, org)
    return (
        f" AND (NOT startsWith({column}, {{col_prefix:String}})"
        f" OR {column} IN {{col_readable:Array(String)}})",
        {"col_prefix": sharing.COLLECTION_PREFIX, "col_readable": readable},
    )


@dataclass(frozen=True)
class OrgScopeFilter:
    """Resolved org-wide read scope for the current caller.

    ``accessible=False`` → non-admin caller in zero namespaces; return empty.
    Otherwise append ``where`` + ``params`` (both empty = see everything).
    """

    accessible: bool
    where: str
    params: dict[str, list[str]]


async def rollup_member_namespaces(org: str) -> list[str]:
    """`resolve_my_namespaces`, sorted, for an org-wide rollup's scope. One that
    comes back empty because a read failed is the request's answer refused, so
    it marks the request the server's, as an admin's `*` scope does."""
    reset()
    ns_list = sorted(await resolve_my_namespaces(org))
    if not ns_list:
        refused_unverified()
    return ns_list


async def every_readable_namespace(org: str, *, column: str = "namespace") -> OrgScopeFilter:
    """WHERE fragment for a caller that reads every namespace of `org` (an org
    admin, or a flat `*` claim): all of them EXCEPT the restricted namespaces
    it holds no grant + claim for, and the collections it cannot read. Empty
    with auth disabled."""
    hidden = await restricted_namespaces_hidden_from(org)
    if hidden == ["*"]:
        return OrgScopeFilter(accessible=False, where="", params={})
    where, params = await collection_clause(org, column)
    if hidden:
        where += f" AND {column} NOT IN {{ns_hidden:Array(String)}}"
        params["ns_hidden"] = hidden
    return OrgScopeFilter(accessible=True, where=where, params=params)


async def resolve_org_scope(org: str, *, column: str = "namespace") -> OrgScopeFilter:
    """WHERE fragment scoping an org-wide read to the namespaces the caller
    may see. Org/platform admins get `every_readable_namespace`'s exclusions,
    the auth-off path an empty filter; everyone else is constrained to
    `resolve_my_namespaces`, and a caller with
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
        return await every_readable_namespace(org, column=column)
    ns_list = await rollup_member_namespaces(org)
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
    "delegated_reach",
    "every_readable_namespace",
    "named_claims",
    "readable_named_claims",
    "resolve_my_namespaces",
    "resolve_org_scope",
    "restricted_namespaces_hidden_from",
    "rollup_member_namespaces",
    "set_current_auth",
]
