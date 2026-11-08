"""Namespace classification — the narrowing axis.

`loom.namespaces.visibility` only ever widens access (`public` = every
principal of the org). `classification = 'restricted'` says the opposite:
no default applies, and a read needs BOTH a token that names the namespace
explicitly AND a live direct grant (server/auth/authz.py, `_check_restricted`).

Three inputs, restricted wins:

* the code-plane repo config (`RepoConfig.classification`, no I/O) — so the
  private repository's index is restricted even if its directory row is lost
  or reset;
* the tracker sources (a configured namespace's `restricted`, no I/O) — so a
  tracker namespace is restricted before its directory row is narrowed;
* the directory row (`loom.namespaces.classification`) — so any namespace of
  any kind (a tracker mirror, a memory namespace) can be
  restricted by the operator.

**This IS an authorization input**, unlike `namespace_visibility`, and it
fails CLOSED: one statement per org, cached for `_TTL_S`; on a ClickHouse
error the last good snapshot for that org is served; with no snapshot the
lookup raises `ClassificationUnavailable`, and the authorization gate denies.
A stale snapshot can only delay a *new* restriction by `_TTL_S`; it can never
widen access on a namespace already restricted, and the config input is
never stale.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from server.auth.directory import ClassificationUnavailable

if TYPE_CHECKING:
    from server.auth.jwt import AuthInfo

log = logging.getLogger(__name__)

RESTRICTED = "restricted"
STANDARD = "standard"
CLASSIFICATIONS = (STANDARD, RESTRICTED)

# Shorter than the visibility cache: a newly restricted namespace should be
# enforced within half a minute on every process.
_TTL_S = 30.0

# org -> (expires_at_monotonic, frozenset of restricted namespaces)
_cache: dict[str, tuple[float, frozenset[str]]] = {}


def reset() -> None:
    """Drop every entry. For tests and an operator-facing cache flush."""
    _cache.clear()


def note_restricted(org: str, namespace: str) -> None:
    """Add `namespace` to the org's snapshot right away — called by the
    writers that classify a row so this process enforces without waiting
    out the TTL."""
    hit = _cache.get(org)
    if hit is None:
        return
    _cache[org] = (hit[0], hit[1] | {namespace})


def config_classification(org: str, namespace: str) -> str:
    """Classification from the code-plane repo config or the tracker sources,
    `standard` when the namespace is neither a configured code entry nor a restricted
    tracker namespace. No I/O."""
    from server.code.domain.index.repos import repo_for_namespace
    from server.tracker.domain.namespaces import tracker_namespace

    tracker = tracker_namespace(org, namespace)
    if tracker is not None and tracker.restricted:
        return RESTRICTED
    cfg = repo_for_namespace(org, namespace)
    if cfg is None:
        return STANDARD
    return str(getattr(cfg, "classification", STANDARD) or STANDARD)


async def restricted_set(org: str) -> frozenset[str]:
    """Namespaces of `org` whose directory row says `restricted`."""
    now = time.monotonic()
    hit = _cache.get(org)
    if hit is not None and hit[0] > now:
        return hit[1]

    from server.core.ch import get_client

    client = get_client()
    if getattr(client, "_client", "absent") is None:
        # A process that never connected (a unit test, an offline CLI) has no
        # directory and therefore no directory-restricted namespaces. Only a
        # connection that FAILS is an outage; see the module docstring.
        return frozenset()
    try:
        rows = await client.query_all(
            """
            SELECT namespace
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND classification = 'restricted'
            """,
            params={"org": org},
        )
    except Exception as e:
        if hit is not None:
            log.warning(
                "namespace classification lookup failed for org %s; serving the "
                "last good snapshot (%d restricted): %s",
                org,
                len(hit[1]),
                e,
            )
            return hit[1]
        log.error("namespace classification lookup failed for org %s with no snapshot: %s", org, e)
        raise ClassificationUnavailable(str(e)) from e

    names = frozenset(str(r["namespace"]) for r in rows if r.get("namespace"))
    _cache[org] = (time.monotonic() + _TTL_S, names)
    return names


def is_restricted_cached(org: str, namespace: str) -> bool:
    """Synchronous, no I/O: the config input plus the org's cached snapshot
    (stale or not). For the writers that run AFTER the authorization gate in
    the same request — the trace row, the argument projection, the repo
    listing — the gate has already populated the snapshot, so this answers
    what the gate answered. With no snapshot it says False: those writers
    only reduce what is recorded and never widen a read."""
    if config_classification(org, namespace) == RESTRICTED:
        return True
    hit = _cache.get(org)
    return hit is not None and namespace in hit[1]


async def is_restricted(org: str, namespace: str) -> bool:
    """True when either input classifies (org, namespace) as restricted.
    Raises `ClassificationUnavailable` when the directory cannot be read
    and no snapshot exists — the caller denies."""
    if config_classification(org, namespace) == RESTRICTED:
        return True
    return namespace in await restricted_set(org)


async def may_administer(
    info: AuthInfo, org: str, namespace: str, *, row: dict[str, Any] | None
) -> bool:
    """Whether `info` may make or unmake admins of a namespace (claim release,
    adoption): for a restricted one, only as the restricted gate admits it at
    admin. `row`, the directory row the caller read, restricts too, since it may
    be newer than the classification snapshot. An unreadable classification
    refuses. These routes take no delegated token (no org role is delegated)."""
    from server.auth.authz import Role, restricted_allows

    try:
        restricted = await is_restricted(org, namespace)
    except ClassificationUnavailable:
        return False
    if not restricted:
        if row is None or row.get("classification") != RESTRICTED:
            return True
        note_restricted(org, namespace)  # the row is newer: the audit row reads the snapshot
    return await restricted_allows(info, Role.ADMIN, org, namespace)


__all__ = [
    "CLASSIFICATIONS",
    "RESTRICTED",
    "STANDARD",
    "config_classification",
    "is_restricted",
    "is_restricted_cached",
    "may_administer",
    "note_restricted",
    "reset",
    "restricted_set",
]
