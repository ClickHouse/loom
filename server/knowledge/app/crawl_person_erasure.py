"""A durable canonical-person deletion fences every affected crawl until raw cleanup ends."""

from __future__ import annotations

import asyncio
import functools
from typing import Any

from server.auth import person_erasure
from server.clients._vendor import CredentialRefused
from server.core.ch import get_client, set_query_tag
from server.erasure.app import crawl_person
from server.knowledge.app import crawl_exclusions as exclusions
from server.knowledge.app import sources as source_ops
from server.knowledge.domain.source_crawl import Person
from server.knowledge.store import crawl_exclusions as exclusion_store
from server.knowledge.store import sources as source_store
from server.settings import settings


async def _inventory(org: str, allowed_customers: set[str], ch: Any) -> dict[str, str]:
    history = await source_store.crawl_history(
        org, maximum=settings.knowledge_crawl_registry_scan_limit, ch=ch
    )
    found: dict[str, str] = {}
    for source in history:
        if (
            source.org != org
            or not source.namespace
            or not source.source_id
            or source.crawl_customer_id not in allowed_customers
            or source.source_id in found
            and found[source.source_id] != source.namespace
        ):
            raise CredentialRefused("crawl person history requires verified repair")
        found[source.source_id] = source.namespace
    return found


async def _clean_source(
    pending: person_erasure.Pending,
    person: person_erasure.Person,
    namespace: str,
    source_id: str,
    contacts: frozenset[str],
    ch: Any,
) -> None:
    await pending.validate(person.org)
    async with source_ops.sources_lease(person.org, namespace):
        await pending.validate(person.org)
        await crawl_person.erase_source(pending, person, namespace, source_id, contacts, ch=ch)
        await pending.validate(person.org)


async def _cleanup(
    pending: person_erasure.Pending,
    person: person_erasure.Person,
    contacts: frozenset[str],
    customers: set[str],
    ch: Any,
) -> None:
    before = await _inventory(person.org, customers, ch)
    for source_id, namespace in sorted(before.items()):
        await _clean_source(pending, person, namespace, source_id, contacts, ch)
    await pending.validate(person.org)
    if await _inventory(person.org, customers, ch) != before:
        raise CredentialRefused("crawl person registrations changed during cleanup")


def _people(
    captured: person_erasure.Capture,
    person: person_erasure.Person,
) -> tuple[Person, ...]:
    return tuple(
        Person(w.customer, w.user, w.emails[0], w.emails[1:])
        for w in captured.workspaces
        if (w.org, w.subject) == (person.org, person.subject)
    )


async def _exclude(pending: person_erasure.Pending, person: person_erasure.Person, ch: Any) -> None:
    people = _people(pending.captured, person)
    contacts = frozenset({*person.emails, *(e for p in people for e in p.addresses)})
    cleanup = functools.partial(
        _cleanup, pending, person, contacts, {p.customer for p in people}, ch
    )
    kwargs = {
        "ttl_s": settings.knowledge_crawl_lease_ttl_s,
        "wait_s": settings.knowledge_crawl_lease_wait_s,
        "authority": pending,
        "ch": ch,
    }
    await pending.validate(person.org)
    row = await exclusion_store.key(person.org, ch=ch)
    if row is not None and row["pending_id"]:
        # This object calculates HMACs only; open/version still refuse a blocked row.
        proof = exclusions.Index(
            person.org, exclusions._open_key(row), row["key_id"], row["generation"], ch
        )
        expected = tuple(
            sorted(
                {
                    *(d for p in people for d in proof.person_digests(p)),
                    *proof.contact_digests(tuple(contacts)),
                }
            )
        )
        if row["pending_exclude"] != 1:
            raise CredentialRefused("another crawl exclusion cleanup is pending")
        await exclusions.repair(person.org, cleanup=cleanup, expected_digests=expected, **kwargs)
    else:
        index = await exclusions.initialize(person.org, **kwargs)
        await index.change(
            people,
            contacts=tuple(sorted(contacts)),
            exclude=True,
            ttl_s=settings.knowledge_crawl_lease_ttl_s,
            wait_s=settings.knowledge_crawl_lease_wait_s,
            authority=pending,
            cleanup=cleanup,
        )


async def finish(pending: person_erasure.Pending) -> None:
    """Only the persisted, prepared deletion capsule can drive these exact-person writes."""
    set_query_tag("knowledge.crawl.person_erasure")
    if not pending.captured.prepared:
        raise person_erasure.unavailable()
    async with asyncio.timeout(settings.knowledge_crawl_person_cleanup_timeout_s):
        for person in pending.captured.people:
            await _exclude(pending, person, get_client())
        await pending.validate()
