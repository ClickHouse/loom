"""Bounded native search within a connected source's registered corpus."""

from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Literal
from urllib.parse import urlsplit, urlunsplit

from server.clients import gdrive, linear, notion
from server.clients._checked_access import CheckedAccess
from server.clients._http import get_client
from server.clients._vendor import VendorError, request_json
from server.core.ch import set_query_tag
from server.knowledge.domain.sources import (
    KnowledgeSource,
    include_in,
    include_mime_types,
    item_ids,
)
from server.settings import settings

_DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
_NOTION_API = "https://api.notion.com/v1"
_DRIVE_FIELDS = "id,name,webViewLink,mimeType,trashed,parents,driveId,modifiedTime"
_LINEAR_SEARCH = """
query LiveDocuments($term: String!, $first: Int!, $projects: [ID!]!) {
  searchDocuments(term: $term, first: $first, includeArchived: false,
                  includeComments: false, filter: {project: {id: {in: $projects}}}) {
    nodes { id title url trashed archivedAt project { id } }
    pageInfo { hasNextPage endCursor }
  }
}
"""
_LINEAR_DOCUMENT_STATE = """
query LiveDocumentState($id: String!) {
  document(id: $id) { id trashed archivedAt project { id } }
}
"""


@dataclass(frozen=True)
class LiveProviderHit:
    id: str
    title: str
    url: str
    provider: Literal["gdrive", "notion", "linear"]
    content: str = ""


class SearchUnavailable(ValueError):
    """The native search cannot establish a bounded source-scoped result."""


def _deadline(source: KnowledgeSource, access: CheckedAccess, deadline: float) -> float:
    if (
        not isinstance(access, CheckedAccess)
        or source.kind != access.kind
        or source.credential_mode != "connected"
        or not math.isfinite(deadline)
        or deadline <= time.monotonic()
    ):
        raise SearchUnavailable("live source is unavailable")
    return min(deadline, access.deadline)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _hit(
    provider: Literal["gdrive", "notion", "linear"],
    ident: Any,
    title: Any,
    url: Any,
) -> LiveProviderHit | None:
    if (
        not isinstance(ident, str)
        or not ident
        or len(ident) > 1024
        or any(ord(c) < 33 or ord(c) == 127 for c in ident)
        or not isinstance(url, str)
        or len(url) > 4096
        or any(ord(c) < 33 or ord(c) == 127 for c in url)
        or "\\" in url
    ):
        return None
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        allowed = {
            "gdrive": host in {"drive.google.com", "docs.google.com"},
            "notion": host in {"notion.so", "www.notion.so"} or host.endswith(".notion.site"),
            "linear": host == "linear.app",
        }[provider]
        if (
            parsed.scheme != "https"
            or not allowed
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
        ):
            return None
    except ValueError:
        return None
    return LiveProviderHit(
        id=ident,
        title=_text(title, 1024),
        url=urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", "")),
        provider=provider,
    )


async def _json(access: CheckedAccess, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
    return await request_json(
        get_client(),
        method,
        url,
        "live search",
        checked=access,
        max_bytes=settings.knowledge_live_search_response_max_bytes,
        **kwargs,
    )


def _drive_scope(source: KnowledgeSource, item: dict[str, Any]) -> tuple[str, str] | None:
    types = include_mime_types(include_in(source.config), item.get("kinds"))
    if not types:
        return None
    kind = item.get("type")
    ident = item.get("id")
    if kind == "filter":
        return gdrive.filter_query(item, types), ""
    if not isinstance(ident, str) or not gdrive.valid_file_id(ident):
        return None
    if kind == "folder":
        # A bounded search covers direct children even for a recursive registration.
        return gdrive.folder_query([ident], types), ""
    if kind == "shared_drive":
        return gdrive.drive_query(types), ident
    # Drive's search language has no file-id equality term; never widen a file item.
    return None


def _drive_member(source: KnowledgeSource, ident: str, row: dict[str, Any]) -> bool:
    if row.get("trashed") is not False or row.get("mimeType") not in include_mime_types(
        include_in(source.config)
    ):
        return False
    parents = row.get("parents")
    if not isinstance(parents, list) or any(not isinstance(p, str) for p in parents):
        return False
    for item in source.items:
        kind = item.get("type")
        if kind == "file" and item.get("id") == ident:
            return True
        if kind == "folder" and item.get("id") in parents:
            return True
        if kind == "shared_drive" and item.get("id") == row.get("driveId"):
            return True
        if kind != "filter":
            continue
        if row.get("mimeType") not in include_mime_types(
            include_in(source.config), item.get("kinds")
        ):
            continue
        if "in" in item and item["in"] not in parents:
            continue
        name, modified = row.get("name"), row.get("modifiedTime")
        if "name_contains" in item and (
            not isinstance(name, str)
            or not name.casefold().startswith(item["name_contains"].casefold())
        ):
            continue
        if "modified_after" in item:
            if not isinstance(modified, str) or not gdrive.valid_stamp(modified):
                continue
            try:
                at = datetime.fromisoformat(modified)
                cutoff = datetime.fromisoformat(item["modified_after"]).replace(tzinfo=UTC)
            except ValueError:
                continue
            if at <= cutoff:
                continue
        return True
    return False


async def _drive(
    source: KnowledgeSource, access: CheckedAccess, query: str, top_k: int
) -> list[LiveProviderHit]:
    found: dict[str, LiveProviderHit] = {}
    for item in source.items[: settings.knowledge_live_search_max_items]:
        scope = _drive_scope(source, item)
        if scope is None:
            continue
        clause, drive_id = scope
        params = {
            "q": f"({clause}) and fullText contains {gdrive.quoted(query)}",
            "pageSize": str(top_k),
            "fields": f"incompleteSearch,files({_DRIVE_FIELDS})",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
            "corpora": "drive" if drive_id else "user",
        }
        if drive_id:
            params["driveId"] = drive_id
        data = await _json(access, "GET", _DRIVE_FILES, params=params)
        rows = data.get("files")
        if data.get("incompleteSearch") is True:
            continue
        if not isinstance(rows, list) or len(rows) > top_k:
            raise SearchUnavailable("invalid native search result")
        for row in rows:
            if not isinstance(row, dict):
                continue
            ident = row.get("id")
            if not isinstance(ident, str) or not gdrive.valid_file_id(ident):
                continue
            if _drive_member(source, ident, row):
                hit = _hit("gdrive", ident, row.get("name"), row.get("webViewLink"))
                if hit is not None:
                    found.setdefault(hit.id, hit)
        if len(found) >= top_k:
            break
    return list(found.values())[:top_k]


async def _notion_json(
    access: CheckedAccess, method: str, path: str, **kwargs: Any
) -> dict[str, Any]:
    await notion._pace(access)
    return await _json(
        access,
        method,
        _NOTION_API + path,
        headers={"Notion-Version": notion.API_VERSION},
        **kwargs,
    )


async def _notion_member(
    source: KnowledgeSource, access: CheckedAccess, row: dict[str, Any]
) -> bool:
    pages = set(item_ids(source.items, "page"))
    databases = set(item_ids(source.items, "database"))
    max_ancestors = settings.knowledge_live_search_max_ancestors
    for hop in range(max_ancestors + 1):
        ident = row.get("id")
        if (
            not isinstance(ident, str)
            or not notion.valid_id(ident)
            or row.get("in_trash") is not False
        ):
            return False
        if row.get("object") == "page" and ident in pages:
            return True
        parent = row.get("parent")
        if not isinstance(parent, dict):
            return False
        kind = parent.get("type")
        if kind in {"database_id", "data_source_id"}:
            return parent.get("database_id") in databases
        if kind not in {"page_id", "block_id"}:
            return False
        ancestor = parent.get(kind)
        if not isinstance(ancestor, str) or not notion.valid_id(ancestor):
            return False
        if hop == max_ancestors:
            return False
        path = "/pages/" if kind == "page_id" else "/blocks/"
        row = await _notion_json(access, "GET", path + ancestor)
        if row.get("id") != ancestor:
            return False
    return False


async def _notion(
    source: KnowledgeSource, access: CheckedAccess, query: str, top_k: int
) -> list[LiveProviderHit]:
    data = await _notion_json(
        access,
        "POST",
        "/search",
        json={
            "query": query,
            "page_size": top_k,
            "filter": {"property": "object", "value": "page"},
        },
    )
    rows = data.get("results")
    if not isinstance(rows, list) or len(rows) > top_k:
        raise SearchUnavailable("invalid native search result")
    found: dict[str, LiveProviderHit] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("object") != "page":
            continue
        if await _notion_member(source, access, row):
            hit = _hit("notion", row.get("id"), notion.title_of(row), row.get("url"))
            if hit is not None:
                found.setdefault(hit.id, hit)
    return list(found.values())[:top_k]


def _linear_projects(source: KnowledgeSource) -> list[str]:
    return list(
        dict.fromkeys(
            item["id"]
            for item in source.items[: settings.knowledge_live_search_max_items]
            if item.get("type") == "project"
            and isinstance(item.get("id"), str)
            and linear.valid_id(item["id"])
        )
    )


def _linear_member(source: KnowledgeSource, ident: Any, row: dict[str, Any]) -> bool:
    project = row.get("project")
    return (
        isinstance(ident, str)
        and linear.valid_id(ident)
        and row.get("id") == ident
        and row.get("trashed") is False
        and "archivedAt" in row
        and row["archivedAt"] is None
        and isinstance(project, dict)
        and isinstance(project.get("id"), str)
        and linear.valid_id(project["id"])
        and project["id"] in _linear_projects(source)
    )


async def _linear(
    source: KnowledgeSource, access: CheckedAccess, query: str, top_k: int
) -> list[LiveProviderHit]:
    projects = _linear_projects(source)
    if not projects:
        return []
    first = min(top_k, linear.PAGE_SIZE)
    try:
        data = await linear._query(
            access,
            _LINEAR_SEARCH,
            {"term": query, "first": first, "projects": projects},
            "live search",
            max_bytes=settings.knowledge_live_search_response_max_bytes,
        )
    except linear.LinearError as e:
        raise SearchUnavailable("live source is unavailable") from e
    page = data.get("searchDocuments")
    rows = page.get("nodes") if isinstance(page, dict) else None
    info = page.get("pageInfo") if isinstance(page, dict) else None
    if (
        not isinstance(rows, list)
        or len(rows) > first
        or any(not isinstance(row, dict) for row in rows)
        or not isinstance(info, dict)
        or not isinstance(info.get("hasNextPage"), bool)
        or (info.get("endCursor") is not None and not isinstance(info["endCursor"], str))
        or (info["hasNextPage"] and not info.get("endCursor"))
    ):
        raise SearchUnavailable("live source is unavailable")
    found: dict[str, LiveProviderHit] = {}
    for row in rows:
        ident = row.get("id")
        if not _linear_member(source, ident, row) or not isinstance(row.get("title"), str):
            continue
        hit = _hit("linear", ident, row["title"], row.get("url"))
        if hit is not None:
            found.setdefault(hit.id, hit)
    return list(found.values())[:top_k]


async def search(
    source: KnowledgeSource,
    access: CheckedAccess,
    query: str,
    *,
    top_k: int,
    deadline: float,
) -> list[LiveProviderHit]:
    """One bounded search; incomplete coverage is never evidence that a document is absent."""
    set_query_tag("knowledge.live.search")
    stop = _deadline(source, access, deadline)
    if not query.strip() or len(query) > 1000 or top_k < 1:
        return []
    async with asyncio.timeout_at(stop):
        await access.check()
        if source.kind == "gdrive":
            result = await _drive(
                source, access, query, min(top_k, settings.knowledge_live_search_max_hits)
            )
        elif source.kind == "notion":
            result = await _notion(
                source, access, query, min(top_k, settings.knowledge_live_search_max_hits)
            )
        elif source.kind == "linear":
            result = await _linear(
                source, access, query, min(top_k, settings.knowledge_live_search_max_hits)
            )
        else:
            raise SearchUnavailable("native search is unavailable for this source")
        await access.check()
        return result


async def revalidate(
    source: KnowledgeSource,
    access: CheckedAccess,
    hit: LiveProviderHit,
    *,
    deadline: float,
) -> bool:
    """Recheck the retained hit's current native membership without replacing it."""
    set_query_tag("knowledge.live.membership")
    stop = _deadline(source, access, deadline)
    if hit.provider != source.kind:
        return False
    async with asyncio.timeout_at(stop):
        await access.check()
        try:
            if source.kind == "gdrive" and gdrive.valid_file_id(hit.id):
                row = await _json(
                    access,
                    "GET",
                    _DRIVE_FILES + "/" + hit.id,
                    params={"fields": _DRIVE_FIELDS, "supportsAllDrives": "true"},
                )
                return row.get("id") == hit.id and _drive_member(source, hit.id, row)
            if source.kind == "notion" and notion.valid_id(hit.id):
                row = await _notion_json(access, "GET", "/pages/" + hit.id)
                return row.get("id") == hit.id and await _notion_member(source, access, row)
            if source.kind == "linear" and linear.valid_id(hit.id):
                data = await linear._query(
                    access,
                    _LINEAR_DOCUMENT_STATE,
                    {"id": hit.id},
                    "live membership",
                    max_bytes=settings.knowledge_live_search_response_max_bytes,
                )
                document = data.get("document")
                return isinstance(document, dict) and _linear_member(source, hit.id, document)
        except (VendorError, linear.LinearError):
            return False
        return False
