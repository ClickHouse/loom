"""Workspace selectors and complete crawl observations; identities remain provider-local."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Literal

from server.knowledge.domain.access_list import (
    UNREAD,
    AccessList,
    from_drive_permissions,
    from_json,
    to_json,
)

# not a setting: the selector wire protocol caps the total people at 2000.
MAX_PEOPLE = 2000
_EMAIL = re.compile(r"[A-Za-z0-9._+\-]+@[A-Za-z0-9.\-]+")
type SubjectKind = Literal["person", "group"]


def email(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 512 or not _EMAIL.fullmatch(value):
        raise ValueError("crawl address refused")
    return value.lower()


@dataclass(frozen=True, repr=False)
class Subject:
    kind: SubjectKind
    email: str

    def __repr__(self) -> str:
        return "Subject(<redacted>)"


@dataclass(frozen=True, repr=False)
class Options:
    directory_subject: str
    subjects: tuple[Subject, ...]

    def __repr__(self) -> str:
        return "Options(<redacted>)"

    @classmethod
    def parse(cls, value: Any) -> Options:
        if not isinstance(value, dict) or set(value) != {"directory_subject", "subjects"}:
            raise ValueError("crawl options refused")
        entries = value["subjects"]
        if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_PEOPLE:
            raise ValueError("crawl subjects refused")
        selected: list[Subject] = []
        for entry in entries:
            if (
                not isinstance(entry, dict)
                or set(entry) != {"type", "email"}
                or entry["type"] not in ("person", "group")
            ):
                raise ValueError("crawl subject refused")
            subject = Subject(entry["type"], email(entry["email"]))
            if subject in selected:
                raise ValueError("duplicate crawl subject")
            selected.append(subject)
        return cls(email(value["directory_subject"]), tuple(selected))

    def stored(self) -> str:
        return json.dumps(
            {
                "directory_subject": self.directory_subject,
                "subjects": [{"type": s.kind, "email": s.email} for s in self.subjects],
            },
            sort_keys=True,
            separators=(",", ":"),
        )


@dataclass(frozen=True, repr=False)
class Person:
    customer: str
    id: str
    email: str
    aliases: tuple[str, ...]

    def __repr__(self) -> str:
        return "Person(<redacted>)"

    @property
    def addresses(self) -> frozenset[str]:
        return frozenset((self.email, *self.aliases))


@dataclass(frozen=True, repr=False)
class Expansion:
    customer: str
    people: tuple[Person, ...]
    # Each selected or nested group has the entire eligible transitive membership.
    groups: tuple[tuple[str, tuple[str, ...]], ...]
    exclusion_version: str

    def __repr__(self) -> str:
        return "Expansion(<redacted>)"


def crawl_access(person: Person, file: dict[str, Any]) -> AccessList:
    """Listing as a person grants nothing without ownership or an explicit permission."""
    raw = file.get("permissions")
    if raw is not None and (not isinstance(raw, list) or any(not isinstance(p, dict) for p in raw)):
        return UNREAD
    parsed = from_drive_permissions(raw)
    owners = file.get("owners")
    owned = isinstance(owners, list) and any(
        isinstance(owner, dict)
        and owner.get("me") is True
        and isinstance(owner.get("emailAddress"), str)
        and owner["emailAddress"].lower() in person.addresses
        for owner in owners
    )
    explicit = not parsed.unread and bool(person.addresses.intersection(parsed.emails))
    if parsed.unread:
        return AccessList(emails=(person.email,)) if owned else UNREAD
    # Keep actual provider ACLs, but do not invent an entry for a link-only reader.
    addresses = set(parsed.emails)
    if owned or explicit:
        addresses.add(person.email)
    return AccessList(
        emails=tuple(addresses), groups=parsed.groups, domains=parsed.domains, anyone=parsed.anyone
    )


def union_access(left: AccessList, right: AccessList) -> AccessList:
    """Only positive observations widen a complete cycle's permission union."""
    if left.unread:
        return right
    if right.unread:
        return left
    if left.owner or right.owner:
        raise ValueError("a crawl has no connected owner")
    return AccessList(
        emails=(*left.emails, *right.emails),
        groups=(*left.groups, *right.groups),
        domains=(*left.domains, *right.domains),
        anyone=left.anyone or right.anyone,
    )


def without_people(raw: str, addresses: frozenset[str]) -> str:
    """Erase exact contact entries without changing another person's valid ACL or text."""
    if not isinstance(raw, str):
        raise ValueError("crawl permission history is unreadable")
    try:
        parsed = from_json(raw)
    except RecursionError:
        return ""
    if parsed.unread or parsed.owner:
        # A crawl never has a connected owner; malformed historical JSON proves no grant.
        return ""
    kept = tuple(value for value in parsed.emails if value not in addresses)
    return raw if kept == parsed.emails else to_json(replace(parsed, emails=kept))


def without_selectors(raw: str, addresses: frozenset[str]) -> str:
    """A retired registration may have no directory subject or remaining selector."""
    try:
        value = json.loads(raw)
        if not isinstance(value, dict) or set(value) != {"directory_subject", "subjects"}:
            raise ValueError("crawl selector history refused")
        directory = value["directory_subject"]
        if directory:
            directory = email(directory)
        elif directory != "":
            raise ValueError("crawl selector history refused")
        subjects = value["subjects"]
        if not isinstance(subjects, list) or len(subjects) > MAX_PEOPLE:
            raise ValueError("crawl selector history refused")
        checked = []
        for subject in subjects:
            if (
                not isinstance(subject, dict)
                or set(subject) != {"type", "email"}
                or subject["type"] not in ("person", "group")
            ):
                raise ValueError("crawl selector history refused")
            checked.append({"type": subject["type"], "email": email(subject["email"])})
        kept = [s for s in checked if s["type"] != "person" or s["email"] not in addresses]
        remaining = "" if directory in addresses else directory
        if len(kept) == len(subjects) and remaining == directory:
            return raw
        return json.dumps(
            {"directory_subject": remaining, "subjects": kept},
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, KeyError, RecursionError):
        raise ValueError("crawl selector history refused") from None


@dataclass(frozen=True, repr=False)
class File:
    id: str
    metadata: dict[str, Any]
    access: AccessList
    presenters: tuple[str, ...]

    def __repr__(self) -> str:
        return "File(<redacted>)"


@dataclass(repr=False)
class Union:
    """Absence is meaningful only after every admitted person's full listing finishes."""

    expansion: Expansion
    maximum_files: int
    _done: set[str] = field(default_factory=set, init=False, repr=False)
    _files: dict[str, File] = field(default_factory=dict, init=False, repr=False)

    def __repr__(self) -> str:
        return "Union(<redacted>)"

    def __post_init__(self) -> None:
        if type(self.maximum_files) is not int or self.maximum_files <= 0:
            raise ValueError("crawl file bound refused")

    def add(self, person: Person, files: list[dict[str, Any]], *, whole: bool) -> None:
        if person not in self.expansion.people or person.id in self._done or not whole:
            raise ValueError("crawl listing is incomplete or changed")
        observed: set[str] = set()
        next_files = dict(self._files)
        for file in files:
            identifier = file.get("id")
            if not isinstance(identifier, str) or not identifier or identifier in observed:
                raise ValueError("crawl file identity is ambiguous")
            if not any(
                isinstance(file.get(k), str) and file[k] for k in ("version", "modifiedTime")
            ):
                raise ValueError("crawl file revision is unavailable")
            observed.add(identifier)
            access = crawl_access(person, file)
            previous = next_files.get(identifier)
            if previous is not None:
                # A single publication must not pair one revision's text with another's ACL.
                if any(
                    previous.metadata.get(k) != file.get(k)
                    for k in (
                        "version",
                        "modifiedTime",
                        "md5Checksum",
                        "mimeType",
                        "webViewLink",
                        "name",
                    )
                ):
                    raise ValueError("crawl file changed during its union")
                next_files[identifier] = File(
                    identifier,
                    previous.metadata,
                    union_access(previous.access, access),
                    (*previous.presenters, person.id),
                )
            else:
                next_files[identifier] = File(identifier, dict(file), access, (person.id,))
            if len(next_files) > self.maximum_files:
                raise ValueError("crawl union exceeds its file bound")
        self._files = next_files
        self._done.add(person.id)

    def complete(self) -> tuple[File, ...]:
        if self._done != {person.id for person in self.expansion.people}:
            raise ValueError("crawl union is incomplete")
        return tuple(self._files[key] for key in sorted(self._files))
