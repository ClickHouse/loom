"""Direct grants that belong to a token, not a person.

A restricted namespace is readable only by a token that names it AND whose own
subject holds a direct grant. A token from the token mint
(``me_service_accounts.create``) runs under a generated ``sa:`` subject: it can
name the namespace but holds no grant of its own, so without one every read of
the restricted repository it was minted for would be refused. This module
writes that grant at mint, capped, ``granted_by`` the person who minted the
token; revokes it with the token; and, when the person's own grant on the
namespace is revoked, revokes the grants of every token they minted there —
access follows the person, at the moment it is taken away, without a read-time
rule that would refuse operator-minted service accounts (which have no creator
row at all).

Standard namespaces are never touched here: they read through the ordinary
rules and creator-grant inheritance (``server/auth/context.py``).
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from datetime import datetime
from typing import TYPE_CHECKING, Any

from fastapi import HTTPException

from server.core.datetimes import naive_utc_now_seconds
from server.identity.app import me_callers, namespace_classification
from server.identity.store import grants as grants_repo
from server.operations.base import grant_rank

if TYPE_CHECKING:
    from server.auth.delegation import Delegated

log = logging.getLogger(__name__)

RESTRICTED_GRANT_CAP = "writer"


def restricted_grant_role(is_code_namespace: bool, effective_role: str) -> str:
    """The grant a token gets on a restricted namespace: a repository index
    or a tracker record is read-only reference data, so ``viewer`` (the code
    and tracker ceiling); a restricted memory
    namespace gets the token's own role capped at ``writer`` — a token never
    administers a restricted namespace."""
    if is_code_namespace:
        return "viewer"
    if not grant_rank(effective_role):
        return "viewer"
    if grant_rank(effective_role) > grant_rank(RESTRICTED_GRANT_CAP):
        return RESTRICTED_GRANT_CAP
    return effective_role


def revoked_grant_row(org: str, subject: str, row: dict[str, Any], now: datetime) -> dict[str, Any]:
    """The new version of a live grant row that marks it revoked — every
    column carried forward (ReplacingMergeTree discipline)."""
    return {
        "org": org,
        "namespace": row["namespace"],
        "subject": subject,
        "role": row["role"],
        "status": row.get("status") or "active",
        "pending_role": None,
        "pending_since": None,
        "granted_at": row.get("granted_at") or now,
        "granted_by": row.get("granted_by") or "",
        "revoked_at": now,
        "version": time.time_ns(),
    }


async def restricted_roles(
    org: str, subject: str, delegated: Delegated, requested: Mapping[str, str | None]
) -> dict[str, str]:
    """The role of the direct grant a new token gets on each restricted namespace it
    names: its own role there, capped at the minting subject's own grant, which a
    restricted namespace needs of every reader. That holds for one the mint named and
    for one a narrower parent's own claim brought in through `*` (that parent named it at
    its own mint). Where the subject holds no grant, a named one is refused (403) and one
    that came from expanding `*` is left without a grant."""
    out: dict[str, str] = {}
    for ns in delegated.namespaces:
        if ns == "*" or not await namespace_classification.is_restricted(org, ns):
            continue
        held = await me_callers.caller_role_on(org, ns, subject)
        if held is None:
            if ns not in requested:
                continue
            raise HTTPException(
                status_code=403,
                detail={"code": "AUTH_INSUFFICIENT", "message": f"cannot delegate access on {ns}"},
            )
        token_role = (delegated.ns_roles or {}).get(ns) or delegated.roles[0]
        out[ns] = min(held, token_role, key=grant_rank)
    return out


async def grant_restricted_to_token(
    org: str,
    sa_subject: str,
    creator: str,
    ns_roles: dict[str, str],
    *,
    is_restricted: Any = None,
    is_code_namespace: Any = None,
) -> list[str]:
    """Write one direct grant per RESTRICTED namespace in ``ns_roles`` for
    the token's own subject. Returns the namespaces granted."""
    if is_restricted is None:
        from server.identity.app.namespace_classification import is_restricted as _isr

        is_restricted = _isr
    if is_code_namespace is None:
        from server.code.domain.index.repos import repo_for_namespace
        from server.tracker.domain.namespaces import tracker_namespace

        def is_code_namespace(o: str, n: str) -> bool:
            # read-only reference data: a repository index or a tracker record
            return repo_for_namespace(o, n) is not None or tracker_namespace(o, n) is not None

    granted: list[str] = []
    rows: list[dict[str, Any]] = []
    now = naive_utc_now_seconds()
    for ns, role in ns_roles.items():
        if not await is_restricted(org, ns):
            continue
        rows.append(
            {
                "org": org,
                "namespace": ns,
                "subject": sa_subject,
                "role": restricted_grant_role(bool(is_code_namespace(org, ns)), role),
                "status": "active",
                "pending_role": None,
                "pending_since": None,
                "granted_at": now,
                "granted_by": creator,
                "revoked_at": None,
                "version": time.time_ns(),
            }
        )
        granted.append(ns)
    # One statement for every grant: a synchronous insert costs seconds on
    # the Cloud cluster, and the mint waits for all of them.
    await grants_repo.insert_rows(rows)
    return granted


async def _live_grants(
    ch: Any, org: str, subject: str, namespace: str | None
) -> list[dict[str, Any]]:
    sql = """
        SELECT namespace, role, status, granted_at, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND subject = {sub:String} AND revoked_at IS NULL
    """
    params: dict[str, Any] = {"org": org, "sub": subject}
    if namespace is not None:
        sql += "          AND namespace = {ns:String}\n"
        params["ns"] = namespace
    rows: list[dict[str, Any]] = list(await ch.query_all(sql, params=params))
    return rows


async def revoke_token_grants(
    org: str, sa_subject: str, *, namespace: str | None = None, ch: Any = None
) -> int:
    """Revoke every live grant held by a token's own subject (on one
    namespace when given). Returns the number revoked."""
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    rows = await _live_grants(ch, org, sa_subject, namespace)
    now = naive_utc_now_seconds()
    await grants_repo.insert_rows([revoked_grant_row(org, sa_subject, r, now) for r in rows])
    return len(rows)


async def cascade_revoke_to_created_tokens(
    org: str,
    namespace: str,
    subject: str,
    *,
    unheld: bool,
    taken: tuple[str, ...] = (),
    ch: Any = None,
) -> int:
    """A revoke of `subject` on ``namespace`` took the person's rows there, stored
    under `taken`: revoke the grants every live token they created holds there,
    under each stored subject as spelled and the subject's spellings. When the
    revoke named an address no verified account holds (`unheld`), those are every
    spelling a revocation reaches (`aliases.spellings`), an unverified pair too
    (the tokens an unverified account made while the address's grant admitted it),
    less any account that still holds an active grant of its own there; otherwise
    the verified ones only, since an unverified account naming the email may be
    someone else's. Returns tokens touched."""
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()
    from server.auth.aliases import spellings, verified_spellings

    # The rows this revoke took go unasked: a read of them now may still see them live.
    gone = {s.lower() for s in taken} | {subject.lower()}
    try:
        if unheld:
            creators = []
            for c in await spellings(org, subject, ch=ch):
                if c.lower() not in gone:
                    # An account naming the address that still holds an active grant
                    # of its own here keeps its tokens' grants: they may be what that
                    # grant gave. A pending request admits nothing, so it keeps none.
                    own = await verified_spellings(org, c, ch=ch)
                    if await grants_repo.stored_spellings(
                        org, namespace, own, rows="active", ch=ch
                    ):
                        continue
                creators.append(c)
        else:
            creators = await verified_spellings(org, subject, ch=ch)
    except Exception as e:  # noqa: BLE001 — the tokens made under the exact subject still go
        log.warning("token grant cascade: spellings of %r unread: %s", subject, e)
        creators = [subject]
    creators = list(dict.fromkeys([*taken, *creators]))
    sa_rows = await ch.query_all(
        """
        SELECT DISTINCT sa_subject FROM loom.service_accounts FINAL
        WHERE org = {org:String} AND lower(created_by) IN {subs:Array(String)}
          AND revoked_at IS NULL
        """,
        # Any case, as the token list matches its maker.
        params={"org": org, "subs": me_callers.lowered(creators)},
    )
    touched = 0
    for r in sa_rows:
        sa = str(r.get("sa_subject") or "")
        if sa and await revoke_token_grants(org, sa, namespace=namespace, ch=ch):
            touched += 1
    return touched
