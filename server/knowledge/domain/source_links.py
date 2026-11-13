"""Pasted links to a document store's folders, files, pages and projects, turned
into a source's items (`knowledge.source.parse_links`, and the `links` of add and
update). Pure: no request to any store, so a link is read by its form alone.

A link of another host, with no id, or to something the sync does not read (a
spreadsheet, a single issue) is `unreadable`, with the reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs, urlsplit

from server.core.errors import BadConfigError
from server.knowledge.domain.sources import parse_items

_DRIVE_FOLDER_RE = re.compile(r"/drive(?:/u/\d+)?(?:/mobile)?/folders/([^/]+)")
_DRIVE_FILE_RE = re.compile(r"/file(?:/u/\d+)?/d/([^/]+)")
_DOCS_RE = re.compile(
    r"(?:/a/[^/]+)?/(document|presentation|spreadsheets|forms|drawings)(?:/u/\d+)?/d/([^/]+)"
)
_NOTION_ID_RE = re.compile(r"(?:^|-)([0-9a-fA-F]{32})$")
_LINEAR_RE = re.compile(r"/[^/]+/(project|issue|document|team|view)/([^/]+)")
_LINEAR_SLUG_RE = re.compile(r"(?:.*-)?([0-9a-f]{12})")
_NOTION_HOSTS = {"notion.so", "www.notion.so"}
_DOCS_UNREAD = {"spreadsheets": "a spreadsheet", "forms": "a form", "drawings": "a drawing"}


@dataclass(frozen=True)
class ParsedLink:
    """One link: the item it names and the source kind that reads it, or why not."""

    link: str
    kind: str = ""
    item: dict[str, Any] = field(default_factory=dict)
    unreadable: str = ""


def parse_links(links: list[str]) -> list[ParsedLink]:
    """Each link, in order, as an item or an `unreadable` reason."""
    return [_parse(link) for link in links]


def _parse(link: str) -> ParsedLink:
    text = link.strip()
    if not text:
        return ParsedLink(link, unreadable="not a link")
    if "://" not in text:
        text = "https://" + text
    try:
        url = urlsplit(text)
    except ValueError:
        return ParsedLink(link, unreadable="not a link")
    host = (url.hostname or "").lower()
    if url.scheme not in ("https", "http"):
        return ParsedLink(link, unreadable="not a web link")
    path = url.path.rstrip("/")
    query = parse_qs(url.query)
    if host == "drive.google.com":
        found = _drive(path)
    elif host == "docs.google.com":
        found = _docs(path)
    elif host in _NOTION_HOSTS or host.endswith(".notion.site"):
        found = _notion(path, query)
    elif host == "linear.app":
        found = _linear(path)
    else:
        return ParsedLink(link, unreadable=f"{host or 'no host'}: not a store a source reads")
    if isinstance(found, str):
        return ParsedLink(link, unreadable=found)
    kind, item = found
    try:
        [checked] = parse_items(kind, [item])
    except BadConfigError as e:
        return ParsedLink(link, unreadable=e.message.removeprefix("items[0]."))
    return ParsedLink(link, kind=kind, item=checked)


def _drive(path: str) -> tuple[str, dict[str, Any]] | str:
    if m := _DRIVE_FOLDER_RE.fullmatch(path):
        return "gdrive", {"type": "folder", "id": m.group(1)}
    if m := _DRIVE_FILE_RE.match(path):
        return "gdrive", {"type": "file", "id": m.group(1)}
    if path == "/open":
        return "an open link names a folder or a file alike; copy the link of the one you mean"
    return "a shared-folder link with no folder or file id"


def _docs(path: str) -> tuple[str, dict[str, Any]] | str:
    m = _DOCS_RE.match(path)
    if not m:
        return "a document link with no id"
    if m.group(1) in _DOCS_UNREAD:
        return f"{_DOCS_UNREAD[m.group(1)]}: the sync reads documents, slides and files"
    if m.group(2) == "e":
        return "a published copy; link the document itself"
    return "gdrive", {"type": "file", "id": m.group(2)}


def _notion(path: str, query: dict[str, list[str]]) -> tuple[str, dict[str, Any]] | str:
    # A page opened beside its database carries the page in `p`.
    peeked = query.get("p", [""])[0]
    if m := _NOTION_ID_RE.fullmatch(peeked):
        return "notion", {"type": "page", "id": m.group(1)}
    last = path.rsplit("/", 1)[-1]
    m = _NOTION_ID_RE.search(last)
    if not m:
        return "a page-store link with no page id"
    # A database's link opens one of its views.
    item_type = "database" if query.get("v") else "page"
    return "notion", {"type": item_type, "id": m.group(1)}


def _linear(path: str) -> tuple[str, dict[str, Any]] | str:
    m = _LINEAR_RE.match(path)
    if not m:
        return "a tracker link with no project"
    if m.group(1) in ("team", "view"):
        return f"a {m.group(1)} link: link one of its projects"
    if m.group(1) != "project":
        return f"a single {m.group(1)}: the sync reads a project's documents; link the project"
    slug = _LINEAR_SLUG_RE.fullmatch(m.group(2).lower())
    if not slug:
        return "a project link with no project id"
    return "linear", {"type": "project", "slug_id": slug.group(1)}


__all__ = ["ParsedLink", "parse_links"]
