"""Canonical-person crawl erasure orders raw stores and verifies completed scope cleanup."""

from __future__ import annotations

from typing import Any

from server.auth import person_erasure, source_registration
from server.clients._vendor import CredentialRefused
from server.core.ch import set_query_tag
from server.erasure.store import crawl_controls, crawl_person


async def erase_source(
    pending: person_erasure.Pending,
    person: person_erasure.Person,
    namespace: str,
    source_id: str,
    contacts: frozenset[str],
    *,
    ch: Any,
) -> None:
    """The knowledge caller retains its org exclusion fence and exact source lease."""
    set_query_tag("erasure.crawl_person.source")
    allowed = frozenset(
        {
            *person.emails,
            *(
                email
                for w in pending.captured.workspaces
                if (w.org, w.subject) == (person.org, person.subject)
                for email in w.emails
            ),
        }
    )
    if person not in pending.captured.people or contacts != allowed:
        raise person_erasure.unavailable()
    await pending.validate(person.org)
    for encoded in await crawl_controls.approval_history(person.org, namespace, source_id, ch=ch):
        if source_registration.names_person(
            encoded,
            org=person.org,
            namespace=namespace,
            source_id=source_id,
            spellings=person.spellings,
        ):
            await crawl_controls.retire_approval(person.org, namespace, source_id, encoded, ch=ch)
    copies = await crawl_person.namespaces(person.org, source_id, ch=ch)
    for mirror in copies:
        # A complete pre-erasure listing cannot admit a post-erasure union.
        await crawl_controls.invalidate_listing(person.org, mirror, source_id, ch=ch)
        await crawl_person.scrub(person.org, mirror, source_id, contacts, ch=ch)
        await crawl_controls.memberships(person.org, mirror, source_id, contacts, ch=ch)
        await pending.validate(person.org)
    await crawl_controls.controls(
        person.org,
        namespace,
        source_id,
        person.subject,
        contacts,
        spellings=person.spellings,
        ch=ch,
    )
    await pending.validate(person.org)
    # Removing a listing-only namespace may shrink discovery; newly appeared copies
    # must not be reported erased without another complete guarded pass.
    remaining = await crawl_person.namespaces(person.org, source_id, ch=ch)
    if not set(remaining).issubset(copies):
        raise CredentialRefused("crawl person copies changed during cleanup")


async def finalize(pending: person_erasure.Pending) -> None:
    """The SCIM caller holds its user lease after all exact-person cleanup succeeded."""
    set_query_tag("erasure.crawl_person.capture")
    await pending.validate()
    history = await crawl_controls.capsule_history(pending.captured.id)
    if pending.encoded not in history:
        raise CredentialRefused("crawl person capture history unavailable")
    for encoded in history:
        previous = person_erasure.load(pending.captured.id, encoded)
        if any(
            not any(
                (old.org, old.subject) == (new.org, new.subject)
                and set(old.emails).issubset(new.emails)
                and set(old.spellings).issubset(new.spellings)
                for new in pending.captured.people
            )
            for old in previous.people
        ) or any(
            not any(
                (old.org, old.subject, old.customer, old.user)
                == (new.org, new.subject, new.customer, new.user)
                and set(old.emails).issubset(new.emails)
                for new in pending.captured.workspaces
            )
            for old in previous.workspaces
        ):
            raise CredentialRefused("historical crawl person capture requires verified repair")
    await pending.validate()
    await crawl_controls.capsules(pending.captured.id)


async def require_refusals(
    org: str,
    digests: list[str],
    *,
    excluded: bool,
    ch: Any,
) -> None:
    from server.erasure.store.census import crawl_refusal_residue

    if await crawl_refusal_residue(org, digests, excluded=excluded, ch=ch):
        raise person_erasure.unavailable()


async def invalidate_restored_approvals(org: str, *, ch: Any) -> None:
    """Called under the org fence after every historical source has retirement work."""
    await crawl_controls.invalidate_restored_approvals(org, ch=ch)
