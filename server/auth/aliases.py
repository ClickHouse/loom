"""One person is one principal.

A human reaches loom under two subjects: the login token names the
account's `owner_email` (the login handle), while grants written by
operators, bootstrap scripts and invitations name the person's email — the
same row's `contact_email`. An exact-subject grant read would refuse a
person granted under one spelling when they arrive under the other, and
list the same human twice as a namespace's readers.

`subject_aliases(org, subject)` returns the subject and every spelling the
directory ties to the same account: the row whose `owner_email` is the
subject gives its `contact_email`, the row whose `contact_email` is the
subject gives its `owner_email`. Only rows whose email is verified take part
(`VERIFIED_EMAIL_SQL`): an SSO account's, or one linked to the address. A
self-signup email is unverified, so it admits nothing: a grant written under an
address before its owner signs in is not handed to whoever typed it first.
Service-account subjects (`sa:` prefix) and wildcards have no aliases. The
gate's grant map (`server.core.grant_cache`) and the `grants.list` fold
(`server.identity.store.grants`) read the same tie in SQL. Where reaching
further fails closed, an unverified pair still ties: the grant map reads it
to cap a claim, never to admit one, and a revocation (`spellings`) reaches it.

Reads are cached per (org, subject) for `grant_cache_ttl_seconds`, the bound
other processes see a grant change within: the auth context, the login
widening and the restricted gate each ask once per request. A change to a
row's spellings calls `invalidate`, and a link or an unlink of an email
`invalidate_org`; each clears this process's entries only.
`canonical_subject`, which picks the subject a grant is written under, reads
the directory itself.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterable
from typing import Any

from server.auth.fail_closed import note_unreadable
from server.core.grant_sql import VERIFIED_EMAIL_SQL
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
        out = await read_aliases(org, subject, ch)
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


async def _rows(
    org: str, subject: str, ch: object | None, *, verified: bool = True
) -> list[dict[str, Any]]:
    """The accounts tying `subject` to another spelling, matched in any case:
    by a verified email only, unless `verified` is False."""
    from server.core.ch import get_client

    client = ch or get_client()
    only_verified = f"AND {VERIFIED_EMAIL_SQL}" if verified else ""
    rows: list[dict[str, Any]] = await client.query_all(  # type: ignore[attr-defined]
        f"""
        SELECT owner_email, contact_email
        FROM loom.namespaces FINAL
        WHERE org = {{org:String}} AND ifNull(active, true) = true
          AND contact_email != '' {only_verified}
          AND (lower({{sub:String}}) = lower(owner_email)
               OR lower({{sub:String}}) = lower(contact_email))
        LIMIT 8
        """,
        params={"org": org, "sub": subject},
    )
    return rows


async def read_aliases(org: str, subject: str, ch: object | None) -> list[str]:
    """`subject` and the spellings a verified email ties to it, read uncached;
    raises when the directory cannot be read (`subject_aliases` caches, and
    answers the subject alone on a failed read)."""
    rows = await _rows(org, subject, ch)
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


def invalidate_org(org: str) -> None:
    """Drop every cached read of `org`: a link or an unlink changed which of
    its accounts' emails are verified, and a link's owner may not be among
    the spellings a cached read found."""
    for key in [k for k in _CACHE if k[0] == org]:
        del _CACHE[key]


async def spellings(org: str, subject: str, *, ch: object | None = None) -> list[str]:
    """Every spelling of `subject` a change keyed by exact spelling must reach
    (a revocation): itself, an email's lower case, and each spelling the
    directory ties to it, as stored, an unverified email too: a revocation
    reaching further fails closed. Read uncached, and raising when the
    directory cannot be read: never guess."""
    from server.core.ch import own_write_reads

    if not subject:
        return []
    out = [subject]
    if "@" in subject and subject.lower() != subject:
        out.append(subject.lower())  # how the directory keeps an address
    if not has_aliases(subject):
        return out
    with own_write_reads():
        rows = await _rows(org, subject, ch, verified=False)
    for r in rows:
        for spelling in (str(r.get("owner_email") or ""), str(r.get("contact_email") or "")):
            if spelling and spelling not in out:
                out.append(spelling)
    return out


async def canonical_subject(org: str, subject: str, *, ch: object | None = None) -> str:
    """The login handle for a human spelling (the row's `owner_email`), so a
    new grant is written under the one subject every read folds onto; the
    spelling as given when the directory ties nothing to it. Read uncached,
    so a link or release another process just made cannot strand the grant;
    a failed read raises rather than guess."""
    if "@" not in subject:
        return subject
    for alias in await verified_spellings(org, subject, ch=ch):
        if "@" not in alias:
            return alias
    return subject


async def verified_spellings(org: str, subject: str, *, ch: object | None = None) -> list[str]:
    """`read_aliases` read past a link or release another process just made: the
    subject alone when it has no aliases. Raises when the directory cannot be read."""
    from server.core.ch import own_write_reads

    if not has_aliases(subject):
        return [subject]
    with own_write_reads():
        return await read_aliases(org, subject, ch)


__all__ = [
    "canonical_subject",
    "invalidate",
    "invalidate_org",
    "read_aliases",
    "spellings",
    "subject_aliases",
    "verified_spellings",
]
