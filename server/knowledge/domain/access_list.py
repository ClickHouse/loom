"""A mirrored document's permission list: who the store lets read it
(docs/architecture/document-sources.md, The permission list). Stored as canonical JSON in
`loom.documents.access_json`; an empty or unreadable value is `unread`, which admits
nobody."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

# Every Drive role reads the file; a role this module does not know admits nobody.
_DRIVE_ROLES = frozenset({"owner", "organizer", "fileOrganizer", "writer", "commenter", "reader"})


@dataclass(frozen=True)
class AccessList:
    """`emails` and `groups` lower-cased, `domains` lower-cased, each sorted; `unread`
    when no current list is known, with every other entry empty."""

    emails: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    domains: tuple[str, ...] = ()
    anyone: bool = False
    unread: bool = False


UNREAD = AccessList(unread=True)


def to_json(access: AccessList) -> str:
    """The stored form: one spelling per list, so an unchanged list compares equal."""
    return json.dumps(
        {
            "anyone": access.anyone,
            "domains": list(access.domains),
            "emails": list(access.emails),
            "groups": list(access.groups),
            "unread": access.unread,
        },
        sort_keys=True,
        separators=(",", ":"),
    )


def from_json(text: str) -> AccessList:
    """The stored list; `unread` for an empty, unreadable or `unread` value."""
    try:
        data = json.loads(text) if text else None
    except ValueError:
        return UNREAD
    if not isinstance(data, dict) or data.get("unread") is not False:
        return UNREAD
    return AccessList(
        emails=_names(data.get("emails")),
        groups=_names(data.get("groups")),
        domains=_names(data.get("domains")),
        anyone=data.get("anyone") is True,
    )


def from_drive_permissions(permissions: list[dict[str, Any]] | None) -> AccessList:
    """The list a `shared` source reads from a Drive file's `permissions`; `unread` when
    the store gave none. A deleted or pending permission (its address may now belong to
    someone else), a link-only share (one that lets a holder of the link open the file,
    not find it), and a type or role this module does not know admit nobody."""
    if permissions is None:
        return UNREAD
    emails: list[str] = []
    groups: list[str] = []
    domains: list[str] = []
    anyone = False
    for p in permissions:
        if p.get("deleted") or p.get("pendingOwner") or p.get("role") not in _DRIVE_ROLES:
            continue
        kind = p.get("type")
        if kind == "user":
            emails.append(str(p.get("emailAddress") or ""))
        elif kind == "group":
            groups.append(str(p.get("emailAddress") or ""))
        elif kind == "domain" and p.get("allowFileDiscovery") is True:
            domains.append(str(p.get("domain") or ""))
        elif kind == "anyone" and p.get("allowFileDiscovery") is True:
            anyone = True
    return AccessList(
        emails=_names(emails), groups=_names(groups), domains=_names(domains), anyone=anyone
    )


def _names(values: Any) -> tuple[str, ...]:
    """Distinct non-empty strings, lower-cased and sorted; nothing from a non-list."""
    if not isinstance(values, list):
        return ()
    return tuple(sorted({v.strip().lower() for v in values if isinstance(v, str) and v.strip()}))


__all__ = ["UNREAD", "AccessList", "from_drive_permissions", "from_json", "to_json"]
