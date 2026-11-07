"""A repository's maintainers flow across its branches.

Every branch of a repository is its own namespace with its own grant rows, so an admin
grant on ``code-x`` would say nothing about ``code-x-26-5`` and a maintainer would have
to ask branch by branch. This module copies *active admin* grants between the
configured branch namespaces of one repository, in one direction (``_reaches``): the
operator's entries pass their admins to every branch, and a branch added from the
page (``code.repo.add``, open to any org member) passes its admins only to other
branches added from the page. Registering a branch of a repository the operator
configures gives no grant on the operator's namespaces.

* ``sync_repo_maintainers`` — every branch gets the union of the active admins
  of the branches that reach it, written where it lacks one (``granted_by =
  repo-maintainers:<source namespace>``), from the admin grants given on a
  branch, not mirrored there; a subject whose grant was given on a branch no
  longer configured is mirrored from a mirror of it onto the branches they have
  no row on. A revocation or a lower role set on a branch after
  the subject's newest such grant is respected, and so is a request made there
  since: admin is not re-added. An older row of any kind is replaced. Called
  after an admin grant is created or approved on a code namespace, when a branch
  is added, and by the registrar on each pass until the self-service registry
  is read, then once per process per namespace (so environment-registered
  branches inherit).
* ``revoke_repo_maintainer`` — a revoked admin leaves the branches the revoke
  reaches, except a branch where they are the only active admin (the handler's
  own guard). From an operator entry it takes off a branch added from the page
  only a mirror, and none while the subject holds an admin grant given on one.

Readers stay per branch: the approval fan-out already turns a person's branch requests
into one decision. Restricted branches also need the token to name them — a person's
next login does that; the rows written here are the direct grants that check reads.
"""

from __future__ import annotations

import logging
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

from server.core.datetimes import naive_utc_now
from server.core.errors import GrantBusyError
from server.core.grant_sql import LIVE_GRANT

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

    return grant_change(org, _repo_key(repo), subject) if repo is not None else nullcontext()


def _repo_key(repo: str) -> str:
    """The claim name of a repository: a sync and a revoke serialise on it."""
    return f"repo:{repo}"


@asynccontextmanager
async def repo_admin_claims(org: str, repo: str | None, subject: str) -> AsyncIterator[None]:
    """Hold what a change that takes a maintainer off the repository's other branches
    needs: `subject`'s maintainer grants there, then the repository's admins,
    from its own last-admin count to its last write, so two such changes in
    one repository run one after the other. Taken before any namespace's
    admins claim. A no-op when `repo` is None."""
    from server.auth.grant_claims import admins_change

    if repo is None:
        yield
        return
    async with repo_change(org, repo, subject), admins_change(org, _repo_key(repo)):
        yield


async def _read_registry_now(client: Any) -> None:
    """Read the self-service registry now, consistent with every write made
    before it on any replica. A revoke that skipped a branch added from the
    page would leave its copies live, and no later sync removes them, so a
    failed read raises `GrantBusyError`."""
    from server.code.app.index.repo_registry import read_registry
    from server.core.ch import sequential_reads

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


# A branch's live admins: the code plane reads grants itself, not through the identity store.
_ADMINS_SQL = (
    "SELECT count() AS n FROM loom.namespace_grants FINAL WHERE org = {org:String}"
    " AND namespace = {ns:String} AND role = 'admin' AND " + LIVE_GRANT
)


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
    was planned against (one granted again since is newer and stays) and is
    not the branch's only admin, counted under its admins claim."""
    read = seen.get((row["namespace"], row["subject"]))
    if read is None or not _live_admin(now) or _version(now) != _version(read):
        return False
    admins = await client.query_one(_ADMINS_SQL, params={"org": org, "ns": row["namespace"]})
    return int((admins or {}).get("n") or 0) > 1


# (client, org, planned row, current row, the plan's rows) -> write it?
_Wanted = Callable[[Any, str, dict[str, Any], dict[str, Any] | None, _Seen], Awaitable[bool]]


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
    holding the claim of the repository `hold_repo` names (None: the caller holds it)
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
    hold_repo: str | None,
) -> int:
    """`_write_under_claims` for one subject's rows. A row that takes admin (a
    revoke or a lowering) also holds its branch's admins claim, so the branch's
    last admin is never taken."""
    from server.auth.grant_claims import admins_change, grant_change
    from server.core import grant_cache

    rows.sort(key=lambda r: r["namespace"])
    async with AsyncExitStack() as held:
        if hold_repo is not None:
            await held.enter_async_context(repo_change(org, hold_repo, subject))
        for row in rows:
            if row["role"] != "admin" or row.get("revoked_at") is not None:  # takes admin
                await held.enter_async_context(admins_change(org, row["namespace"]))
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
        live = r.get("revoked_at") is None and str(r.get("status") or "active") in _ACTIVE
        if live and r.get("role") == "admin":
            active[ns].add(subject)
            granted_by = str(r.get("granted_by") or "")
            if not granted_by.startswith(GRANTED_BY_PREFIX):
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
    admins of the branches that reach it (`_reaches`). Returns the number of
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
    if len(branches) < 2:
        return 0
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await _grant_rows(client, org, branches)
    inserts: list[dict[str, Any]] = []
    for kind in ("config", "registry"):
        targets = [ns for ns in branches if sources[ns] == kind]
        read = [ns for ns in branches if _reaches(sources[ns], kind)]
        if targets:
            inserts += _plan_mirrors(org, rows, read, targets, branches)
    written = (
        await _write_under_claims(client, org, inserts, _mirror_wanted, rows, hold_repo=repo)
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


def _given_on_added(
    subject: str, admins: dict[str, dict[str, dict[str, Any]]], sources: dict[str, str]
) -> bool:
    """`subject` holds an admin grant given (not mirrored) on a branch added from
    the page: such a grant reaches every branch added from the page, so an
    operator revoke leaves them all to it. A mirror whose page-added source was
    removed holds no such grant and is taken, like a mirror of an operator
    entry."""
    return any(
        sources.get(ns) == "registry"
        and not str(by_subject[subject].get("granted_by") or "").startswith(GRANTED_BY_PREFIX)
        for ns, by_subject in admins.items()
        if subject in by_subject
    )


async def _is_code_namespace(client: Any, org: str, namespace: str) -> bool:
    """The directory row says `namespace` is a code index: a branch added from
    the page is one before the registry snapshot lists it."""
    from server.core.ch import sequential_reads
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
    applies). `repo` is what `read_repo` answered, and the caller holds
    `repo_admin_claims` on it.

    A revoke on a branch added from the page leaves the operator entries alone
    (`_reaches`); one on an operator entry takes off a branch added from the
    page only a mirror, and none while the subject holds an admin grant given
    on a branch added from the page.

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
        if (
            own == "config"
            and sources[ns] == "registry"
            and _given_on_added(subject, admins, sources)
        ):
            continue  # a grant given on a branch added from the page still reaches it
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
                revoked=lowered_to is None,
                role=lowered_to or "admin",
            )
        )
    written = (
        await _write_under_claims(client, org, out, _still_admin, rows, hold_repo=None)
        if out
        else 0
    )
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


async def sync_quietly(org: str, namespace: str, *, ch: Any | None = None) -> None:
    """The fan-out is a consequence, never a reason for the grant op to fail."""
    try:
        await sync_repo_maintainers(org, namespace, ch=ch)
    except Exception:  # the fan-out never fails the op that caused it
        log.warning("repo maintainers: sync failed for %s/%s", org, namespace, exc_info=True)
