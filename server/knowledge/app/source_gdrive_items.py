"""A `gdrive` source's listing from its items, for the folder turn in
`source_sync`: folders (with `recursive`, every subfolder to MAX_DEPTH, and
MAX_WALK_FOLDERS subfolders per source), shared drives, single files and filters, at most
`gdrive.MAX_PARENTS` parent folders per query.

The listing also says what the items reach (`Listing.reaches`), so a file a
complete listing lacks is retired only when the store says it is outside every
item.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from server.clients import gdrive
from server.knowledge.domain.sources import (
    MAX_DEPTH,
    MAX_WALK_FOLDERS,
    include_mime_types,
    unread_file_note,
)


@dataclass
class Listing:
    files: list[dict[str, Any]] = field(default_factory=list)
    # The store searched everything every query asked for.
    whole: bool = True
    # The pass ran out before every query was asked.
    ran_out: bool = False
    # An item listed nothing, which says nothing is absent (a shared drive the account left).
    lost_item: bool = False
    # Why the listing does not say what is absent (a walk cut at its bound); '' when it does.
    walk_cut: str = ""
    # Items that answered 404, are in the trash, or list nothing the source reads:
    # "item <n>: <reason>".
    notes: list[str] = field(default_factory=list)
    # What the items reach.
    folders: set[str] = field(default_factory=set)
    drives: set[str] = field(default_factory=set)
    file_ids: set[str] = field(default_factory=set)
    # Each filter with the file types it reads: `include` narrowed to its `kinds`.
    filters: list[tuple[dict[str, Any], list[str]]] = field(default_factory=list)
    mime_types: list[str] = field(default_factory=list)

    def reads_kind(self, state: dict[str, Any]) -> bool:
        """Whether `include` names the file's kind; a file whose kind the store did not
        say counts as read."""
        mime = state.get("mimeType")
        return not mime or mime in self.mime_types

    def reaches(self, file_id: str, state: dict[str, Any]) -> bool:
        """Whether a file, as `gdrive.file_state` reads it, is still one an item
        reaches. A filter is matched more broadly than the store matches it (a name
        fragment anywhere), so a doubt keeps the file. A file of a kind `include` no
        longer names is reached by none."""
        if not self.reads_kind(state):
            return False
        if file_id in self.file_ids or str(state.get("driveId") or "") in self.drives:
            return True
        parents = {str(p) for p in state.get("parents") or []}
        if parents & self.folders:
            return True
        return any(_matches(f, mime_types, state) for f, mime_types in self.filters)


def _matches(item: dict[str, Any], mime_types: list[str], state: dict[str, Any]) -> bool:
    if str(state.get("mimeType") or "") not in mime_types:
        return False
    name = str(state.get("name") or "").lower()
    if "name_contains" in item and item["name_contains"].lower() not in name:
        return False
    modified = str(state.get("modifiedTime") or "")
    if "modified_after" in item and modified <= item["modified_after"] + "T00:00:00":
        return False
    return "in" not in item or item["in"] in {str(p) for p in state.get("parents") or []}


async def walk(
    token: str,
    roots: list[str],
    out: Listing,
    *,
    pace_s: float,
    out_of_time: Callable[[], bool] = lambda: False,
) -> None:
    """Add `roots` and their subfolders to `out.folders`, breadth first and level by
    level together, to MAX_DEPTH levels and at most MAX_WALK_FOLDERS subfolders, and
    record on `out` whether the store searched everything, the pass ran out, or the
    walk stopped at a bound with folders left unread."""
    seen = set(roots)
    out.folders.update(roots)
    frontier = list(roots)
    walked = 0
    for _ in range(MAX_DEPTH):
        if out_of_time():
            out.ran_out = True
            return
        # Enough ids to fill the walk's room even if every folder already seen is among
        # them; past that the walk is cut anyway.
        room = MAX_WALK_FOLDERS - walked + len(seen)
        children, complete = await gdrive.subfolders(token, frontier, limit=room, pace_s=pace_s)
        out.whole = out.whole and complete
        frontier = []
        for child in children:
            if child in seen:
                continue
            if walked >= MAX_WALK_FOLDERS:
                out.walk_cut = "more subfolders than a walk reads"
                return
            walked += 1
            seen.add(child)
            out.folders.add(child)
            frontier.append(child)
        if not frontier:
            return
    # The last level is cut only if it holds folders of its own: one page is enough to
    # tell, and an unsure answer counts as cut.
    if out_of_time():
        out.ran_out = True
    elif await gdrive.any_subfolder(token, frontier, seen):
        out.walk_cut = "more subfolders than a walk reads"


async def list_items(
    token: str,
    items: list[dict[str, Any]],
    include: list[str],
    *,
    modified_after: str = "",
    pace_s: float = gdrive.PAGE_PACE_S,
    out_of_time: Callable[[], bool] = lambda: False,
) -> Listing:
    """Every file the items list, each once, or only those changed after
    `modified_after`. A rate limit, a refused credential or a failed query raises;
    a single file or a shared drive that answers 404 is noted and the rest listed.
    The recursive folders are walked on every listing, together, so a new file in a new
    subfolder comes in with the next turn (files moved in keep their change time, so they
    come with the next full listing); once `out_of_time()`, no further walk level,
    folder listing or other item starts, and the listing records that it ran out, so
    the turn keeps its stamp and lists again. A `file` item is one metadata read on
    every listing, an incremental one too."""
    out = Listing(mime_types=include_mime_types(include))
    files: dict[str, dict[str, Any]] = {}

    def add(found: list[dict[str, Any]]) -> None:
        for f in found:
            files.setdefault(str(f.get("id") or ""), f)

    roots = [i["id"] for i in items if i["type"] == "folder" and i.get("recursive")]
    if roots:
        await walk(token, roots, out, pace_s=pace_s, out_of_time=out_of_time)
    out.folders.update(i["id"] for i in items if i["type"] == "folder" and not i.get("recursive"))
    if out.ran_out or out_of_time():
        out.ran_out = True
        return out
    found, whole = await gdrive.list_all(
        token, sorted(out.folders), out.mime_types, modified_after=modified_after, pace_s=pace_s
    )
    add(found)
    out.whole = out.whole and whole
    for n, item in enumerate(items):
        # A folder item was listed above; every other type asks the store.
        if item["type"] != "folder" and out_of_time():
            out.ran_out = True
            break
        if item["type"] == "shared_drive":
            out.drives.add(item["id"])
            try:
                found, whole = await gdrive.list_query(
                    token,
                    gdrive.drive_query(out.mime_types),
                    drive_id=item["id"],
                    modified_after=modified_after,
                    pace_s=pace_s,
                )
            except gdrive.NotFound:
                # A drive the account left lists nothing, which says nothing is absent.
                out.notes.append(f"item {n}: http_404")
                out.lost_item = True
                continue
            add(found)
            out.whole = out.whole and whole
        elif item["type"] == "filter":
            # Add and update refuse a filter whose kinds share none with include.
            mime_types = include_mime_types(include, item.get("kinds"))
            out.filters.append((item, mime_types))
            found, whole = await gdrive.list_query(
                token,
                gdrive.filter_query(item, mime_types),
                modified_after=modified_after,
                pace_s=pace_s,
            )
            add(found)
            out.whole = out.whole and whole
        elif item["type"] == "file":
            out.file_ids.add(item["id"])
            meta = await _file_item(token, n, item["id"], out)
            if meta and not (
                modified_after and str(meta.get("modifiedTime") or "") <= modified_after
            ):
                add([meta])
    out.files = [f for k, f in files.items() if k]
    return out


async def _file_item(token: str, n: int, file_id: str, out: Listing) -> dict[str, Any] | None:
    """The metadata of the `file` item `n`, or None with a note on `out` when it lists
    nothing (gone, trashed, or of a kind the source does not read)."""
    try:
        meta = await gdrive.file_meta(token, file_id)
    except gdrive.NotFound:
        out.notes.append(f"item {n}: http_404")
        return None
    if meta.get("trashed"):
        out.notes.append(f"item {n}: in the trash")
        return None
    if meta.get("mimeType") not in out.mime_types:
        out.notes.append(f"item {n}: {unread_file_note(str(meta.get('mimeType') or ''))}")
        return None
    return meta


__all__ = ["Listing", "list_items", "walk"]
