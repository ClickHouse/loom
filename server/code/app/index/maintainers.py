"""A repository's maintainer set is one across its branches.

Every branch of a repository is its own namespace with its own grant rows, so an admin
grant on ``code-x`` would say nothing about ``code-x-26-5`` and a maintainer would have
to ask branch by branch. This module keeps the set of *active admin* subjects identical
across the configured branch namespaces of one repository:

* ``sync_repo_maintainers`` — the union of active admins over the branches,
  written to every branch that lacks one (``granted_by =
  repo-maintainers:<source namespace>``), from the admin grants given on a
  branch, not mirrored there; a subject whose grant was given on a branch no
  longer configured is mirrored from a mirror of it onto the branches they have
  no row on. A revocation or a lower role set on a branch after
  the subject's newest such grant is respected, and so is a request made there
  since: admin is not re-added. An older row of any kind is replaced. Called
  after an admin grant is created or approved on a code namespace, when a branch
  is added, and once per process per namespace by the registrar (so
  environment-registered branches inherit).
* ``revoke_repo_maintainer`` — a revoked admin leaves every branch, except a
  branch where they are the only active admin (the handler's own guard).

Readers stay per branch: the approval fan-out already turns a person's branch requests
into one decision. Restricted branches also need the token to name them — a person's
next login does that; the rows written here are the direct grants that check reads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager, AsyncExitStack, nullcontext
from datetime import datetime
from typing import Any

from server.core.datetimes import naive_utc_now
from server.core.errors import GrantBusyError

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


def repo_change(org: str, namespace: str, subject: str) -> AbstractAsyncContextManager[None]:
    """Hold `subject`'s maintainer grants on `namespace`'s repository: one change
    at a time across its branches, taken before any branch's own claim. A
    no-op off a configured repository."""
    from server.auth.grant_claims import grant_change
    from server.code.domain.index.repos import configured_repos

    me = next((c for c in configured_repos() if c.org == org and c.namespace == namespace), None)
    return grant_change(org, f"repo:{me.repo}", subject) if me is not None else nullcontext()


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
               revoked_at, granted_at, granted_by, version
        FROM loom.namespace_grants FINAL
        WHERE org = {org:String} AND namespace IN {ns:Array(String)}
        """,
        params={"org": org, "ns": list(branches)},
    )
    return rows


_GRANT_ROW_SQL = """
    SELECT namespace, subject, role, ifNull(toString(status), 'active') AS status,
           revoked_at, granted_at, granted_by, version
    FROM loom.namespace_grants FINAL
    WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}
"""


def _version(row: dict[str, Any] | None) -> int | None:
    """The grant row's version: a new one on every change, where `granted_at`
    keeps whole seconds and two changes within one compare equal."""
    return None if row is None else int(row["version"])


def _live_admin(row: dict[str, Any] | None) -> bool:
    return (
        row is not None
        and row.get("role") == "admin"
        and row.get("revoked_at") is None
        and str(row.get("status") or "active") in _ACTIVE
    )


# The grant rows a plan was made from, by (namespace, subject).
_Seen = dict[tuple[str, str], dict[str, Any]]


async def _mirror_wanted(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> bool:
    """A mirror is written only over the grant it was planned from (one changed
    since, an admin's lowering say, is left to the next sync), and only while
    its source is still the admin grant it copies: a revoke that landed since
    the plan read must not be copied back."""
    subject, source_ns = row["subject"], row["granted_by"].removeprefix(GRANTED_BY_PREFIX)
    if _version(now) != _version(seen.get((row["namespace"], subject))):
        return False
    source = await client.query_one(
        _GRANT_ROW_SQL, params={"org": org, "ns": source_ns, "sub": subject}
    )
    return _live_admin(source) and _version(source) == _version(seen.get((source_ns, subject)))


async def _still_admin(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> bool:
    """A revocation stands while the subject still holds the admin grant it
    was planned against; one granted again since is newer and stays."""
    read = seen.get((row["namespace"], row["subject"]))
    return read is not None and _live_admin(now) and _version(now) == _version(read)


# (client, org, planned row, current row, the plan's rows) -> write it?
_Wanted = Callable[[Any, str, dict[str, Any], dict[str, Any] | None, _Seen], Awaitable[bool]]


async def _write_under_claims(
    client: Any,
    org: str,
    planned: list[dict[str, Any]],
    still_wanted: _Wanted,
    read: list[dict[str, Any]],
    *,
    hold_repo: bool,
) -> int:
    """Write each subject's planned rows that `still_wanted` keeps in one insert,
    holding its repository claim (with `hold_repo`, else the caller holds it)
    and then its branch claims: with the repository claim first, one change
    across a subject's branches runs at a time. A subject that fails writes
    none of its rows; one whose claim is busy does not stop the others, and
    is raised once all were tried. Any other failure stops at once: Keeper or
    ClickHouse not answering one subject will not answer the next."""
    seen = {(str(r["namespace"]), str(r["subject"])): r for r in read}
    by_subject: dict[str, list[dict[str, Any]]] = {}
    for row in planned:
        by_subject.setdefault(row["subject"], []).append(row)
    written, failed = 0, []
    for subject, rows in by_subject.items():
        try:
            written += await _write_subject(
                client, org, subject, rows, still_wanted, seen, hold_repo
            )
        except GrantBusyError as e:  # that subject's change runs; the others are free
            failed.append(e)
    if failed:
        raise failed[0]
    return written


async def _write_subject(
    client: Any,
    org: str,
    subject: str,
    rows: list[dict[str, Any]],
    still_wanted: _Wanted,
    seen: _Seen,
    hold_repo: bool,
) -> int:
    """`_write_under_claims` for one subject's rows."""
    from server.auth.grant_claims import grant_change
    from server.core import grant_cache

    rows.sort(key=lambda r: r["namespace"])
    async with AsyncExitStack() as held:
        if hold_repo:
            await held.enter_async_context(repo_change(org, rows[0]["namespace"], subject))
        for row in rows:
            await held.enter_async_context(grant_change(org, row["namespace"], subject))
        wanted = []
        for row in rows:
            ns = row["namespace"]
            now = await client.query_one(
                _GRANT_ROW_SQL, params={"org": org, "ns": ns, "sub": subject}
            )
            if await still_wanted(client, org, row, now, seen):
                wanted.append(row)
        if wanted:
            try:
                await client.insert(
                    "loom.namespace_grants",
                    wanted,
                    settings={"async_insert": 0, "wait_for_async_insert": 1},
                )
            finally:
                grant_cache.invalidate(org)
    return len(wanted)


async def sync_repo_maintainers(org: str, namespace: str, *, ch: Any | None = None) -> int:
    """Bring every branch of `namespace`'s repository to the union of their
    active admins. Returns the number of grant rows written."""
    branches = branch_namespaces(org, namespace)
    if len(branches) < 2:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _grant_rows(client, org, branches)
    # Grant changes are ordered by row version: `granted_at` keeps whole seconds,
    # so a lowering and a promotion within one would compare equal.
    active: dict[str, set[str]] = {ns: set() for ns in branches}
    # The newest admin grant per subject that is not itself a mirror, and its branch:
    # a mirror's own version says when the sync wrote it, not when admin was given.
    union: dict[str, tuple[int, datetime, str]] = {}
    # The version of any other row per (branch, subject): a revoke, a lower role or a
    # request. A request after a revoke replaces its row, so it is respected the same.
    ended: dict[tuple[str, str], int] = {}
    # A subject whose admin grant was given on a branch no longer configured: a mirror
    # of it is its source, onto the branches it has no row on.
    mirrored: dict[str, tuple[int, datetime, str]] = {}
    for r in rows:
        ns, subject, version = str(r["namespace"]), str(r["subject"]), int(r["version"])
        if ns not in active:
            continue
        live = r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE
        if live and r.get("role") == "admin":
            active[ns].add(subject)
            granted_by = str(r.get("granted_by") or "")
            if not granted_by.startswith(GRANTED_BY_PREFIX):
                if subject not in union or version > union[subject][0]:
                    union[subject] = (version, r["granted_at"], ns)
            elif granted_by.removeprefix(GRANTED_BY_PREFIX) not in active:
                # Its source branch is gone: the mirror stands in for it.
                if subject not in mirrored or version > mirrored[subject][0]:
                    mirrored[subject] = (version, r["granted_at"], ns)
        else:
            ended[(ns, subject)] = version
    for subject in union:
        mirrored.pop(subject, None)
    now = naive_utc_now()
    inserts: list[dict[str, Any]] = []
    for ns in branches:
        for subject, (version, at, src) in (union | mirrored).items():
            if subject in active[ns] or ended.get((ns, subject), 0) > version:
                continue  # admin there already, or removed, lowered or asked there since
            if subject in mirrored and (ns, subject) in ended:
                continue  # a mirror's version is no time to compare a row there with
            # A mirror keeps its source's time; its own version replaces any older row.
            inserts.append(
                _row(
                    org, ns, subject, granted_by=f"{GRANTED_BY_PREFIX}{src}", now=now, granted_at=at
                )
            )
    written = (
        await _write_under_claims(client, org, inserts, _mirror_wanted, rows, hold_repo=True)
        if inserts
        else 0
    )
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
    org: str, namespace: str, subject: str, *, ch: Any | None = None
) -> int:
    """Revoke `subject`'s active admin grant on every other branch of the
    repository; a branch where they are the only active admin keeps them
    (the same guard `grants.revoke` applies). The caller holds `repo_change`.
    Returns rows written."""
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
    written = (
        await _write_under_claims(client, org, out, _still_admin, rows, hold_repo=False)
        if out
        else 0
    )
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
