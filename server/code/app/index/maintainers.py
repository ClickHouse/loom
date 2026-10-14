"""A repository's maintainer set is one across its branches.

Every branch of a repository is its own namespace with its own grant rows, so an admin
grant on ``code-x`` would say nothing about ``code-x-26-5`` and a maintainer would have
to ask branch by branch. This module keeps the set of *active admin* subjects identical
across the configured branch namespaces of one repository:

* ``sync_repo_maintainers`` — the union of active admins over the branches,
  written to every branch that lacks one (``granted_by =
  repo-maintainers:<source namespace>``). An explicit revocation on a branch
  that is newer than the subject's newest sibling grant is respected: the
  row is not re-added. Called after an admin grant is created or approved
  on a code namespace, when a branch is added, and once per process per
  namespace by the registrar (so environment-registered branches inherit).
* ``plan_revoke`` / ``write_revokes`` — a revoked admin leaves every branch,
  except a branch where they are the only active admin (the handler's own
  guard). ``grants.revoke`` plans first, writes the cutoffs for every branch
  in one call, then writes the grant rows.

Readers stay per branch: the approval fan-out already turns a person's branch requests
into one decision. Restricted branches also need the token to name them — a person's
next login does that; the rows written here are the direct grants that check reads.
"""

from __future__ import annotations

import logging
import time
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
    org: str, ns: str, subject: str, *, granted_by: str, now: datetime, revoked: bool = False
) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": ns,
        "subject": subject,
        "role": "admin",
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "granted_at": now,
        "granted_by": granted_by,
        "revoked_at": now if revoked else None,
        "version": time.time_ns(),
    }


async def _admin_rows(client: Any, org: str, branches: list[str]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = await client.query_all(
        """
        SELECT namespace, subject, ifNull(toString(status), 'active') AS status,
               revoked_at, granted_at, granted_by
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace IN {ns:Array(String)}
          AND role = 'admin'
        """,
        params={"org": org, "ns": list(branches)},
    )
    return rows


async def _insert(client: Any, rows: list[dict[str, Any]]) -> None:
    await client.insert(
        "loom.namespace_grants",
        rows,
        settings={"async_insert": 0, "wait_for_async_insert": 1},
    )


async def sync_repo_maintainers(org: str, namespace: str, *, ch: Any | None = None) -> int:
    """Bring every branch of `namespace`'s repository to the union of their
    active admins. Returns the number of grant rows written."""
    branches = branch_namespaces(org, namespace)
    if len(branches) < 2:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _admin_rows(client, org, branches)
    active: dict[str, dict[str, datetime]] = {ns: {} for ns in branches}
    revoked: dict[tuple[str, str], datetime] = {}
    for r in rows:
        ns, subject = str(r["namespace"]), str(r["subject"])
        if ns not in active:
            continue
        if r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE:
            active[ns][subject] = r["granted_at"]
        elif r.get("revoked_at") is not None:
            key = (ns, subject)
            revoked[key] = max(revoked.get(key, r["revoked_at"]), r["revoked_at"])
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
                continue  # removed from this branch after their newest sibling grant
            inserts.append(_row(org, ns, subject, granted_by=f"{GRANTED_BY_PREFIX}{src}", now=now))
    if inserts:
        await _insert(client, inserts)
        log.info(
            "repo maintainers: %s/%s — %d admin grant(s) mirrored across %d branches",
            org,
            namespace,
            len(inserts),
            len(branches),
        )
    return len(inserts)


async def plan_revoke(
    org: str, namespace: str, subject: str, *, ch: Any | None = None
) -> list[dict[str, Any]]:
    """The revoke rows that take `subject`'s active admin grant off every
    other branch of the repository, not written yet. A failed read plans
    none: the revoke that asked goes on, and the sibling grants stay for an
    operator to revoke, each with its own cutoff."""
    try:
        return await _plan_revoke(org, namespace, subject, ch=ch)
    except Exception:  # the fan-out never fails the revoke that caused it
        log.warning(
            "repo maintainers: revoke plan failed for %s/%s/%s",
            org,
            namespace,
            subject,
            exc_info=True,
        )
        return []


async def _plan_revoke(
    org: str, namespace: str, subject: str, *, ch: Any | None = None
) -> list[dict[str, Any]]:
    branches = [b for b in branch_namespaces(org, namespace) if b != namespace]
    if not branches:
        return []
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _admin_rows(client, org, branches)
    admins: dict[str, dict[str, dict[str, Any]]] = {ns: {} for ns in branches}
    for r in rows:
        if r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE:
            admins[str(r["namespace"])][str(r["subject"])] = r
    now = naive_utc_now()
    out: list[dict[str, Any]] = []
    for ns in branches:
        mine = admins[ns].get(subject)
        if mine is None:
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
    return out


async def write_revokes(
    org: str, namespace: str, subject: str, rows: list[dict[str, Any]], *, ch: Any | None = None
) -> None:
    """Write the planned revoke rows; the caller wrote their cutoffs first, so
    a failure here leaves grants that reach no older token's claim."""
    if not rows:
        return
    from server.core.ch import get_client

    try:
        await _insert(ch or get_client(), rows)
    except Exception:  # the fan-out never fails the revoke that caused it
        log.warning(
            "repo maintainers: revoke fan-out failed for %s/%s/%s",
            org,
            namespace,
            subject,
            exc_info=True,
        )
        return
    log.info(
        "repo maintainers: %s/%s — %r revoked on %d sibling branch(es)",
        org,
        namespace,
        subject,
        len(rows),
    )


async def sync_quietly(org: str, namespace: str, *, ch: Any | None = None) -> None:
    """The fan-out is a consequence, never a reason for the grant op to fail."""
    try:
        await sync_repo_maintainers(org, namespace, ch=ch)
    except Exception:  # the fan-out never fails the op that caused it
        log.warning("repo maintainers: sync failed for %s/%s", org, namespace, exc_info=True)
