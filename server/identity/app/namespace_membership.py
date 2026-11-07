"""Namespace self-service membership: join, leave, cancel a request, list the
caller's namespaces and mark them seen."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any

from server.auth.grant_claims import admins_change, grant_change
from server.core.ch import get_client
from server.core.grant_sql import LIVE_GRANT
from server.identity.api.models import (
    NamespaceCancelRequestInput,
    NamespaceCancelRequestOutput,
    NamespaceJoinInput,
    NamespaceJoinOutput,
    NamespaceLeaveInput,
    NamespaceLeaveOutput,
    NamespaceListMineInput,
    NamespaceListMineOutput,
    NamespaceMarkSeenInput,
    NamespaceMarkSeenOutput,
    _MineEntry,
)
from server.identity.app import namespace as _registry
from server.identity.domain import join_policy as _jp
from server.operations.base import grant_rank


async def handle_namespace_join(
    input: NamespaceJoinInput,
) -> NamespaceJoinOutput:
    """Self-grant a role on a namespace.

    `viewer` on a public namespace is auto-active. `writer`/`admin`, any
    role on a `requires_approval` namespace, and any join of a private
    namespace land `pending` — zero access until an admin approves.
    Idempotent: re-joining at or below the current role is a no-op.
    """
    from server.auth.sharing import is_collection
    from server.core.errors import ValidationError

    if is_collection(input.namespace):
        raise ValidationError("a collection is shared by grants, not joined")
    caller = _registry.resolve_subject(input.subject)
    async with grant_change(input.org, input.namespace, caller):
        return await _join(input, caller)


async def _join(input: NamespaceJoinInput, caller: str) -> NamespaceJoinOutput:
    """The join, under the grant's claim."""

    ch = get_client()
    row = await ch.query_one(
        """
        SELECT toString(visibility) AS visibility, active, requires_approval,
               ifNull(kind, 'memory') AS kind, toString(join_policy) AS join_policy,
               toString(classification) AS classification
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
        LIMIT 1
        """,
        params={"org": input.org, "ns": input.namespace},
    )
    if row is None or not bool(row.get("active", False)):
        from server.core.errors import NotFoundError

        raise NotFoundError(f"namespace {input.namespace!r} does not exist in org {input.org!r}")

    # The directory row says how it is joined. `inherit` resolves to the default rule (public
    # + viewer + not requires_approval → active; else pending). Code namespaces cap members
    # at viewer; a writer/admin self-join on one is refused, not pended.
    from server.core.errors import AuthInsufficientError, ValidationError
    from server.identity.store import grants as grants_repo

    existing = await grants_repo.find_any_grant(input.org, input.namespace, caller)
    policy = _jp.effective_join_policy(row)
    decision = _jp.join_gate(policy=policy, role=input.role)
    # A restricted row is kept out of the directory (its existence is part of what is kept),
    # except a restricted repository whose rule is approval, which a person may find and
    # ask for; to a caller holding no grant, any other one answers as an absent name does.
    hidden = str(row.get("classification") or "") == "restricted" and not (
        str(row.get("kind") or "memory") == "code" and policy == "approval"
    )
    if (decision == "closed" or hidden) and existing is None:
        # A closed namespace is joined by an admin's grant only; to a caller holding none, a
        # distinct refusal would confirm the name, so it answers as an absent one does.
        from server.core.errors import NotFoundError

        raise NotFoundError(f"namespace {input.namespace!r} does not exist in org {input.org!r}")
    kind = str(row.get("kind") or "memory")
    ceiling = _jp.code_role_ceiling_violation(kind, input.role)
    if ceiling:
        raise ValidationError(ceiling, code="ROLE_CEILING")
    if decision == "closed":
        raise AuthInsufficientError(
            f"namespace {input.namespace!r} is closed to self-join; ask an admin for a grant",
            join_policy=policy,
        )
    gated = decision == "pending"
    now = datetime.now(UTC).replace(tzinfo=None)

    if existing is not None:
        current_role = existing.role
        existing_pending = existing.pending_role
        # Active equal-or-higher → no-op.
        if grant_rank(current_role) >= grant_rank(input.role):
            return NamespaceJoinOutput(
                joined=False,
                namespace=input.namespace,
                role=current_role,
                status=existing.status or "active",
                requested_role=existing_pending or None,
                pending_admins=(
                    await _list_namespace_admins(ch, input.org, input.namespace)
                    if existing_pending
                    else []
                ),
                joined_at=now,
            )
        # Same pending request already on file → no-op, return pending state.
        if gated and existing_pending == input.role:
            return NamespaceJoinOutput(
                joined=False,
                namespace=input.namespace,
                role=current_role,
                status="pending",
                requested_role=input.role,
                pending_admins=await _list_namespace_admins(
                    ch,
                    input.org,
                    input.namespace,
                ),
                joined_at=now,
            )

    if gated:
        # No active grant → status='pending' row (zero access until an
        # admin flips it active); a second request replaces the first, it
        # never becomes a base role. Active grant → keep that base role and
        # record the elevation as pending_role.
        if existing is None or existing.status == "pending":
            await grants_repo.insert_row(
                {
                    "org": input.org,
                    "namespace": input.namespace,
                    "subject": caller,
                    "role": input.role,
                    "status": "pending",
                    "pending_role": None,
                    "pending_since": now,
                    "granted_at": now,
                    "granted_by": caller,
                    "revoked_at": None,
                    "version": time.time_ns(),
                }
            )
            pending_admins = await _list_namespace_admins(ch, input.org, input.namespace)
            return NamespaceJoinOutput(
                joined=False,
                namespace=input.namespace,
                role=input.role,
                status="pending",
                requested_role=input.role,
                pending_admins=pending_admins,
                joined_at=now,
            )

        base_role = existing.role
        await grants_repo.insert_row(
            {
                "org": input.org,
                "namespace": input.namespace,
                "subject": caller,
                "role": base_role,
                "status": "active",
                "pending_role": input.role,
                "pending_since": now,
                "granted_at": now,
                "granted_by": caller,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        )
        pending_admins = await _list_namespace_admins(ch, input.org, input.namespace)
        return NamespaceJoinOutput(
            joined=True,
            namespace=input.namespace,
            role=base_role,
            status="pending",
            requested_role=input.role,
            pending_admins=pending_admins,
            joined_at=now,
        )

    # Ungated path — single row, exact role requested.
    await grants_repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": caller,
            "role": input.role,
            "status": "active",
            "pending_role": None,
            "pending_since": None,
            "granted_at": now,
            "granted_by": caller,
            "revoked_at": None,
            "version": time.time_ns(),
        }
    )
    return NamespaceJoinOutput(
        joined=True,
        namespace=input.namespace,
        role=input.role,
        status="active",
        joined_at=now,
    )


async def _list_namespace_admins(ch: Any, org: str, namespace: str) -> list[str]:
    """Return the subjects who can approve a pending grant on this
    (org, namespace). Used to tell the requester who they're waiting
    on. Excludes pending rows — only active admins can approve."""
    rows = await ch.query_all(
        """
        SELECT subject FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace = {ns:String}
          AND role = 'admin' AND {live}
        """.replace("{live}", LIVE_GRANT),
        params={"org": org, "ns": namespace},
    )
    return [r["subject"] for r in rows]


async def handle_namespace_leave(
    input: NamespaceLeaveInput,
) -> NamespaceLeaveOutput:
    """Drop caller's own grant on a namespace.

    Refuses if the caller is the only active admin — forces an explicit
    ownership transfer first (via `grants.create` to promote another
    member, then re-leave).
    """
    caller = _registry.resolve_subject(input.subject)
    from server.identity.store import grants as grants_repo

    before = await grants_repo.find_active_grant(input.org, input.namespace, caller)
    if before is None or before.role != "admin":
        async with grant_change(input.org, input.namespace, caller):
            now = await grants_repo.find_active_grant(input.org, input.namespace, caller)
            if now is None or now.role != "admin":
                return await _leave(input, caller)
    # Only an admin's leave takes admin away, so only it is counted against the admins.
    async with (
        admins_change(input.org, input.namespace),
        grant_change(input.org, input.namespace, caller),
    ):
        return await _leave(input, caller)


async def _leave(input: NamespaceLeaveInput, caller: str) -> NamespaceLeaveOutput:
    """The leave, under the grant's claim."""

    ch = get_client()
    from server.identity.store import grants as grants_repo

    existing = await grants_repo.find_active_grant(input.org, input.namespace, caller)
    if existing is None:
        return NamespaceLeaveOutput(left=False, namespace=input.namespace)

    if existing.role == "admin":
        if await grants_repo.count_active_admins(input.org, input.namespace, ch=ch) <= 1:
            from server.core.errors import PreconditionFailedError

            raise PreconditionFailedError(
                f"{caller!r} is the only admin of {input.namespace!r}; promote "
                f"another member to admin (grants.create) before leaving"
            )

    now = datetime.now(UTC).replace(tzinfo=None)
    await grants_repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": caller,
            "role": existing.role,
            "granted_at": now,
            "granted_by": existing.granted_by,
            "revoked_at": now,
            "version": time.time_ns(),
        }
    )
    return NamespaceLeaveOutput(left=True, namespace=input.namespace, left_at=now)


async def handle_namespace_cancel_request(
    input: NamespaceCancelRequestInput,
) -> NamespaceCancelRequestOutput:
    """Clear the caller's pending_role; preserve their base grant."""
    caller = _registry.resolve_subject(input.subject)
    async with grant_change(input.org, input.namespace, caller):
        return await _cancel_request(input, caller)


async def _cancel_request(
    input: NamespaceCancelRequestInput, caller: str
) -> NamespaceCancelRequestOutput:
    """The cancel, under the grant's claim."""

    from server.identity.store import grants as grants_repo

    existing = await grants_repo.find_any_grant(input.org, input.namespace, caller)
    if existing is None:
        return NamespaceCancelRequestOutput(
            cancelled=False,
            namespace=input.namespace,
        )

    pending_role = existing.pending_role
    # No-base-access request: status='pending', role IS the request, no
    # pending_role (see handle_namespace_join's `existing is None and gated`
    # branch). Elevation request: status='active', pending_role carries it.
    no_base_access = existing.status == "pending"
    if not pending_role and not no_base_access:
        return NamespaceCancelRequestOutput(
            cancelled=False,
            namespace=input.namespace,
        )

    now = datetime.now(UTC).replace(tzinfo=None)
    if no_base_access and not pending_role:
        # No underlying base row — cancelling means revoking entirely.
        await grants_repo.insert_row(
            {
                "org": input.org,
                "namespace": input.namespace,
                "subject": caller,
                "role": existing.role,
                "status": "pending",
                "pending_role": None,
                "pending_since": None,
                "granted_at": now,
                "granted_by": existing.granted_by or caller,
                "revoked_at": now,
                "version": time.time_ns(),
            }
        )
        return NamespaceCancelRequestOutput(
            cancelled=True,
            namespace=input.namespace,
            cleared_role=existing.role,
            cancelled_at=now,
        )

    # New-shape: clear pending_role, leave base role active.
    await grants_repo.insert_row(
        {
            "org": input.org,
            "namespace": input.namespace,
            "subject": caller,
            "role": existing.role,
            "status": "active",
            "pending_role": None,
            "pending_since": None,
            "granted_at": now,
            "granted_by": existing.granted_by or caller,
            "revoked_at": None,
            "version": time.time_ns(),
        }
    )
    return NamespaceCancelRequestOutput(
        cancelled=True,
        namespace=input.namespace,
        cleared_role=pending_role,
        cancelled_at=now,
    )


_MEMBERSHIP_SQL = """
        SELECT
            g.namespace AS namespace,
            g.role AS role,
            ifNull(toString(g.status), 'active') AS status,
            g.pending_role AS pending_role,
            g.pending_since AS pending_since,
            g.granted_at AS joined_at,
            toString(ifNull(n.visibility, 'private')) AS visibility,
            ifNull(n.kind, '') AS kind,
            ifNull(n.description, '') AS description,
            if(g.granted_by != g.subject AND
               (v.last_seen_at IS NULL OR v.last_seen_at < g.granted_at),
               1, 0) AS is_new
        FROM (SELECT * FROM loom.namespace_grants FINAL) AS g
        LEFT JOIN (SELECT * FROM loom.namespaces FINAL) AS n
            ON n.org = g.org AND n.namespace = g.namespace
        LEFT JOIN (
            -- a visit under any of the person's spellings clears `is_new`
            SELECT org, namespace, max(last_seen_at) AS last_seen_at
            FROM loom.namespace_visits FINAL
            WHERE org = {org:String} AND lower(subject) IN {subs:Array(String)}
            GROUP BY org, namespace
        ) AS v
            ON v.org = g.org AND v.namespace = g.namespace
        WHERE g.org = {org:String} AND lower(g.subject) IN {subs:Array(String)}
          AND g.revoked_at IS NULL
          AND ifNull(n.active, true) = true
        ORDER BY g.granted_at DESC
        -- A grant on a namespace with no directory row (a repository listed only in
        -- LOOM_CODE_REPOS_JSON) is a member's too: with the default, the join's empty
        -- side reads `active = false` and the row would be dropped.
        SETTINGS join_use_nulls = 1
"""


def _kind(org: str, namespace: str, kind: object) -> str:
    """A namespace's kind: its directory row's, else `code` for a configured repository
    entry, else `memory`, the directory's own default."""
    if kind:
        return str(kind)
    from server.code.domain.index.repos import repo_for_namespace

    return "code" if repo_for_namespace(org, namespace) is not None else "memory"


async def _membership_rows(ch: Any, org: str, subjects: list[str]) -> list[dict[str, Any]]:
    """Active grant rows (joined with the directory + visits) for any of
    `subjects` — one person's aliases, or a token's creator's."""
    # Any case, as the gate matches a grant's subject (`authz.grant_role_for`).
    subs = list(dict.fromkeys(s.lower() for s in subjects))
    rows: list[dict[str, Any]] = list(
        await ch.query_all(_MEMBERSHIP_SQL, params={"org": org, "subs": subs})
    )
    return rows


def _one_per_namespace(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per namespace when a person holds grants under more than one
    spelling (`server.auth.aliases`): the first row (newest grant) stands, the
    highest role wins, and the namespace is new only if no spelling has seen it."""
    by_ns: dict[str, dict[str, Any]] = {}
    for r in rows:
        ns = str(r.get("namespace") or "")
        head = by_ns.get(ns)
        if head is None:
            by_ns[ns] = dict(r)
            continue
        if grant_rank(str(r.get("role"))) > grant_rank(str(head.get("role"))):
            head["role"] = r.get("role")
        head["is_new"] = int(bool(head.get("is_new")) and bool(r.get("is_new")))
    return list(by_ns.values())


async def _inherited_rows(ch: Any, org: str, caller: str, skip: set[str]) -> list[dict[str, Any]]:
    """A client token (`sa:` subject minted by `service_accounts.create`) reads every namespace its
    creator holds a live grant on when its row says it inherits (`server/auth/service_accounts`,
    the gate's rule), so `namespace.list_mine` must list those too, or
    the doctor and any client asking "what can I read" would see only the token's own rows.
    Returns the creator's rows for namespaces not in `skip`, minus restricted ones —
    inheritance never reads a restricted namespace. Fails open to [] on any error: the list
    stays what it was."""
    from server.auth.service_accounts import inheriting_creator

    creator = await inheriting_creator(org, caller, ch=ch)
    if creator is None:
        return []
    try:
        from server.auth.aliases import subject_aliases
        from server.identity.app.namespace_classification import (
            RESTRICTED,
            config_classification,
            restricted_set,
        )

        subjects = await subject_aliases(org, creator, ch=ch)
        rows = await _membership_rows(ch, org, subjects)
        try:
            restricted = await restricted_set(org)
        except Exception as e:  # noqa: BLE001 — unknown restrictions: list nothing inherited
            # Inheritance never reads a restricted namespace. Without the restricted set we
            # cannot tell which those are, so none are listed; the caller's own rows are
            # unaffected.
            logging.getLogger(__name__).warning(
                "namespace.list_mine: restricted set unreadable (%s); no inherited rows", e
            )
            return []
        out: list[dict[str, Any]] = []
        for r in rows:
            ns = str(r.get("namespace") or "")
            active = (r.get("status") or "active") == "active"
            # The directory's restricted set, or a repository entry's own classification
            # for one listed only in LOOM_CODE_REPOS_JSON.
            config_restricted = config_classification(org, ns) == RESTRICTED
            if not ns or ns in skip or ns in restricted or config_restricted or not active:
                continue
            skip.add(ns)
            out.append(r)
        return out
    except Exception:  # noqa: BLE001 — inherited rows are extra; list_mine keeps its own rows
        return []


async def handle_namespace_list_mine(
    input: NamespaceListMineInput,
) -> NamespaceListMineOutput:
    """List namespaces the caller is a member of in `org`.

    Union of the JWT's `namespaces` claim and the caller's active rows in
    `loom.namespace_grants` — under the caller's subject or any spelling the
    directory ties to it (`server.auth.aliases`: a person's login handle and
    verified email are one principal, as the read gate counts them). Also auto-claims
    `personal-<sub>` if it doesn't yet exist — the dashboard calls this on
    load, so the personal namespace lazily-creates on first visit.
    """
    from server.auth.aliases import subject_aliases

    ch = get_client()
    caller = _registry.resolve_subject(input.subject)

    # The personal namespace is provisioned by the transports' admission
    # gate (server.transport.gates.authorize →
    # server.auth.personal_namespace.ensure_personal_namespace_cached), so
    # this handler stays read-only.
    # Union of grant rows + JWT namespaces. LEFT JOIN namespace_visits
    # for the `is_new` flag — true when the caller hasn't visited since
    # the grant was made. A grant written under the person's verified email
    # before their first sign-in (an operator's, an invitation's, a sync's) is
    # theirs as much as one under the login handle: the gate reads both,
    # so this list names both — else the dashboard would refuse a namespace
    # the server serves.
    subjects = await subject_aliases(input.org, caller, ch=ch)
    grant_rows = _one_per_namespace(await _membership_rows(ch, input.org, subjects))
    seen = {r["namespace"] for r in grant_rows}
    # Only resolve admins for rows with a pending_role.
    needs_admins = {r["namespace"] for r in grant_rows if r.get("pending_role")}
    admins_by_ns: dict[str, list[str]] = {}
    for ns in needs_admins:
        admins_by_ns[ns] = await _list_namespace_admins(ch, input.org, ns)

    out_rows: list[_MineEntry] = [
        _MineEntry(
            namespace=r["namespace"],
            role=r["role"],
            status=(r.get("status") or "active"),
            pending_role=r.get("pending_role") or None,
            pending_since=r.get("pending_since"),
            pending_admins=admins_by_ns.get(r["namespace"], []),
            visibility=r.get("visibility") or "private",
            kind=_kind(input.org, r["namespace"], r.get("kind")),
            description=r.get("description") or "",
            joined_at=r["joined_at"],
            is_new=bool(r.get("is_new")),
        )
        for r in grant_rows
    ]

    from server.auth.authz import live_claims
    from server.auth.context import current_auth

    auth = current_auth()
    if auth is not None:
        jwt_only = await live_claims(
            auth, input.org, [ns for ns in auth.namespaces if ns not in seen]
        )
        # Claim-only namespaces have no grant row to JOIN through, so look up
        # kind/visibility directly — otherwise a code namespace named in a
        # token's claim would masquerade as a memory namespace and the header
        # picker would happily default to it.
        meta = await _namespace_meta(ch, input.org, jwt_only) if jwt_only else {}
        for ns in jwt_only:
            m = meta.get(ns) or {}
            out_rows.append(
                _MineEntry(
                    namespace=ns,
                    role="jwt",
                    visibility=m.get("visibility") or "private",  # type: ignore[arg-type]
                    kind=_kind(input.org, ns, m.get("kind")),
                    description=m.get("description") or "",
                )
            )

    # What a client token reads through its creator.
    seen_all = seen | {r.namespace for r in out_rows}
    for r in await _inherited_rows(ch, input.org, caller, seen_all):
        out_rows.append(
            _MineEntry(
                namespace=r["namespace"],
                role=r["role"],
                status="active",
                visibility=r.get("visibility") or "private",
                kind=_kind(input.org, r["namespace"], r.get("kind")),
                description=r.get("description") or "",
                joined_at=r.get("joined_at"),
                is_new=False,
                inherited=True,
            )
        )

    return NamespaceListMineOutput(rows=out_rows)


async def _namespace_meta(ch: Any, org: str, namespaces: list[str]) -> dict[str, dict[str, str]]:
    """kind/visibility/description for a handful of namespaces; {} on error
    (fail-open to the private/memory defaults — never sink list_mine)."""
    try:
        rows = await ch.query_all(
            """
            SELECT namespace, ifNull(kind, 'memory') AS kind,
                   toString(visibility) AS visibility, description
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace IN {ns:Array(String)}
            """,
            params={"org": org, "ns": namespaces},
        )
    except Exception:  # noqa: BLE001 — rows list without metadata
        return {}
    return {
        str(r["namespace"]): {
            "kind": str(r.get("kind") or "memory"),
            "visibility": str(r.get("visibility") or "private"),
            "description": str(r.get("description") or ""),
        }
        for r in rows
    }


async def handle_namespace_mark_seen(
    input: NamespaceMarkSeenInput,
) -> NamespaceMarkSeenOutput:
    """Record that the caller has visited `namespace`. Clears the
    `is_new` indicator on subsequent list_mine calls."""
    ch = get_client()
    caller = _registry.resolve_subject(input.subject)
    now = datetime.now(UTC).replace(tzinfo=None)
    await ch.insert(
        "loom.namespace_visits",
        [
            {
                "org": input.org,
                "namespace": input.namespace,
                "subject": caller,
                "last_seen_at": now,
                "version": time.time_ns(),
            }
        ],
    )
    return NamespaceMarkSeenOutput(namespace=input.namespace, seen_at=now)
