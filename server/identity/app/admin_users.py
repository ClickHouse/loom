"""admin.users handler — org-wide membership rollup."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from server.core.ch import get_client
from server.core.grant_sql import VERIFIED_EMAIL_SQL
from server.identity.api.models_admin_users import (
    AdminUsersInput,
    AdminUsersOutput,
    UserNamespaceGrant,
    UserSummary,
)


async def handle_admin_users(input: AdminUsersInput) -> AdminUsersOutput:
    """Aggregate subjects from namespace_grants + namespaces.owner_email,
    join in last-active from the audit log, mark configured platform
    admins. ORG_ADMIN at the wire surface; the deployment's platform-admin
    list (operator identities across every org) goes only to a platform admin.
    """
    from server.settings import settings as _settings

    ch = get_client()
    admin_emails = _settings.platform_admins_set
    params: dict[str, Any] = {"org": input.org, "lim": int(input.limit)}

    ns_filter = ""
    if input.namespace:
        ns_filter = " AND namespace = {ns:String}"
        params["ns"] = input.namespace

    # Pull every (subject, namespace, role) the org knows about. UNION
    # two sources because grants table doesn't always carry the owner
    # (owners get implicit admin via the namespace row).
    rows = await ch.query_all(
        f"""
        SELECT subject, namespace, role, granted_at, pending_role, is_owner
        FROM (
            SELECT
                subject,
                namespace,
                toString(role) AS role,
                granted_at,
                pending_role,
                0 AS is_owner
            FROM loom.namespace_grants FINAL
            WHERE org = {{org:String}} AND revoked_at IS NULL {ns_filter}

            UNION ALL

            SELECT
                owner_email AS subject,
                namespace,
                'admin' AS role,
                created_at AS granted_at,
                '' AS pending_role,
                1 AS is_owner
            FROM loom.namespaces FINAL
            WHERE org = {{org:String}} AND active = 1
              AND owner_email != ''
              {ns_filter}
        )
        ORDER BY subject, namespace
        """,
        params=params,
    )

    # Group by subject, dedup (subject, namespace) — owner row beats
    # grant row when both exist.
    by_subject: dict[str, dict[str, dict[str, Any]]] = {}
    for r in rows:
        subj = str(r["subject"])
        ns = str(r["namespace"])
        existing = by_subject.setdefault(subj, {})
        prev = existing.get(ns)
        if prev is None or (r["is_owner"] and not prev["is_owner"]):
            existing[ns] = r

    # Last-active from audit_log.actor (the JWT principal), not
    # agent_events.user_id (the end-user the agent acts for, not the subject).
    subjects = list(by_subject.keys())
    last_active_map: dict[str, Any] = {}
    if subjects:
        active_rows = await ch.query_all(
            """
            SELECT actor AS subject, max(ts) AS last_active
            FROM loom.audit_log
            WHERE org = {org:String} AND actor IN {subjects:Array(String)}
            GROUP BY actor
            """,
            params={"org": input.org, "subjects": subjects},
        )
        last_active_map = {str(r["subject"]): r["last_active"] for r in active_rows}

    # Contact emails + password presence (humans vs service accounts).
    contact_map: dict[str, dict[str, Any]] = {}
    if subjects:
        contact_rows = await ch.query_all(
            """
            SELECT owner_email AS subject,
                   any(contact_email) AS contact,
                   max(password_hash != '') AS has_pw,
                   anyIf(contact_email, {verified}) AS verified_email
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND owner_email IN {subjects:Array(String)}
            GROUP BY owner_email
            """.replace("{verified}", VERIFIED_EMAIL_SQL),
            params={"org": input.org, "subjects": subjects},
        )
        contact_map = {
            str(r["subject"]): {
                "contact_email": str(r.get("contact") or ""),
                "has_pw": bool(r.get("has_pw") or False),
                "verified_email": str(r.get("verified_email") or ""),
            }
            for r in contact_rows
        }

    users: list[UserSummary] = []
    for subj in sorted(by_subject.keys()):
        grants = by_subject[subj]
        info = contact_map.get(subj, {})
        contact = str(info.get("contact_email", ""))
        # An email entry names only a verified address, as at login (`platform_roles`).
        verified = str(info.get("verified_email", ""))
        is_admin = subj.lower() in admin_emails or (
            verified.strip().lower() in admin_emails if verified else False
        )
        # Service-account filter: subjects without a contact_email AND
        # without password_hash are almost always service accounts.
        if not input.include_service_accounts:
            if not contact and not info.get("has_pw"):
                continue
        users.append(
            UserSummary(
                subject=subj,
                contact_email=contact,
                grants=[
                    UserNamespaceGrant(
                        namespace=g["namespace"],
                        role=str(g["role"]),
                        is_owner=bool(g["is_owner"]),
                        granted_at=g.get("granted_at"),
                        pending_role=(str(g["pending_role"]) or None)
                        if g.get("pending_role")
                        else None,
                    )
                    for g in sorted(grants.values(), key=lambda x: x["namespace"])
                ],
                is_platform_admin=is_admin,
                last_active=last_active_map.get(subj),
                has_password_auth=bool(info.get("has_pw")),
            )
        )
    capped = users[: int(input.limit)]
    # Counts over the capped page, matching what's rendered.
    pending_count = sum(1 for u in capped for g in u.grants if g.pending_role)
    seven_days = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=7)
    active_count = sum(
        1 for u in capped if u.last_active and u.last_active.replace(tzinfo=None) >= seven_days
    )
    return AdminUsersOutput(
        users=capped,
        total=len(users),
        total_pending_elevations=pending_count,
        total_active_last_7d=active_count,
        platform_admins_configured=sorted(admin_emails) if _sees_platform_admins() else [],
    )


def _sees_platform_admins() -> bool:
    """The configured platform-admin list names operators of the whole deployment, not
    members of this org: a platform admin (or the auth-off operator) reads it."""
    from server.auth.context import current_auth
    from server.operations.base import Role

    auth = current_auth()
    return auth is None or auth.has_role(Role.PLATFORM_ADMIN)
