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

Reads are cached for `TTL_S` per (org, subject): the auth context, the
login widening and the restricted gate each ask once per request.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.core.errors import TooManySpellingsError

log = logging.getLogger("loom.auth.aliases")

TTL_S = 60.0
_CACHE: dict[tuple[str, str], tuple[float, tuple[str, ...]]] = {}
_CACHE_MAX = 10_000
# Accounts a lookup reads per subject; a cutoff reads many more, and refuses
# rather than cut short when even that is not all of them.
_LOOKUP_SPELLINGS = 8
_CUTOFF_SPELLINGS = 1000


def _is_human_subject(subject: str) -> bool:
    return bool(subject) and subject != "*" and not subject.startswith("sa:")


async def subject_aliases(
    org: str, subject: str, *, ch: object | None = None, strict: bool = False
) -> list[str]:
    """`subject` first, then the other spelling(s) the directory ties to it;
    the subject alone when the read fails or nothing is tied to it.

    `strict` reads the directory now and raises when it cannot, for a caller
    whose answer persists (an OAuth refresh's grant read). A grant cutoff
    reads `cutoff_spellings` instead."""
    if not _is_human_subject(subject):
        return [subject] if subject else []
    key = (org, subject)
    hit = _CACHE.get(key)
    now = time.monotonic()
    if hit is not None and not strict and now - hit[0] < TTL_S:
        return list(hit[1])
    try:
        out = await _read_spellings(org, subject, ch, for_cutoff=False)
    except Exception:
        if strict:
            raise
        log.warning(
            "aliases: lookup failed for %r in %r; subject stands alone", subject, org, exc_info=True
        )
        return [subject]
    if len(_CACHE) >= _CACHE_MAX:
        _CACHE.clear()
    _CACHE[key] = (now, tuple(out))
    return out


async def cutoff_spellings(org: str, subject: str, *, ch: object | None = None) -> list[str]:
    """Every spelling a grant cutoff for `subject` must cover: read now, never
    cached, raising when the directory cannot answer, and counting accounts
    since deactivated, whose other spelling may still be in a token."""
    if not _is_human_subject(subject):
        return [subject] if subject else []
    return await _read_spellings(org, subject, ch, for_cutoff=True)


async def _read_spellings(
    org: str, subject: str, ch: object | None, *, for_cutoff: bool
) -> list[str]:
    """The spellings tied to `subject`. A cutoff must reach all of them,
    inactive accounts' too, so it reads up to `_CUTOFF_SPELLINGS` and raises
    past that rather than cut short."""
    from server.core.ch import get_client

    limit = _CUTOFF_SPELLINGS if for_cutoff else _LOOKUP_SPELLINGS
    # A cutoff reads one row more than it may use, to see that it has them all.
    fetch = limit + 1 if for_cutoff else limit
    client = ch or get_client()
    rows = await client.query_all(  # type: ignore[attr-defined]
        """
        SELECT DISTINCT owner_email, contact_email
        FROM loom.namespaces FINAL
        WHERE org = {org:String} AND ({all:Bool} OR ifNull(active, true) = true)
          AND contact_email != ''
          AND (owner_email = {sub:String} OR lower(contact_email) = lower({sub:String}))
        LIMIT {lim:UInt32}
        """,
        params={
            "org": org,
            "sub": subject,
            "all": for_cutoff,
            "lim": fetch,
        },
    )
    if for_cutoff and len(rows) > limit:
        log.error(
            "aliases: more than %d accounts name %r in %r; release the extra ones "
            "with `loom claim-release --org %s --namespace <account>`",
            limit,
            subject,
            org,
            org,
        )
        raise TooManySpellingsError(
            f"too many accounts name {subject!r} in {org!r}; ask your Loom operator"
        )
    out, seen = [subject], {subject.lower()}
    for r in rows:
        for spelling in (str(r.get("owner_email") or ""), str(r.get("contact_email") or "")):
            if spelling and spelling.lower() not in seen:
                out.append(spelling)
                seen.add(spelling.lower())
    return out


async def canonical_subject(org: str, subject: str, *, ch: object | None = None) -> str:
    """The login handle for a human spelling (the row's `owner_email`), so a
    new grant is written under the one subject every read folds onto; the
    spelling as given when the directory ties nothing to it."""
    if not _is_human_subject(subject) or "@" not in subject:
        return subject
    for alias in await subject_aliases(org, subject, ch=ch):
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


__all__ = ["TTL_S", "canonical_subject", "fold_by_alias", "subject_aliases"]
