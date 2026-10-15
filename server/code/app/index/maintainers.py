"""A repository's maintainer set is one across its branches.

Every branch of a repository is its own namespace with its own grant rows, so an admin
grant on ``code-x`` would say nothing about ``code-x-26-5`` and a maintainer would have
to ask branch by branch. This module keeps the set of *active admin* subjects identical
across the configured branch namespaces of one repository:

* ``sync_repo_maintainers`` — the union of active admins over the branches,
  written to every branch that lacks one (``granted_by =
  repo-maintainers:<source namespace>``). An explicit revocation on a branch,
  or a lower role set there, newer than the subject's newest sibling grant is
  respected: admin is not re-added. Called after an admin grant is created or approved
  on a code namespace, when a branch is added, and once per process per
  namespace by the registrar (so environment-registered branches inherit).
* ``revoke_repo_maintainer`` — a revoked admin leaves every branch, except a
  branch where they are the only active admin (the handler's own guard).

Readers stay per branch: the approval fan-out already turns a person's branch requests
into one decision. Restricted branches also need the token to name them — a person's
next login does that; the rows written here are the direct grants that check reads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now

log = logging.getLogger(__name__)

GRANTED_BY_PREFIX = "repo-maintainers:"
_ACTIVE = ("", "active")


def branch_namespaces(org: str, namespace: str) -> list[str]:
    """Every configured branch namespace of the repository `namespace`
    belongs to, itself included, in configuration order; ``[]`` when
    `namespace` is not a configured code entry."""
    from server.code.domain.index.repos import configured_repos

    repos = configured_repos()
    me = next((c for c in repos if c.org == org and c.namespace == namespace), None)
    if me is None:
        return []
    return [c.namespace for c in repos if c.org == org and c.repo == me.repo]


def _row(
    org: str,
    ns: str,
    subject: str,
    *,
    granted_by: str,
    now: datetime,
    revoked: bool = False,
    granted_at: datetime | None = None,
) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": ns,
        "subject": subject,
        "role": "admin",
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "granted_at": granted_at or now,
        "granted_by": granted_by,
        "revoked_at": now if revoked else None,
        "version": time.time_ns(),
    }


async def _grant_rows(client: Any, org: str, branches: list[str]) -> list[dict[str, Any]]:
    """Every subject's grant row on `branches`, of any role: a branch where a
    maintainer was lowered holds a non-admin row for them."""
    rows: list[dict[str, Any]] = await client.query_all(
        """
        SELECT namespace, subject, role, ifNull(toString(status), 'active') AS status,
               revoked_at, granted_at, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace IN {ns:Array(String)}
        """,
        params={"org": org, "ns": list(branches)},
    )
    return rows


_GRANT_ROW_SQL = """
    SELECT namespace, subject, role, ifNull(toString(status), 'active') AS status,
           revoked_at, granted_at, granted_by
    FROM loom.namespace_grants FINAL
    WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}
"""


def _state(row: dict[str, Any] | None) -> tuple[Any, ...] | None:
    if row is None:
        return None
    return row.get("role"), row.get("status"), row.get("revoked_at"), row.get("granted_at")


def _unchanged(now: dict[str, Any] | None, read: dict[str, Any] | None) -> bool:
    """A mirror is written only over the grant it was planned from: one changed
    since (an admin's lowering, say) is left to the next sync."""
    return _state(now) == _state(read)


def _still_admin(now: dict[str, Any] | None, _read: dict[str, Any] | None) -> bool:
    """A revocation stands while the subject is still an admin there."""
    return (
        now is not None
        and now.get("role") == "admin"
        and now.get("revoked_at") is None
        and str(now.get("status") or "active") in _ACTIVE
    )


async def _write_under_claims(
    client: Any,
    org: str,
    planned: list[dict[str, Any]],
    still_wanted: Callable[[dict[str, Any] | None, dict[str, Any] | None], bool],
    read: list[dict[str, Any]],
) -> int:
    """Write each planned row under its grant's claim, if `still_wanted(current
    row, row as read)` says so. A row that fails does not stop the others; the
    first failure is raised once all were tried."""
    from server.auth.grant_claims import grant_change
    from server.core import grant_cache

    seen = {(str(r["namespace"]), str(r["subject"])): r for r in read}
    written, failed = 0, []
    for row in planned:
        ns, subject = row["namespace"], row["subject"]
        try:
            async with grant_change(org, ns, subject):
                now = await client.query_one(
                    _GRANT_ROW_SQL, params={"org": org, "ns": ns, "sub": subject}
                )
                if not still_wanted(now, seen.get((ns, subject))):
                    continue
                await client.insert(
                    "loom.namespace_grants",
                    [row],
                    settings={"async_insert": 0, "wait_for_async_insert": 1},
                )
        except Exception as e:  # noqa: BLE001 — the other branches are still written
            failed.append(e)
            continue
        finally:
            grant_cache.invalidate(org)
        written += 1
    if failed:
        raise failed[0]
    return written


async def sync_repo_maintainers(org: str, namespace: str, *, ch: Any | None = None) -> int:
    """Bring every branch of `namespace`'s repository to the union of their
    active admins. Returns the number of grant rows written."""
    branches = branch_namespaces(org, namespace)
    if len(branches) < 2:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _grant_rows(client, org, branches)
    active: dict[str, dict[str, datetime]] = {ns: {} for ns in branches}
    # When a subject's admin ended on a branch: revoked, or set to a lower role.
    revoked: dict[tuple[str, str], datetime] = {}
    for r in rows:
        ns, subject = str(r["namespace"]), str(r["subject"])
        if ns not in active:
            continue
        live = r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE
        if live and r.get("role") == "admin":
            active[ns][subject] = r["granted_at"]
        elif r.get("revoked_at") is not None or live:
            key = (ns, subject)
            ended = r["revoked_at"] if r.get("revoked_at") is not None else r["granted_at"]
            revoked[key] = max(revoked.get(key, ended), ended)
    # newest sibling grant per subject, and the branch it came from
    union: dict[str, tuple[datetime, str]] = {}
    for ns, subs in active.items():
        for subject, at in subs.items():
            if subject not in union or at > union[subject][0]:
                union[subject] = (at, ns)
    now = naive_utc_now()
    inserts: list[dict[str, Any]] = []
    for ns in branches:
        for subject, (at, src) in union.items():
            if subject in active[ns]:
                continue
            rv = revoked.get((ns, subject))
            if rv is not None and rv >= at:
                continue  # removed or lowered on this branch after their newest sibling grant
            # A mirror keeps its source's time: it is no newer a promotion than that grant.
            inserts.append(
                _row(
                    org,
                    ns,
                    subject,
                    granted_by=f"{GRANTED_BY_PREFIX}{src}",
                    now=now,
                    granted_at=at,
                )
            )
    written = await _write_under_claims(client, org, inserts, _unchanged, rows) if inserts else 0
    if written:
        log.info(
            "repo maintainers: %s/%s — %d admin grant(s) mirrored across %d branches",
            org,
            namespace,
            written,
            len(branches),
        )
    return written


async def revoke_repo_maintainer(
    org: str,
    namespace: str,
    subject: str,
    *,
    ch: Any | None = None,
    granted_before: datetime | None = None,
) -> int:
    """Revoke `subject`'s active admin grant on every other branch of the
    repository, or only those granted no later than `granted_before`; a branch
    where they are the only active admin keeps them (the same guard
    `grants.revoke` applies). Returns rows written."""
    branches = [b for b in branch_namespaces(org, namespace) if b != namespace]
    if not branches:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _grant_rows(client, org, branches)
    admins: dict[str, dict[str, dict[str, Any]]] = {ns: {} for ns in branches}
    for r in rows:
        live = r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE
        if live and r.get("role") == "admin":
            admins[str(r["namespace"])][str(r["subject"])] = r
    now = naive_utc_now()
    out: list[dict[str, Any]] = []
    for ns in branches:
        mine = admins[ns].get(subject)
        if mine is None or (granted_before is not None and mine["granted_at"] > granted_before):
            continue
        if len(admins[ns]) <= 1:
            log.warning(
                "repo maintainers: %s/%s keeps %r — only admin of that branch",
                org,
                ns,
                subject,
            )
            continue
        out.append(
            _row(
                org,
                ns,
                subject,
                granted_by=str(mine.get("granted_by") or ""),
                now=now,
                revoked=True,
            )
        )
    written = await _write_under_claims(client, org, out, _still_admin, rows) if out else 0
    if written:
        log.info(
            "repo maintainers: %s/%s — %r revoked on %d sibling branch(es)",
            org,
            namespace,
            subject,
            written,
        )
    return written


async def sync_quietly(org: str, namespace: str, *, ch: Any | None = None) -> None:
    """The fan-out is a consequence, never a reason for the grant op to fail."""
    try:
        await sync_repo_maintainers(org, namespace, ch=ch)
    except Exception:  # the fan-out never fails the op that caused it
        log.warning("repo maintainers: sync failed for %s/%s", org, namespace, exc_info=True)
