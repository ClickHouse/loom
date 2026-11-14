"""A mirrored document's permission list: who the store lets read it
(docs/architecture/document-sources.md, The permission list). Stored as canonical JSON in
`loom.documents.access_json`; `unread` is stored empty, and an empty or unreadable value
reads `unread`, which admits nobody."""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import asdict, dataclass, fields
from typing import Any

# Every Drive role on the whole file reads it; any other role (`published_reader`, or
# one this module does not know) admits nobody.
_DRIVE_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})


def _is_str_list(values: Any) -> bool:
    return isinstance(values, list) and all(isinstance(v, str) for v in values)


def _names(values: Iterable[Any]) -> tuple[str, ...]:
    """Distinct non-empty strings, lower-cased and sorted; a non-string is dropped."""
    return tuple(sorted({v.strip().lower() for v in values if isinstance(v, str) and v.strip()}))


@dataclass(frozen=True)
class AccessList:
    """Who the store lets read a mirrored document. `owner` is a connected source's
    owner subject. `unread`: no current list is known; it admits nobody. `emails`,
    `groups` and `domains` are kept lower-cased and sorted."""

    emails: tuple[str, ...] = ()
    owner: str = ""
    groups: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    anyone: bool = False
    unread: bool = False

    def __post_init__(self) -> None:
        for name in ("emails", "groups", "domains"):
            object.__setattr__(self, name, _names(getattr(self, name)))
        if self.unread and (
            self.emails or self.groups or self.domains or self.anyone or self.owner
        ):
            raise ValueError("an unread list names nobody")


UNREAD = AccessList(unread=True)


def to_json(access: AccessList) -> str:
    """The stored form; `unread` is stored empty, as every other writer stores it."""
    if access.unread:
        return ""
    return json.dumps(asdict(access), sort_keys=True, separators=(",", ":"))


def from_json(text: str) -> AccessList:
    """The stored list; `unread` for an empty or unreadable value, one without
    `unread: false`, one with a key it does not know, or one with a field or an entry of
    the wrong type."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return UNREAD
    if not isinstance(data, dict) or data.get("unread") is not False:
        return UNREAD
    if set(data) - {f.name for f in fields(AccessList)}:
        return UNREAD  # a key a later build added may narrow the list
    emails = data.get("emails", [])
    groups = data.get("groups", [])
    domains = data.get("domains", [])
    owner = data.get("owner", "")
    anyone = data.get("anyone", False)
    if not (
        all(_is_str_list(v) for v in (emails, groups, domains))
        and isinstance(owner, str)
        and isinstance(anyone, bool)
    ):
        return UNREAD
    return AccessList(emails=emails, owner=owner, groups=groups, domains=domains, anyone=anyone)


def from_drive_permissions(permissions: list[dict[str, Any]] | None) -> AccessList:
    """The list a `shared` source reads from a Drive file's `permissions`; `unread` when
    the store gave none. A deleted permission (its address may now belong to someone
    else), a link-only share (one that lets a holder of the link open the file, not find
    it), a grant to one view of the file only (`view`), and a type or role this module
    does not know admit nobody."""
    if permissions is None:
        return UNREAD
    emails: list[Any] = []
    groups: list[Any] = []
    domains: list[Any] = []
    anyone = False
    for p in permissions:
        if p.get("deleted") or p.get("view") or p.get("role") not in _DRIVE_ROLES:
            continue
        kind = p.get("type")
        if kind == "user":
            emails.append(p.get("emailAddress"))
        elif kind == "group":
            groups.append(p.get("emailAddress"))
        elif kind == "domain" and p.get("allowFileDiscovery") is True:
            domains.append(p.get("domain"))
        elif kind == "anyone" and p.get("allowFileDiscovery") is True:
            anyone = True
    return AccessList(
        emails=tuple(emails), groups=tuple(groups), domains=tuple(domains), anyone=anyone
    )


__all__ = ["UNREAD", "AccessList", "from_drive_permissions", "from_json", "to_json"]
