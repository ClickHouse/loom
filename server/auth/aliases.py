"""One person is one principal.

A human reaches loom under two subjects: the login token names the
self-signup row's `owner_email` (the login handle), while grants written by
operators, bootstrap scripts and invitations name the person's email — the
same row's `contact_email`, the credential the password login itself matches
on. An exact-subject grant read would refuse a person granted under one
spelling when they arrive under the other, and list the same human twice as
a namespace's readers.

`subject_aliases(org, subject)` returns the subject and every spelling the
directory ties to the same self-signup row: the row whose `owner_email` is
the subject gives its `contact_email`, the row whose `contact_email` is the
subject gives its `owner_email`. Only rows a person signed up (a non-empty
`contact_email`) take part; the pair is the person's own login identity —
`self_signup` refuses a second account for an email already used — so a
grant under either spelling is a grant to that person and nobody else.
Service-account subjects (`sa:` prefix) and wildcards have no aliases.

Reads are cached per (org, subject) for `grant_cache_ttl_seconds`, the bound
other processes see a grant change within: the auth context, the login
widening and the restricted gate each ask once per request. A change to a
row's spellings calls `invalidate`, which clears this process's entries only.
`canonical_subject`, which picks the subject a grant is written under, reads
the directory itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from server.auth.fail_closed import note_unreadable
from server.core.principals import has_aliases

log = logging.getLogger("loom.auth.aliases")

_CACHE: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_CACHE_MAX = 10_000


async def subject_aliases(org: str, subject: str, *, ch: object | None = None) -> list[str]:
    """`subject` first, then the other spelling(s) the directory ties to it;
    the subject alone when the read fails or nothing is tied to it."""
    from server.settings import settings

    if not has_aliases(subject):
        return [subject] if subject else []
    key = (org, subject)
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit is not None and now - hit[0] < settings.grant_cache_ttl_seconds:
        return list(hit[1])
    try:
        out = await _read(org, subject, ch)
    except Exception:
        log.warning(
            "aliases: lookup failed for %r in %r; subject stands alone", subject, org, exc_info=True
        )
        note_unreadable()
        return [subject]
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now, tuple(out))
    return out


async def _read(org: str, subject: str, ch: object | None) -> list[str]:
    from server.core.ch import get_client

    client = ch or get_client()
    rows = await client.query_all(  # type: ignore[attr-defined]
        """
        SELECT owner_email, contact_email
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND ifNull(active, true) = true
          AND contact_email != ''
          AND lower({sub:String}) IN (lower(owner_email), lower(contact_email))
        LIMIT 8
        """,
        params={"org": org, "sub": subject},
    )
    out = [subject]
    for r in rows:
        for spelling in (str(r.get("owner_email") or ""), str(r.get("contact_email") or "")):
            if spelling and spelling.lower() not in {s.lower() for s in out}:
                out.append(spelling)
    return out


def invalidate(org: str, subjects: Iterable[str]) -> None:
    """Drop the cached reads of `org` that asked about, or found, one of
    `subjects`: a row naming them changed."""
    lowered = {s.lower() for s in subjects if s}
    for key, (_, spellings) in list(_CACHE.items()):
        if key[0] == org and lowered & {s.lower() for s in (key[1], *spellings)}:
            del _CACHE[key]


async def canonical_subject(org: str, subject: str, *, ch: object | None = None) -> str:
    """The login handle for a human spelling (the row's `owner_email`), so a
    new grant is written under the one subject every read folds onto; the
    spelling as given when the directory ties nothing to it. Read uncached,
    so a link or release another process just made cannot strand the grant;
    a failed read raises rather than guess."""
    from server.core.ch import own_write_reads

    if not has_aliases(subject) or "@" not in subject:
        return subject
    with own_write_reads():
        spellings = await _read(org, subject, ch)
    for alias in spellings:
        if "@" not in alias:
            return alias
    return subject


def fold_by_alias(
    rows: list[dict[str, Any]],
    aliases_of: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Grant rows folded so one person is one row: rows whose subjects alias
    one another collapse onto the first (the highest role wins, the other
    spellings ride in `also`); pure, for the Readers list."""
    from server.auth.authz import grant_rank

    groups: list[dict[str, Any]] = []
    seen: dict[str, dict[str, Any]] = {}
    for r in rows:
        subject = str(r.get("subject") or "")
        names = {subject.lower(), *(a.lower() for a in aliases_of.get(subject, []))}
        target = next((seen[n] for n in names if n in seen), None)
        if target is None:
            target = dict(r)
            target["also"] = []
            groups.append(target)
        else:
            if grant_rank(str(r.get("role"))) > grant_rank(str(target.get("role"))):
                target["role"] = r.get("role")
            head = str(target.get("subject") or "").lower()
            if subject.lower() != head and subject not in target["also"]:
                target["also"].append(subject)
        for n in names:
            seen[n] = target
    return groups


__all__ = ["canonical_subject", "fold_by_alias", "invalidate", "subject_aliases"]
