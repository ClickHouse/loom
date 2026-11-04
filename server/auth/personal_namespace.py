"""Personal-namespace auto-provisioning.

Every authenticated human caller gets a ``personal-<slug>`` namespace that
is unambiguously theirs. This module owns the slug rules and the idempotent
"create if missing" path, with a process-local cache so the hot path is one
set-membership check.

Runs from the HTTP auth pre-step after each successful JWT verify, so
federated callers get a personal namespace without touching the dashboard.
Best-effort: failures are logged, never raised, and the cache is not
populated on failure so the next request retries.
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Final

log = logging.getLogger(__name__)

_seen: Final[set[tuple[str, str]]] = set()
_MAX_CACHE = 100_000
# The claim outlives the read and the two inserts; a dead holder's is taken over.
CLAIM_TTL_S = 30


class PersonalNamespaceBusy(Exception):
    """Another process is provisioning this name right now; the next request retries."""


def derive_personal_slug(sub: str) -> str:
    """Build the candidate ``personal-<slug>`` name for a JWT subject.

    A subject already starting with ``personal-`` is returned as-is (the
    self-signup path, where subject equals the chosen namespace). Otherwise
    the email local-part is slugified to ``[a-z0-9-]``, falling back to a
    sha1 prefix if slugification empties it.
    """
    from server.core.naming import slugify

    if sub.startswith("personal-"):
        return sub
    local = sub.split("@", 1)[0] if "@" in sub else sub
    slug = slugify(local, max_len=55) or hashlib.sha1(sub.encode("utf-8")).hexdigest()[:8]
    return f"personal-{slug}"


def personal_candidates(sub: str) -> list[str]:
    """The names `ensure_personal_namespace` tries for `sub`, in order."""
    base = derive_personal_slug(sub)
    if sub.startswith("personal-"):
        return [base]
    return [base, f"{base}-{hashlib.sha1(sub.encode('utf-8')).hexdigest()[:6]}"]


async def ensure_personal_namespace(
    ch: Any,
    org: str,
    sub: str,
) -> str | None:
    """Idempotently ensure ``sub`` has a personal namespace + admin grant.

    Picks ``personal-<slug>`` from :func:`derive_personal_slug`. If that
    name is taken by a different owner (two ``bob@*.com`` callers fighting
    for ``personal-bob``), falls back to ``personal-<slug>-<sha6>`` so
    the second caller still gets a namespace.

    Returns the resolved namespace name on success (whether created or
    pre-existing), ``None`` only in the pathological case where every
    candidate is owned by someone else. Raises PersonalNamespaceBusy while
    another process holds a candidate's claim.
    """
    from server.core import leases

    for cand in personal_candidates(sub):
        # One claimant per name across replicas; while another holds it (likely
        # this subject's own concurrent first request) nothing is decided here.
        async with leases.holding(f"personal-ns:{org}:{cand}", ttl_s=CLAIM_TTL_S, ch=ch) as held:
            if not held:
                raise PersonalNamespaceBusy(cand)
            owner = await namespace_owner(ch, org, cand)
            if owner is None:
                from server.auth.self_signup import provision_account

                await provision_account(
                    org=org,
                    namespace=cand,
                    subject=sub,
                    contact_email="",
                    description="Auto-claimed personal namespace.",
                    ch=ch,
                )
                log.info(
                    "auto-claimed personal namespace %s/%s for subject=%s",
                    org,
                    cand,
                    sub,
                )
                return cand
        if owner == sub:
            return cand
    return None


async def namespace_owner(ch: Any, org: str, namespace: str) -> str | None:
    """Who owns `namespace` (its row's `owner_email`), None with no row."""
    from server.core.ch import sequential_reads

    with sequential_reads():  # a row another replica just provisioned
        row = await ch.query_one(
            """
            SELECT toString(ifNull(owner_email, '')) AS owner
            FROM loom.namespaces FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
            LIMIT 1
            """,
            params={"org": org, "ns": namespace},
        )
    return None if row is None else str(row.get("owner") or "")


def provisioning_pending(org: str, subject: str) -> bool:
    """Whether the next `ensure_personal_namespace_cached` call for this pair
    reads and may provision; False once this process has resolved it."""
    return (org, subject) not in _seen


async def ensure_personal_namespace_cached(
    *,
    org: str,
    subject: str,
    is_service_account: bool,
) -> None:
    """Cached, best-effort entrypoint used by the HTTP auth pre-step.

    Skips service accounts, wildcards, and empty subjects. Also skips
    when the subject already owns any namespace in this org — a direct
    self-signup user (who picked their own name) doesn't need a
    redundant ``personal-<sub>`` alongside it. The personal-namespace
    auto-claim is a federation-only safety net: it fills the gap for
    subjects whose JWT arrives from an upstream minter without a
    matching grants row on file.

    Caches each ``(org, subject)`` pair after the first resolution so
    the hot path is one set-membership check.

    Errors are swallowed and logged — provisioning is opportunistic; if
    ClickHouse is unhappy, the request still goes through and the next
    request retries.
    """
    if is_service_account:
        return
    if not subject or subject == "*" or not org:
        return
    key = (org, subject)
    if key in _seen:
        return
    if len(_seen) >= _MAX_CACHE:
        # Forget, don't stop: a full cache must not end provisioning for every
        # new subject. The next call per subject re-reads one grant row.
        _seen.clear()

    try:
        from server.auth.aliases import subject_aliases
        from server.core.ch import get_client
        from server.core.grant_sql import LIVE_GRANT

        ch = get_client()
        # Any spelling of the person, in any case, counts, as it does at the
        # gate: a person granted under their verified email already has a namespace.
        subjects = [s.lower() for s in await subject_aliases(org, subject, ch=ch)]
        owned = await ch.query_one(
            """
            SELECT 1 FROM loom.namespace_grants FINAL
            WHERE org = {org:String}
              AND lower(subject) IN {subs:Array(String)}
              AND {live}
            LIMIT 1
            """.replace("{live}", LIVE_GRANT),
            params={"org": org, "subs": subjects},
        )
        if owned is None:
            await ensure_personal_namespace(ch, org, subject)
        _seen.add(key)
    except PersonalNamespaceBusy:
        return  # another request is provisioning it; not cached, so the next one checks
    except Exception:
        log.warning(
            "ensure_personal_namespace_cached failed for org=%s subject=%s",
            org,
            subject,
            exc_info=True,
        )
