"""The probe of a document source: an authenticated check and listing with the
credential, before anything is stored (add, update, rotate) or on request (check),
bounded as a whole by LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S.

Each kind has one probe function, over the items its caller checks (`sample` picks
them). It answers a `Probe` or raises `SourceUnreachable` / `CredentialRefused`; the
credential is held only for the call. A tracker project named by its short id is
resolved to its UUID first (`resolve`).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from server.knowledge.domain.sources import (
    SAMPLE_ITEMS,
    include_in,
    include_mime_types,
    item_ids,
)
from server.settings import settings

# Titles a probe returns, at most.
SAMPLE_TITLES = 5


@dataclass(frozen=True)
class Probe:
    # Documents the source lists now: the first page, 1000 at most, of the folders, of
    # each shared drive and of each filter, and each file item (gdrive); at most 1000
    # (recording); each project's first page (linear); the listed pages and the first
    # 100 pages of each of up to three databases (notion).
    documents: int
    # Up to five of their titles, most recently changed first.
    sample: list[str] = field(default_factory=list)
    # True when more match than were listed, so `documents` is a lower bound.
    more: bool = False
    # The items checked, as the store named them: a recursive `folder` item that is a
    # shared drive's root comes back as a `shared_drive` one. [] from a kind without items.
    items: list[dict[str, Any]] = field(default_factory=list)
    # The file items checked whose kind the source's `include` does not name, by id,
    # with the store's type of each.
    foreign: dict[str, str] = field(default_factory=dict)
    # The file items checked that are in the trash.
    trashed: list[str] = field(default_factory=list)


class SourceUnreachable(Exception):
    def __init__(self, message: str, item_id: str = "") -> None:
        super().__init__(message)
        # The id the refusal names, when it is about one: an item's, or a filter's `in`
        # folder.
        self.item_id = item_id


class CredentialRefused(Exception):
    pass


def sample(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The items a probe checks: all of them, or the SAMPLE_ITEMS added last."""
    return items[-SAMPLE_ITEMS:]


async def _check_gdrive_item(
    gdrive: Any, token: str, item: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any] | None]:
    """Check one item is one the account sees: the item as the store names it, and a
    file item's metadata (so the listing reads it once), else None."""
    try:
        if item["type"] == "folder":
            folder = await gdrive.require_folder(token, item["id"])
            # A recursive walk of a drive's root reads the drive: stored as the drive, it
            # is listed in one query. A folder that is not recursive reads the root's own
            # files.
            if item.get("recursive") and folder.get("driveId") == item["id"]:
                return {"type": "shared_drive", "id": item["id"]}, None
        elif item["type"] == "shared_drive":
            await gdrive.require_drive(token, item["id"])
        elif item["type"] == "file":
            return item, await gdrive.file_meta(token, item["id"])
        elif item["type"] == "filter" and "in" in item:
            await gdrive.require_folder(token, item["in"])
    except gdrive.NotFound as e:
        # A filter's `in` folder is what was checked.
        seen = item["in"] if item["type"] == "filter" else item["id"]
        what = {"shared_drive": "shared drive", "file": "file"}.get(item["type"], "folder")
        # A 404 is a missing share (a drive's: no membership); an id the store answered is
        # no folder, or trashed.
        why = "not found; share it with the service account"
        if item["type"] == "shared_drive":
            why = "not found; add the service account as a member of the drive"
        elif e.status != 404:
            why = "not a folder, or in the trash"
        raise SourceUnreachable(f"{what} {seen}: {why}", seen) from None
    return item, None


async def _check_items(
    gdrive: Any, token: str, items: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Check every item at once: the items as the store names them, and the file items'
    metadata by id. The first failure cancels the rest and is raised."""
    try:
        async with asyncio.TaskGroup() as group:
            checks = [group.create_task(_check_gdrive_item(gdrive, token, i)) for i in items]
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    results = [c.result() for c in checks]
    files = {item["id"]: meta for item, meta in results if meta is not None}
    return [item for item, _ in results], files


async def _probe_gdrive(
    items: list[dict[str, Any]], config: dict[str, Any], credential: str
) -> Probe:
    from server.clients import gdrive

    include = include_in(config)
    mime_types = include_mime_types(include)
    try:
        account = gdrive.parse_service_account(credential)
        token = await gdrive.access_token(account)
        # Every item must be one the account sees (an unshared folder lists empty,
        # not refused); the checks run together, then one listing page per query.
        checked, files = await _check_items(gdrive, token, items)
        found, more = await _gdrive_first_pages(gdrive, token, checked, files, mime_types, include)
    except gdrive.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except gdrive.NotFound as e:
        raise SourceUnreachable(f"{e}; share it with the service account") from None
    except gdrive.VendorError as e:
        raise SourceUnreachable(str(e)) from None
    found.sort(key=lambda f: str(f.get("modifiedTime") or ""), reverse=True)
    titles = [str(f.get("name") or "") for f in found[:SAMPLE_TITLES]]
    # A trashed file, or one of a kind the source does not read, lists nothing: add and
    # update refuse it; check and rotate, which test the credential, do not.
    trashed = sorted(i for i, meta in files.items() if meta.get("trashed"))
    foreign = {
        i: str(meta.get("mimeType") or "")
        for i, meta in sorted(files.items())
        if meta.get("mimeType") not in mime_types and not meta.get("trashed")
    }
    # The first page reads a recursive folder's own files, not its subfolders'.
    more = more or any(i["type"] == "folder" and i.get("recursive") for i in checked)
    return Probe(
        documents=len(found),
        sample=titles,
        more=more,
        items=checked,
        foreign=foreign,
        trashed=trashed,
    )


async def _gdrive_first_pages(
    gdrive: Any,
    token: str,
    items: list[dict[str, Any]],
    files: dict[str, dict[str, Any]],
    mime_types: list[str],
    include: list[str],
) -> tuple[list[dict[str, Any]], bool]:
    """The first listing page of the items' folders, of each shared drive and of
    each filter, and each file item (its metadata in `files`, read by the check): the
    files found, each once, and whether more follow."""
    # (query, drive id) for each first page, read together as the checks are.
    queries: list[tuple[str, str]] = []
    folders = [i["id"] for i in items if i["type"] == "folder"]
    if folders:
        queries.append((gdrive.folder_query(folders, mime_types), ""))
    pages: list[tuple[list[dict[str, Any]], bool]] = []
    for item in items:
        if item["type"] == "shared_drive":
            queries.append((gdrive.drive_query(mime_types), item["id"]))
        elif item["type"] == "filter":
            # Add and update refuse a filter whose kinds share none with include.
            filter_types = include_mime_types(include, item.get("kinds"))
            queries.append((gdrive.filter_query(item, filter_types), ""))
        elif item["type"] == "file":
            meta = files[item["id"]]
            readable = meta.get("mimeType") in mime_types and not meta.get("trashed")
            pages.append(([meta] if readable else [], False))
    try:
        async with asyncio.TaskGroup() as group:
            tasks = [group.create_task(gdrive.list_files(token, q, drive_id=d)) for q, d in queries]
    except ExceptionGroup as failed:
        raise failed.exceptions[0] from None
    pages += [t.result() for t in tasks]
    found = {str(f.get("id")): f for page, _ in pages for f in page}
    return list(found.values()), any(more for _, more in pages)


async def _probe_linear(
    items: list[dict[str, Any]], config: dict[str, Any], credential: str
) -> Probe:
    from server.clients import linear

    try:
        # One page of each project: every project must be one the key sees.
        async with asyncio.TaskGroup() as group:
            pages = [
                group.create_task(linear.documents_page(credential, project_id))
                for project_id in item_ids(items, "project")
            ]
    except ExceptionGroup as failed:
        e = failed.exceptions[0]
        if isinstance(e, linear.CredentialRefused):
            raise CredentialRefused(str(e)) from None
        if isinstance(e, linear.NotFound):
            raise SourceUnreachable(f"{e}; the key's account must see the project") from None
        if isinstance(e, linear.LinearError):
            raise SourceUnreachable(str(e)) from None
        raise e from None
    found = [node for page in pages for node in page.result()[0]]
    found.sort(key=lambda node: str(node.get("updatedAt") or ""), reverse=True)
    titles = [str(node.get("title") or "") for node in found[:SAMPLE_TITLES]]
    more = any(page.result()[1] for page in pages)
    return Probe(documents=len(found), sample=titles, more=more, items=items)


# Databases whose pages a page-store probe samples. Every listed id is read; only
# this many first data sources are queried, so a probe of 20 ids stays well inside
# its time bound at the store's pace of about three requests a second.
_SAMPLED_DATABASES = 3  # not a setting: a bound on one probe's requests


async def _probe_notion(
    items: list[dict[str, Any]], config: dict[str, Any], credential: str
) -> Probe:
    from server.clients import notion

    if not notion.valid_token(credential):
        raise CredentialRefused("the credential is not an integration token")
    found: list[dict[str, Any]] = []
    more = False
    sampled = 0
    try:
        # Every listed page and database must be one the integration sees: one
        # shared with it answers, one that is not answers 404.
        for page_id in item_ids(items, "page"):
            page = await notion.page(credential, page_id)
            if page.get("in_trash"):
                raise SourceUnreachable(f"page {page_id}: in the trash")
            found.append(page)
        for database_id in item_ids(items, "database"):
            db = await notion.database(credential, database_id)
            if db.get("in_trash"):
                raise SourceUnreachable(f"database {database_id}: in the trash")
            sources = notion.data_source_ids(db)
            if sources and sampled < _SAMPLED_DATABASES:
                sampled += 1
                pages, has_more = await notion.first_pages(credential, sources[0])
                found.extend(pages)
                more = more or has_more or len(sources) > 1
            elif sources:
                more = True  # not sampled: its pages are not counted
    except notion.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except notion.NotFound as e:
        raise SourceUnreachable(f"{e}; share it with the integration") from None
    except notion.VendorError as e:
        raise SourceUnreachable(str(e)) from None
    # A listed page may also be a listed database's.
    found = list({str(p.get("id")): p for p in found}.values())
    found.sort(key=lambda p: str(p.get("last_edited_time") or ""), reverse=True)
    titles = [notion.title_of(p) for p in found[:SAMPLE_TITLES]]
    return Probe(documents=len(found), sample=titles, more=more, items=items)


async def _probe_recording(
    items: list[dict[str, Any]], config: dict[str, Any], credential: str
) -> Probe:
    from server.clients import recording
    from server.knowledge.app import source_sync_recording

    try:
        session = await source_sync_recording.open_session(config["provider"], credential)
        found, lost = await source_sync_recording.list_recordings(session, config["ids"])
    except recording.CredentialRefused as e:
        raise CredentialRefused(str(e)) from None
    except recording.VendorError as e:
        raise SourceUnreachable(str(e)) from None
    if lost:
        raise SourceUnreachable(f"{lost[0]}; check the id and that the account can see it")
    found.sort(key=lambda r: r["modifiedTime"], reverse=True)
    titles = [r["name"] for r in found[:SAMPLE_TITLES]]
    return Probe(documents=min(len(found), 1000), sample=titles, more=len(found) > 1000)


PROBES: dict[str, Callable[[list[dict[str, Any]], dict[str, Any], str], Awaitable[Probe]]] = {
    "gdrive": _probe_gdrive,
    "linear": _probe_linear,
    "notion": _probe_notion,
    "recording": _probe_recording,
}


async def probe(
    kind: str,
    config: dict[str, Any],
    credential: str,
    *,
    items: list[dict[str, Any]],
) -> Probe:
    """The kind's probe of `items`, with the options `config` holds (`include`, a
    recording's provider and ids), bounded by LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S as a
    whole."""
    limit = float(settings.knowledge_source_probe_timeout_s)
    try:
        async with asyncio.timeout(limit):
            return await PROBES[kind](items, config, credential)
    except TimeoutError:
        raise SourceUnreachable(f"the source did not answer within {limit:g} s") from None


async def resolve(kind: str, items: list[dict[str, Any]], credential: str) -> list[dict[str, Any]]:
    """`items` with each tracker project named by its short id (a link's) named by
    its UUID, bounded by LOOM_KNOWLEDGE_SOURCE_PROBE_TIMEOUT_S; SourceUnreachable
    names a short id the tracker does not know."""
    slugs = [i["slug_id"] for i in items if "slug_id" in i]
    if kind != "linear" or not slugs:
        return items
    from server.clients import linear

    limit = float(settings.knowledge_source_probe_timeout_s)
    try:
        async with asyncio.timeout(limit):
            found = await linear.project_ids(credential, slugs)
    except TimeoutError:
        raise SourceUnreachable(f"the tracker did not answer within {limit:g} s") from None
    except linear.CredentialRefused:
        raise CredentialRefused("the tracker refused the key") from None
    except linear.LinearError:
        raise SourceUnreachable("the tracker could not look the projects up") from None
    lost = [s for s in slugs if s not in found]
    if lost:
        raise SourceUnreachable(
            f"project {lost[0]}: not found; the key's account must see it", lost[0]
        )
    return [{"type": "project", "id": found[i["slug_id"]]} if "slug_id" in i else i for i in items]


__all__ = [
    "PROBES",
    "CredentialRefused",
    "Probe",
    "SourceUnreachable",
    "probe",
    "resolve",
    "sample",
]
