"""A repository's maintainers flow across its branches.

Every branch of a repository is its own namespace with its own grant rows, so an admin
grant on ``code-x`` would say nothing about ``code-x-26-5`` and a maintainer would have
to ask branch by branch. This module copies *active admin* grants between the
configured branch namespaces of one repository, in one direction (``_reaches``): the
operator's entries pass their admins to every branch, and a branch added from the
page (``code.repo.add``, open to any org member) passes its admins only to other
standard branches added from the page: a restricted one (``_restricted``) takes the
operator's admins only. Registering a branch of a repository the operator configures
gives no grant on the operator's namespaces.

* ``sync_repo_maintainers`` — every branch gets the union of the active admins
  of the branches that reach it, written where it lacks one (``granted_by =
  repo-maintainers:<source namespace>``), from the admin grants given on a
  branch, not mirrored there; a subject whose grant was given on a branch no
  longer configured is mirrored from a mirror of it onto the branches they have
  no row on. A revocation or a lower role set on a branch after the subject's
  newest such grant is respected, and so is a request made there since: admin
  is not re-added. An older row of any kind is replaced. Called after an admin
  grant is created or approved on a code namespace, when a branch is added,
  restricted or widened, and by the registrar on each pass until the
  self-service registry is read, then once per process per namespace (so
  environment-registered branches inherit), and by the ingest pass on every
  pass for a repository with a restricted branch added from the page. Each pass
  also revokes, on a restricted branch added from the page, every copy that
  does not trace to an operator grant (``_stray``), or rewrites it as a copy of
  the subject's one.
* ``revoke_repo_maintainer`` — a revoked admin leaves the branches the revoke
  reaches, except a branch where they are the only active admin (the handler's
  own guard). From an operator entry it takes off a branch added from the page
  only a mirror, and none from a standard one while the subject holds an admin
  grant given on one. On a restricted branch added from the page it takes only
  copies: one whose source is not an operator entry (``_foreign``) without the
  guard, unless the subject keeps an admin grant given on an operator entry other
  than the revoked one, which the next sync copies over it; an operator copy
  only on an operator revoke, under the guard. The next sync takes a copy the
  guard keeps once it no longer traces to an operator grant.

Readers stay per branch: the approval fan-out already turns a person's branch requests
into one decision. Restricted branches also need the token to name them — a person's
next login does that; the rows written here are the direct grants that check reads.
"""

from __future__ import annotations

import logging
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import (
    AbstractAsyncContextManager,
    AsyncExitStack,
    asynccontextmanager,
    nullcontext,
)
from datetime import datetime
from typing import Any

from server.core.ch import sequential_reads
from server.core.datetimes import naive_utc_now
from server.core.errors import GrantBusyError
from server.core.grant_sql import LIVE_GRANT
from server.operations.base import grant_rank

log = logging.getLogger(__name__)

GRANTED_BY_PREFIX = "repo-maintainers:"
_ACTIVE = ("", "active")
# The namespaces whose sync waited for the registry read, logged once each per process.
_UNSYNCED: set[tuple[str, str]] = set()


def reset_for_tests() -> None:
    """Forget which namespaces logged a skipped sync."""
    _UNSYNCED.clear()


def _branches(org: str, repo: str) -> dict[str, str]:
    """Every configured branch namespace of `repo`, in configuration order,
    with its source: ``config`` for the operator's entries, ``registry`` for a
    branch added from the page."""
    from server.code.domain.index.repos import configured_repos

    return {c.namespace: c.source for c in configured_repos() if c.org == org and c.repo == repo}


def _reaches(source: str, target: str) -> bool:
    """An admin change on a branch whose source is `source` (``config`` or
    ``registry``) reaches a branch whose source is `target`. Any
    org member may add a branch of a repository the operator configures, so a
    branch added from the page never passes its admins (or a revoke of one) to
    an operator entry; the operator's admins reach every branch."""
    return source == "config" or target != "config"


async def _restricted(client: Any, org: str, sources: dict[str, str]) -> set[str]:
    """The restricted branches among the ones in `sources` added from the page, by
    this process's snapshot or by their directory rows read now, as
    `namespace_classification.is_restricted` reads it: a restriction made on another
    replica may be newer than the snapshot. A failed read raises, so no branch is
    given a copy or skipped by a revoke on a guess."""
    from server.code.domain.index.repos import configured_repos
    from server.identity.app.registration import classifications

    added = [ns for ns, source in sources.items() if source == "registry"]
    if not added:
        return set()
    with sequential_reads():
        rows = await classifications(org, added, ch=client)
    snapshot = {c.namespace: c.classification for c in configured_repos() if c.org == org}
    return {ns for ns in added if "restricted" in (rows.get(ns), snapshot.get(ns))}


def _is_copy(row: dict[str, Any]) -> bool:
    """The row was written by this fan-out, not given on its branch."""
    return str(row.get("granted_by") or "").startswith(GRANTED_BY_PREFIX)


def _foreign(row: dict[str, Any], config: list[str]) -> bool:
    """A live copy, of any role (a lowering keeps the copy's source), whose source is
    none of the operator entries in `config`: one a restricted branch added from the
    page must not hold."""
    source = str(row.get("granted_by") or "").removeprefix(GRANTED_BY_PREFIX)
    return _live(row) and _is_copy(row) and source not in config


def repo_of(org: str, namespace: str) -> str | None:
    """The repository `namespace` is a configured branch of, in this process's
    snapshot; None off a configured repository."""
    from server.code.domain.index.repos import configured_repos

    me = next((c for c in configured_repos() if c.org == org and c.namespace == namespace), None)
    return me.repo if me is not None else None


def repo_change(org: str, repo: str | None, subject: str) -> AbstractAsyncContextManager[None]:
    """Hold `subject`'s maintainer grants on `repo`: one change at a time across
    its branches, taken before any branch's own claim. A no-op when `repo` is
    None."""
    from server.auth.grant_claims import grant_change

    return grant_change(org, repo_claim_key(repo), subject) if repo is not None else nullcontext()


def repo_claim_key(repo: str) -> str:
    """The claim name of a repository: a sync, a revoke and a restriction serialise
    on its admins claim."""
    return f"repo:{repo}"


@asynccontextmanager
async def repo_admin_claims(org: str, repo: str | None, subject: str) -> AsyncIterator[None]:
    """Hold what a change to a maintainer's copies on the repository's other
    branches (a sync, or a revoke that takes them off) needs: `subject`'s
    maintainer grants there, then the repository's admins, from its own
    last-admin count to its last write, so two such changes in one repository
    run one after the other. Taken before any namespace's admins claim. A no-op
    when `repo` is None."""
    from server.auth.grant_claims import admins_change

    if repo is None:
        yield
        return
    async with repo_change(org, repo, subject), admins_change(org, repo_claim_key(repo)):
        yield


async def _read_registry_now(client: Any) -> None:
    """Read the self-service registry now, consistent with every write made
    before it on any replica. A revoke that skipped a branch added from the
    page would leave its copies live, and no later sync removes them, so a
    failed read raises `GrantBusyError`."""
    from server.code.app.index.repo_registry import read_registry

    with sequential_reads():
        read = await read_registry(client)
    if not read:
        raise GrantBusyError("the self-service registry cannot be read; retry shortly")


async def read_repo(org: str, namespace: str, *, ch: Any | None = None) -> str | None:
    """Before a change that takes admin on `namespace`: the repository it is a
    branch of, from the self-service registry read now (`_read_registry_now`),
    so a branch another process added since this one's last read is seen. It
    names the claims `repo_admin_claims` takes. Off a code namespace only its
    directory row is read, and None returned."""
    from server.core.ch import get_client

    client = ch or get_client()
    if repo_of(org, namespace) is None and not await _is_code_namespace(client, org, namespace):
        return None
    await _read_registry_now(client)
    return repo_of(org, namespace)


def _row(
    org: str,
    ns: str,
    subject: str,
    *,
    granted_by: str,
    now: datetime,
    revoked: bool = False,
    granted_at: datetime | None = None,
    role: str = "admin",
    version: int | None = None,
) -> dict[str, Any]:
    return {
        "org": org,
        "namespace": ns,
        "subject": subject,
        "role": role,
        "status": "active",
        "pending_role": None,
        "pending_since": None,
        "granted_at": granted_at or now,
        "granted_by": granted_by,
        "revoked_at": now if revoked else None,
        "version": version or time.time_ns(),
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


async def _read_grants(
    client: Any, org: str, branches: list[str]
) -> tuple[int, list[dict[str, Any]]]:
    """When the read began, and `_grant_rows` read with sequential consistency: a
    lagging replica could miss a grant just made, and a stray's revoke planned from
    the rows is versioned at the read, or just above the stray (`_revoke_row`)."""
    read_at = time.time_ns()
    with sequential_reads():
        rows = await _grant_rows(client, org, branches)
    return read_at, rows


_GRANT_ROW_SQL = """
    SELECT namespace, subject, role, ifNull(toString(status), 'active') AS status,
           revoked_at, granted_at, granted_by, version
    FROM loom.namespace_grants FINAL
    WHERE org = {org:String} AND namespace = {ns:String} AND subject = {sub:String}
"""


# A branch's live admins: the code plane reads grants itself, not through the identity store.
_ADMINS_SQL = (
    "SELECT count() AS n FROM loom.namespace_grants FINAL WHERE org = {org:String}"
    " AND namespace = {ns:String} AND role = 'admin' AND " + LIVE_GRANT
)


def _version(row: dict[str, Any] | None) -> int | None:
    """The grant row's version: a new one on every change, where `granted_at`
    keeps whole seconds and two changes within one compare equal."""
    return None if row is None else int(row["version"])


def _live(row: dict[str, Any] | None) -> bool:
    return (
        row is not None
        and row.get("revoked_at") is None
        and str(row.get("status") or "active") in _ACTIVE
    )


def _live_admin(row: dict[str, Any] | None) -> bool:
    return _live(row) and (row or {}).get("role") == "admin"


# The grant rows a plan was made from, by (namespace, subject).
_Seen = dict[tuple[str, str], dict[str, Any]]


async def _mirror_wanted(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> dict[str, Any] | None:
    """A mirror is written only over the grant it was planned from (one changed
    since, an admin's lowering say, is left to the next sync), and only while
    its source is still the admin grant it copies: a revoke that landed since
    the plan read must not be copied back."""
    return await _copy_wanted(client, org, row, now, seen, await _source_row(client, org, row))


async def _source_row(client: Any, org: str, row: dict[str, Any]) -> dict[str, Any] | None:
    """The grant row a planned copy copies, read now."""
    source_ns = row["granted_by"].removeprefix(GRANTED_BY_PREFIX)
    source: dict[str, Any] | None = await client.query_one(
        _GRANT_ROW_SQL, params={"org": org, "ns": source_ns, "sub": row["subject"]}
    )
    return source


async def _copy_wanted(
    client: Any,
    org: str,
    row: dict[str, Any],
    now: dict[str, Any] | None,
    seen: _Seen,
    source: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """`_mirror_wanted` with the source row already read."""
    subject, source_ns = row["subject"], row["granted_by"].removeprefix(GRANTED_BY_PREFIX)
    if _version(now) != _version(seen.get((row["namespace"], subject))):
        return None
    if source is None or not _live_admin(source):
        return None
    if _version(source) != _version(seen.get((source_ns, subject))):
        return None
    if await _target_restricted_now(client, org, row["namespace"], source_ns, source):
        return None
    return row


async def _target_restricted_now(
    client: Any, org: str, target: str, source: str, source_row: dict[str, Any]
) -> bool:
    """True when the target is restricted now, read under the claims; False without
    a read for a copy of an admin grant given on an operator entry."""
    sources = _branches(org, repo_of(org, target) or "")
    if sources.get(source) == "config" and not _is_copy(source_row):
        return False
    # A target the snapshot no longer places is read as one added from the page.
    return target in await _restricted(client, org, {target: sources.get(target, "registry")})


async def _copy_unchanged(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> dict[str, Any] | None:
    """A stray copy's planned row on a restricted branch added from the page is written
    while the row the plan read is unchanged, with no last-admin guard: a stray never
    keeps the branch's last admin."""
    read = seen.get((row["namespace"], row["subject"]))
    return row if read is not None and _live(now) and _version(now) == _version(read) else None


async def _rewrite_wanted(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> dict[str, Any] | None:
    """A stray copy planned to become a copy of the operator grant. When that grant is
    no longer a live admin grant, the stray's revoke is written instead: the
    restricted branch must not keep it. One that only changed since the plan (given
    again, say) leaves the stray to the next sync."""
    read_at = time.time_ns()
    source = await _source_row(client, org, row)
    if await _copy_wanted(client, org, row, now, seen, source):
        return row
    if now is None or _live_admin(source) or not await _copy_unchanged(client, org, row, now, seen):
        return None
    _log_stray(org, now)
    return _revoke_row(org, now, read_at=read_at)


async def _still_admin(
    client: Any, org: str, row: dict[str, Any], now: dict[str, Any] | None, seen: _Seen
) -> dict[str, Any] | None:
    """A revocation stands while the subject still holds the admin grant it
    was planned against (one granted again since is newer and stays) and is
    not the branch's only admin, counted under its admins claim."""
    read = seen.get((row["namespace"], row["subject"]))
    if read is None or not _live_admin(now) or _version(now) != _version(read):
        return None
    admins = await client.query_one(_ADMINS_SQL, params={"org": org, "ns": row["namespace"]})
    return row if int((admins or {}).get("n") or 0) > 1 else None


# (client, org, planned row, current row, the plan's rows) -> the row to write, or None.
# Some checks read nothing more and are async only to share this signature.
_Wanted = Callable[
    [Any, str, dict[str, Any], dict[str, Any] | None, _Seen], Awaitable[dict[str, Any] | None]
]


async def _write_under_claims(
    client: Any,
    org: str,
    planned: list[dict[str, Any]],
    still_wanted: _Wanted,
    read: list[dict[str, Any]],
    *,
    hold_repo: str | None,
) -> int:
    """Write each subject's planned rows that `still_wanted` keeps in one insert,
    holding the claims `_write_subject` takes (with `hold_repo` None the caller
    holds the repository's): one change across a subject's branches runs at a
    time. A subject that fails writes none of its rows; one whose claim is busy
    does not stop the others, and is raised once all were tried. Any other
    failure stops at once: Keeper or ClickHouse not answering one subject will
    not answer the next."""
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
    hold_repo: str | None,
) -> int:
    """`_write_under_claims` for one subject's rows. With `hold_repo` the rows hold
    `repo_admin_claims`, so a sync and a revoke in one repository run one after the
    other. Each row also holds its branch's admins claim, all taken before any grant
    claim: one that takes admin (a revoke or a lowering) so the branch's last admin
    is never taken, and a copy so that a restriction, written under that claim,
    lands before its classification is read or after its write."""
    from server.auth.grant_claims import admins_change, grant_change
    from server.core import grant_cache

    rows.sort(key=lambda r: r["namespace"])
    async with AsyncExitStack() as held:
        if hold_repo is not None:
            await held.enter_async_context(repo_admin_claims(org, hold_repo, subject))
        for row in rows:
            await held.enter_async_context(admins_change(org, row["namespace"]))
        for row in rows:
            await held.enter_async_context(grant_change(org, row["namespace"], subject))
        wanted = []
        for row in rows:
            ns = row["namespace"]
            now = await client.query_one(
                _GRANT_ROW_SQL, params={"org": org, "ns": ns, "sub": subject}
            )
            write = await still_wanted(client, org, row, now, seen)
            if write is not None:
                wanted.append(write)
        if wanted:
            try:
                await client.insert(
                    "loom.namespace_grants",
                    wanted,
                    settings={"async_insert": 0, "wait_for_async_insert": 1},
                )
            finally:
                await grant_cache.changed(org, [subject])
    return len(wanted)


def _plan_mirrors(
    org: str,
    rows: list[dict[str, Any]],
    read: list[str],
    targets: list[str],
    all_branches: list[str],
) -> list[dict[str, Any]]:
    """The admin rows that bring `targets` to the union of the active admins on
    the `read` branches. A mirror stands in for its source only when that source
    is none of the repository's `all_branches`, read or not."""
    # Grant changes are ordered by row version: `granted_at` keeps whole seconds,
    # so a lowering and a promotion within one would compare equal.
    active: dict[str, set[str]] = {ns: set() for ns in read}
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
        if _live_admin(r):
            active[ns].add(subject)
            granted_by = str(r.get("granted_by") or "")
            if not _is_copy(r):
                if subject not in union or version > union[subject][0]:
                    union[subject] = (version, r["granted_at"], ns)
            elif granted_by.removeprefix(GRANTED_BY_PREFIX) not in all_branches:
                # Its source branch is gone: the mirror stands in for it.
                if subject not in mirrored or version > mirrored[subject][0]:
                    mirrored[subject] = (version, r["granted_at"], ns)
        else:
            ended[(ns, subject)] = version
    for subject in union:
        mirrored.pop(subject, None)
    now = naive_utc_now()
    inserts: list[dict[str, Any]] = []
    for ns in targets:
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
    return inserts


async def sync_repo_maintainers(org: str, namespace: str, *, ch: Any | None = None) -> int:
    """Bring every branch of `namespace`'s repository to the union of the active
    admins of the branches that reach it (`_reaches`); a restricted branch added from
    the page (`_restricted`) takes the operator entries' admins only, and its stray
    copies (`_stray`) are revoked or rewritten. Returns the number of
    grant rows written; none while the self-service registry has never been
    read, when the branches added from the page are unknown: a grant made then
    is copied by the next admin grant or approval on a branch of the
    repository, or by a branch added to it."""
    from server.code.app.index.repo_registry import registry_read

    repo = repo_of(org, namespace)
    sources = _branches(org, repo) if repo is not None else {}
    branches = list(sources)
    if sources and not registry_read():
        if (org, namespace) not in _UNSYNCED:
            _UNSYNCED.add((org, namespace))
            log.warning(
                "repo maintainers: %s/%s not synced — the self-service registry is not read yet",
                org,
                namespace,
            )
        return 0
    if not branches:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    read_at, rows = await _read_grants(client, org, branches)
    restricted = await _restricted(client, org, sources)
    config = [ns for ns in branches if sources[ns] == "config"]
    inserts: list[dict[str, Any]] = []
    for kind in ("config", "registry"):
        targets = [ns for ns in branches if sources[ns] == kind and ns not in restricted]
        read = [ns for ns in branches if _reaches(sources[ns], kind)]
        if targets:
            inserts += _plan_mirrors(org, rows, read, targets, branches)
    by_key = {(str(r["namespace"]), str(r["subject"])): r for r in rows}
    for ns in sorted(restricted):
        kept = [
            r
            for r in rows
            # A stray admin copy does not count, so an operator admin's is rewritten.
            if not (r["namespace"] == ns and _live_admin(r) and _stray(r, config, by_key))
            # A page copy on an operator entry is no source for it.
            and not (r["namespace"] in config and _foreign(r, config))
        ]
        inserts += _plan_mirrors(org, kept, [*config, ns], [ns], branches)
    planned = {(r["namespace"], r["subject"]) for r in inserts}
    revokes, strays = [], set()
    for r in rows:
        key = (r["namespace"], r["subject"])
        if r["namespace"] in restricted and _stray(r, config, by_key):
            strays.add(key)
            if key not in planned:
                _log_stray(org, r)
                revokes.append(_revoke_row(org, r, read_at=read_at))
    rewrites = [r for r in inserts if (r["namespace"], r["subject"]) in strays]
    inserts = [r for r in inserts if (r["namespace"], r["subject"]) not in strays]
    written, busy = 0, None
    stages = ((revokes, _copy_unchanged), (rewrites, _rewrite_wanted), (inserts, _mirror_wanted))
    for planned_rows, still_wanted in stages:
        if not planned_rows:
            continue
        try:
            written += await _write_under_claims(
                client, org, planned_rows, still_wanted, rows, hold_repo=repo
            )
        except GrantBusyError as e:  # the later stages are still written; a later sync retries
            busy = busy or e
    if busy is not None:
        raise busy
    if written:
        log.info(
            "repo maintainers: %s/%s — %d admin grant(s) mirrored or revoked across %d branches",
            org,
            namespace,
            written,
            len(branches),
        )
    return written


def _stray(row: dict[str, Any], config: list[str], by_key: _Seen) -> bool:
    """On a restricted branch added from the page: a live copy that does not lead,
    copy by copy across the operator entries, to a grant given on one with at least
    its role. A mirror standing in for a gone source is written with its holder's
    name, so the name alone proves nothing."""
    if not (_live(row) and _is_copy(row)):
        return False
    visited: set[tuple[str, str]] = set()
    while _is_copy(row):
        if not _live(row) or _foreign(row, config):
            return True
        key = (str(row["granted_by"]).removeprefix(GRANTED_BY_PREFIX), str(row["subject"]))
        source = by_key.get(key)
        if key in visited or source is None or not _live(source):
            return True
        if grant_rank(str(row.get("role"))) > grant_rank(str(source.get("role"))):
            return True
        visited.add(key)
        row = source
    return False


def _log_stray(org: str, row: dict[str, Any]) -> None:
    log.warning(
        "repo maintainers: %s/%s plans to revoke %r — %s does not trace to an operator grant here",
        org,
        row["namespace"],
        row["subject"],
        row["granted_by"],
    )


def _revoke_row(org: str, row: dict[str, Any], *, read_at: int) -> dict[str, Any]:
    """A stray's revoke, versioned at `read_at`, when the rows it was planned from were
    read, or just above the stray if that is later: older than any grant made after
    the read, which the next sync then copies onto the branch."""
    return _row(
        org,
        str(row["namespace"]),
        str(row["subject"]),
        granted_by=str(row.get("granted_by") or ""),
        now=naive_utc_now(),
        revoked=True,
        role=str(row.get("role") or "admin"),
        version=max(read_at, int(row["version"]) + 1),
    )


def _given_on_added(
    subject: str, admins: dict[str, dict[str, dict[str, Any]]], sources: dict[str, str]
) -> bool:
    """`subject` holds an admin grant given (not mirrored) on a branch added from
    the page: such a grant reaches every branch added from the page, so an
    operator revoke leaves them all to it. A mirror whose page-added source was
    removed holds no such grant and is taken, like a mirror of an operator
    entry."""
    return any(
        sources.get(ns) == "registry" and not _is_copy(by_subject[subject])
        for ns, by_subject in admins.items()
        if subject in by_subject
    )


async def _is_code_namespace(client: Any, org: str, namespace: str) -> bool:
    """The directory row says `namespace` is a code index: a branch added from
    the page is one before the registry snapshot lists it."""
    from server.identity.app.registration import namespace_row

    with sequential_reads():  # a lagging replica would skip the fan-out
        row = await namespace_row(org, namespace, ch=client)
    return row is not None and str(row.get("kind") or "") == "code"


async def revoke_repo_maintainer(
    org: str,
    namespace: str,
    subject: str,
    *,
    repo: str | None,
    ch: Any | None = None,
    lowered_to: str | None = None,
) -> int:
    """Revoke `subject`'s active admin grant on the other branches of the
    repository the revoke reaches, or lower it to `lowered_to`; a branch where
    they are the only active admin keeps them (the same guard `grants.revoke`
    applies); on a restricted branch added from the page the next sync takes such a
    copy once it no longer traces to an operator grant (`_stray`).
    `repo` is what `read_repo` answered, and the caller holds
    `repo_admin_claims` on it.

    A revoke on a branch added from the page leaves the operator entries alone
    (`_reaches`); one on an operator entry takes off a branch added from the page
    only a mirror, and none from a standard one while the subject holds an admin
    grant given on a branch added from the page. On a restricted branch added from
    the page, a revoke takes a copy whose source is not an operator entry
    (`_foreign`), even as that branch's last admin, unless the subject keeps an
    admin grant given on an operator entry other than `namespace`, which the next
    sync copies over it; a revoke from an operator entry also takes an operator
    copy, under the guard.

    The registry is read again under the claims: a branch added after
    `read_repo` read it, and given a copy by a sync that held the claims
    first, is then among the branches. Raises `GrantBusyError` when that read
    fails, or when it puts `namespace` in another repository than the claims
    hold. Returns rows written."""
    from server.core.ch import get_client

    client = ch or get_client()
    if repo is not None:
        await _read_registry_now(client)
    if repo_of(org, namespace) != repo:
        raise GrantBusyError("the repository's branches changed; retry shortly")
    if repo is None:
        return 0
    sources = _branches(org, repo)
    own = sources[namespace]
    branches = [b for b, source in sources.items() if b != namespace and _reaches(own, source)]
    if not branches:
        return 0
    restricted = await _restricted(client, org, {b: sources[b] for b in branches})
    config = [b for b, source in sources.items() if source == "config"]
    # The operator entries a page revoke does not reach are read too, for `operator_admin`.
    read_at, rows = await _read_grants(client, org, list(dict.fromkeys([*branches, *config])))
    admins: dict[str, dict[str, dict[str, Any]]] = {ns: {} for ns in branches}
    for r in rows:
        if _live_admin(r) and r["namespace"] in admins:
            admins[str(r["namespace"])][str(r["subject"])] = r
    # An operator admin's stray copy is the sync's to rewrite as a copy of that grant.
    operator_admin = any(
        r["subject"] == subject
        and r["namespace"] in config
        and r["namespace"] != namespace
        and _live_admin(r)
        and not _is_copy(r)
        for r in rows
    )
    now = naive_utc_now()
    out: list[dict[str, Any]] = []
    strays: list[dict[str, Any]] = []
    # An admin grant given on a branch added from the page still reaches them all.
    keeps_added = own == "config" and _given_on_added(subject, admins, sources)
    by_key = {(str(r["namespace"]), str(r["subject"])): r for r in rows}
    for ns in branches:
        theirs = by_key.get((ns, subject))
        if ns in restricted and theirs is not None and _foreign(theirs, config):
            if not operator_admin:  # taken whatever the guard or the lowering
                _log_stray(org, theirs)
                strays.append(_revoke_row(org, theirs, read_at=read_at))
            continue
        mine = admins[ns].get(subject)
        if mine is None:
            continue
        if ns in restricted:
            if not _is_copy(mine):
                continue  # a grant given there stays
            if own == "registry":
                continue  # an operator copy, which a page revoke never reaches
        elif keeps_added and sources[ns] == "registry":
            continue
        if len(admins[ns]) <= 1:
            log.warning(
                "repo maintainers: %s/%s keeps %r — only admin of that branch", org, ns, subject
            )
            continue
        out.append(
            _row(
                org,
                ns,
                subject,
                granted_by=str(mine.get("granted_by") or ""),
                now=now,
                revoked=lowered_to is None,
                role=lowered_to or "admin",
            )
        )
    written = await _write_revokes(client, org, strays, out, rows)
    if written:
        log.info(
            "repo maintainers: %s/%s — %r %s on %d sibling branch(es)",
            org,
            namespace,
            subject,
            f"lowered to {lowered_to}" if lowered_to else "revoked",
            written,
        )
    return written


async def _write_revokes(
    client: Any,
    org: str,
    strays: list[dict[str, Any]],
    out: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> int:
    """A revoke's rows: the stray copies with no last-admin guard, then the rest with
    it. The caller holds `repo_admin_claims`."""
    written = 0
    if strays:
        written += await _write_under_claims(
            client, org, strays, _copy_unchanged, rows, hold_repo=None
        )
    if out:
        written += await _write_under_claims(client, org, out, _still_admin, rows, hold_repo=None)
    return written


async def sync_restricted_page_branches(
    entries: list[Any], *, exhausted: Callable[[], bool]
) -> None:
    """Sync, once each, the repositories among `entries` with a branch added from the
    page that is restricted, by this process's snapshot or by its directory row: the
    copies it must not hold go on every pass, even when the sync after its
    restriction failed. Stops once `exhausted` says the caller's budget is spent."""
    from server.core.ch import get_client

    page = [c for c in entries if c.source == "registry"]
    # A fresh order each pass: a budget spent part way does not leave the same
    # repositories unserved pass after pass.
    random.shuffle(page)
    done: set[tuple[str, str]] = set()
    for org in dict.fromkeys(c.org for c in page):
        if exhausted():
            return
        mine = [c for c in page if c.org == org]
        try:
            restricted = await _restricted(get_client(), org, {c.namespace: c.source for c in mine})
        except Exception as e:  # noqa: BLE001 — an unread directory waits for the next pass
            log.warning("repo maintainers: %s classification unreadable: %s", org, e)
            continue
        for cfg in mine:
            if exhausted():
                return
            if cfg.namespace in restricted and (org, cfg.repo) not in done:
                done.add((org, cfg.repo))
                await sync_quietly(org, cfg.namespace)


async def sync_quietly(org: str, namespace: str, *, ch: Any | None = None) -> None:
    """The fan-out is a consequence and never fails the op or pass that runs it: a
    failure is logged with its traceback."""
    try:
        await sync_repo_maintainers(org, namespace, ch=ch)
    except Exception:  # the fan-out never fails the op or pass that runs it
        log.warning("repo maintainers: sync failed for %s/%s", org, namespace, exc_info=True)
