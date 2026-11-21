"""Verified canonical-person deletion metadata survives contact and sign-in removal.

This is cleanup input, never a request identity, credential or permission to read as
the person. The required downstream port runs outside SCIM's identity leases.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from server.auth import aliases
from server.auth.reader_identity import resolve_reader
from server.core.ch import get_client, own_write_reads, set_query_tag
from server.core.errors import BackendUnavailableError
from server.core.secrets_at_rest import seal, unseal
from server.settings import settings

PURPOSE = "auth.crawl_person_erasure"


def unavailable() -> BackendUnavailableError:
    return BackendUnavailableError(
        "canonical-person cleanup is unavailable", operation="crawl_erasure"
    )


@dataclass(frozen=True, repr=False)
class Person:
    org: str
    subject: str
    emails: tuple[str, ...]
    spellings: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if (
            any(not isinstance(v, str) or not v or "\0" in v for v in (self.org, self.subject))
            or not self.emails
            or self.emails != tuple(sorted(set(self.emails)))
            or self.spellings != tuple(sorted(set(self.spellings)))
            or self.subject not in self.spellings
            or any(not isinstance(v, str) or not v or "\0" in v for v in self.spellings)
            or any(
                not isinstance(v, str)
                or v != v.lower()
                or v.count("@") != 1
                or not all(v.split("@"))
                or any(ord(c) < 33 or ord(c) > 126 for c in v)
                for v in self.emails
            )
        ):
            raise unavailable()


@dataclass(frozen=True, repr=False)
class Workspace:
    """A real Directory observation tied to an already verified canonical person."""

    org: str
    subject: str
    customer: str
    user: str
    emails: tuple[str, ...]

    def __post_init__(self) -> None:
        Person(self.org, self.subject, self.emails, (self.subject,))
        if any(
            not isinstance(value, str)
            or not value
            or len(value) > 512
            or any(not 33 <= ord(char) <= 126 for char in value)
            for value in (self.customer, self.user)
        ):
            raise unavailable()


@dataclass(frozen=True, repr=False)
class Capture:
    id: str
    people: tuple[Person, ...]
    workspaces: tuple[Workspace, ...] = ()
    prepared: bool = False


def _seal(captured: Capture) -> str:
    value = {"format": 2, **asdict(captured)}
    encoded = seal(PURPOSE, json.dumps(value, separators=(",", ":"), sort_keys=True))
    load(captured.id, encoded)
    return encoded


async def capture(uid: str, email: str, ties: dict[str, set[str]]) -> str:
    """Only verified directory rows can join a contact to a canonical subject and org."""
    set_query_tag("auth.person_erasure.capture")
    if not uid or not email or not isinstance(ties, dict):
        raise unavailable()
    people: dict[tuple[str, str], Person] = {}
    for subject, orgs in sorted(ties.items()):
        for org in sorted(orgs):
            identity = await resolve_reader(org, subject)
            if identity is None or email.lower() not in identity.emails:
                raise unavailable()
            person = Person(
                org,
                identity.subject,
                tuple(sorted(identity.emails)),
                tuple(sorted(await aliases.verified_spellings(org, identity.subject))),
            )
            key = org, identity.subject
            if key in people and people[key] != person:
                raise unavailable()
            people[key] = person
    return _seal(Capture(uid, tuple(people[key] for key in sorted(people))))


def load(uid: str, encoded: str) -> Capture:
    try:
        value = json.loads(unseal(PURPOSE, encoded, warn=False))
        if (
            not isinstance(value, dict)
            or set(value) != {"format", "id", "people", "workspaces", "prepared"}
            or type(value["format"]) is not int
            or value["format"] != 2
            or not uid
            or value["id"] != uid
            or not isinstance(value["people"], list)
            or not isinstance(value["workspaces"], list)
            or type(value["prepared"]) is not bool
            or any(
                not isinstance(p, dict)
                or set(p) != {"org", "subject", "emails", "spellings"}
                or not isinstance(p["emails"], list)
                or not isinstance(p["spellings"], list)
                for p in value["people"]
            )
        ):
            raise unavailable()
        people = tuple(
            Person(p["org"], p["subject"], tuple(p["emails"]), tuple(p["spellings"]))
            for p in value["people"]
        )
        keys = [(p.org, p.subject) for p in people]
        if keys != sorted(set(keys)):
            raise unavailable()
        workspaces = _workspaces(value["workspaces"], people)
        return Capture(uid, people, workspaces, value["prepared"])
    except (ValueError, TypeError, KeyError, RecursionError):
        raise unavailable() from None


def _workspaces(values: Any, people: tuple[Person, ...]) -> tuple[Workspace, ...]:
    if not isinstance(values, list) or any(
        not isinstance(w, dict)
        or set(w) != {"org", "subject", "customer", "user", "emails"}
        or not isinstance(w["emails"], list)
        for w in values
    ):
        raise unavailable()
    result = tuple(
        Workspace(w["org"], w["subject"], w["customer"], w["user"], tuple(w["emails"]))
        for w in values
    )
    keys = [(w.org, w.subject, w.customer, w.user) for w in result]
    if keys != sorted(set(keys)) or any(
        not any(
            (p.org, p.subject) == (w.org, w.subject) and set(p.emails).intersection(w.emails)
            for p in people
        )
        for w in result
    ):
        raise unavailable()
    return result


@dataclass(frozen=True, repr=False)
class Pending:
    """Only this exact durable SCIM deletion can authorize its private cleanup."""

    captured: Capture
    encoded: str

    async def validate(self, org: str | None = None, *, before_delete: bool = False) -> None:
        if org is not None and org not in {p.org for p in self.captured.people}:
            raise unavailable()
        with own_write_reads():
            row = await get_client().query_one(
                "SELECT active, deleted, crawl_erasure_enc, "
                "crawl_erasure_required, crawl_erasure_done "
                "FROM loom.scim_users FINAL WHERE id = {id:String} LIMIT 1",
                params={"id": self.captured.id},
            )
        if (
            row is None
            or row["crawl_erasure_enc"] != self.encoded
            or not row["crawl_erasure_required"]
            or row["crawl_erasure_done"]
            or before_delete
            and row["deleted"]
        ):
            raise unavailable()
        if before_delete:
            for person in self.captured.people:
                if org is not None and person.org != org:
                    continue
                identity = await resolve_reader(person.org, person.subject)
                if (
                    identity is None
                    or identity.subject != person.subject
                    or identity.emails != frozenset(person.emails)
                    or tuple(sorted(await aliases.verified_spellings(person.org, person.subject)))
                    != person.spellings
                ):
                    raise unavailable()


class Preparer(Protocol):
    async def __call__(self, pending: Pending) -> tuple[Workspace, ...]: ...


class Cleanup(Protocol):
    async def __call__(self, pending: Pending) -> None: ...


_cleanup: Cleanup | None = None
_preparer: Preparer | None = None
_finalizer: Cleanup | None = None


def bind(preparer: Preparer, cleanup: Cleanup, finalizer: Cleanup) -> None:
    global _cleanup, _preparer, _finalizer
    _cleanup, _preparer, _finalizer = cleanup, preparer, finalizer


def interrupted(uid: str, encoded: str, observed: str) -> str:
    """Retain both verified intents when reactivation changed the canonical person."""
    previous, current = load(uid, encoded), load(uid, observed)
    merged: dict[tuple[str, str], set[str]] = {}
    spellings: dict[tuple[str, str], set[str]] = {}
    for person in (*previous.people, *current.people):
        merged.setdefault((person.org, person.subject), set()).update(person.emails)
        spellings.setdefault((person.org, person.subject), set()).update(person.spellings)
    people = tuple(
        Person(
            org,
            subject,
            tuple(sorted(merged[org, subject])),
            tuple(sorted(spellings[org, subject])),
        )
        for org, subject in sorted(merged)
    )
    return _seal(Capture(uid, people, previous.workspaces, False))


async def prepare(uid: str, encoded: str) -> str:
    if _preparer is None:
        raise unavailable()
    captured = load(uid, encoded)
    pending = Pending(captured, encoded)
    await pending.validate(before_delete=True)
    if captured.prepared:
        return encoded
    workspaces = await _preparer(pending)
    await pending.validate(before_delete=True)
    return _seal(Capture(uid, captured.people, workspaces, True))


async def finish(uid: str, encoded: str) -> None:
    if _cleanup is None:
        raise unavailable()
    captured = load(uid, encoded)
    if not captured.prepared:
        raise unavailable()
    pending = Pending(captured, encoded)
    await pending.validate()
    await _cleanup(pending)
    await pending.validate()


async def finalize(uid: str, encoded: str) -> None:
    """SCIM holds its user lease after cleanup, so no newer capture can be overwritten."""
    if _finalizer is None:
        raise unavailable()
    captured = load(uid, encoded)
    if not captured.prepared:
        raise unavailable()
    pending = Pending(captured, encoded)
    await pending.validate()
    await _finalizer(pending)


async def require_settled(*, ch: Any = None) -> None:
    """Missing historical identity proof stops crawls until explicit verified backfill."""
    with own_write_reads():
        row = await (ch or get_client()).query_one(
            "SELECT 1 AS pending FROM loom.scim_users FINAL "
            "WHERE (deleted = 1 OR crawl_erasure_required = 1) AND NOT crawl_erasure_done LIMIT 1"
        )
    if row is not None:
        raise unavailable()


async def _reseal_user(uid: str, *, dry_run: bool, ch: Any) -> tuple[int, bool]:
    from server.auth import scim
    from server.core.secrets_at_rest import resealed

    changed = 0
    async with scim._held(scim._user_lease(uid)):
        rows = await ch.query_all(
            "SELECT DISTINCT crawl_erasure_enc FROM loom.scim_users "
            "WHERE id = {id:String} AND crawl_erasure_enc != '' LIMIT {limit:UInt32}",
            params={"id": uid, "limit": settings.knowledge_crawl_registry_scan_limit + 1},
        )
        if len(rows) > settings.knowledge_crawl_registry_scan_limit:
            raise unavailable()
        for row in rows:
            encoded = str(row["crawl_erasure_enc"])
            if not unseal(PURPOSE, encoded, warn=False):
                return changed, True
            captured = load(uid, encoded)
            replacement = resealed(PURPOSE, encoded)
            if replacement is None:
                continue
            if load(uid, replacement) != captured:
                raise unavailable()
            changed += 1
            if not dry_run:
                await ch.execute(
                    "ALTER TABLE loom.scim_users UPDATE crawl_erasure_enc = {new:String} "
                    "WHERE id = {id:String} AND crawl_erasure_enc = {old:String}",
                    params={"id": uid, "new": replacement, "old": encoded},
                    settings={"mutations_sync": 2},
                )
                residue = await ch.query_one(
                    "SELECT count() AS n FROM loom.scim_users WHERE id = {id:String} "
                    "AND crawl_erasure_enc = {old:String}",
                    params={"id": uid, "old": encoded},
                )
                if residue is None or residue["n"] != 0:
                    raise unavailable()
    return changed, False


async def reseal_registry(*, dry_run: bool, ch: Any) -> tuple[int, list[str]]:
    """Reseal every historical capsule under the same identity lease as deletion.

    Completion inspects raw capsule history, so resealing only FINAL would leave
    that proof unreadable when the preceding root is removed.
    """
    if ch is not get_client():
        raise RuntimeError("crawl_person_reseal_target_mismatch")
    set_query_tag("auth.person_erasure.reseal")
    after, scanned, changed, lost = "", 0, 0, []
    while page := await ch.query_all(
        "SELECT DISTINCT id FROM loom.scim_users "
        "WHERE id > {after:String} AND crawl_erasure_enc != '' "
        "ORDER BY id LIMIT {limit:UInt32}",
        params={"after": after, "limit": settings.knowledge_crawl_registry_page_size},
    ):
        identifiers = [str(row["id"]) for row in page]
        scanned += len(identifiers)
        if (
            scanned > settings.knowledge_crawl_registry_scan_limit
            or identifiers != sorted(set(identifiers))
            or any(uid <= after for uid in identifiers)
        ):
            raise unavailable()
        for uid in identifiers:
            count, unreadable = await _reseal_user(uid, dry_run=dry_run, ch=ch)
            changed += count
            if unreadable:
                lost.append(uid)
        after = identifiers[-1]
    return changed, lost
