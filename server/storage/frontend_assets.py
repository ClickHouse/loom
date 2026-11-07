"""The dashboard bundle shared across builds, for rolling upgrades.

A page served by build N asks for `/assets/<hash>.js`; behind one URL with no
sticky sessions that request can land on a replica still running N-1, whose
disk has only its own bundle. Every replica therefore publishes its
`frontend/dist/assets` files into `loom.frontend_assets` (keyed by path; Vite
names are content-hashed, so a path names one body), and `/assets/<path>`
falls back to that table when a content-hashed file is not on local disk.

Rows carry `published_at`; the table's TTL drops a build's files 30 days
after the last replica of that build republished them (each replica
republishes every `_REPUBLISH_S`). A missing table logs once and serves the
404 the route answered before.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import mimetypes
import re
import time
from collections import OrderedDict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from starlette.responses import Response

from server.core.datetimes import naive_utc_now
from server.settings import settings

log = logging.getLogger(__name__)

TABLE = "loom.frontend_assets"
# A bundle file above this is not published (the dashboard's chunks are well under).
MAX_ASSET_BYTES = 5 * 1024 * 1024
# A row younger than this with the same sha is not rewritten: N replicas
# booting together write each file about once.
_FRESH = timedelta(hours=6)
# How often a running replica republishes, so a live build never ages out.
_REPUBLISH_S = 12 * 3600.0
# Vite's `[name]-[hash].[ext]`: the hash makes the name a version.
_HASHED = re.compile(r"-[A-Za-z0-9_-]{8,}\.[A-Za-z0-9]+$")
_MAX_NAME_CHARS = 255  # not a setting: a file system's longest file name
# Fetched bodies, by path (content-addressed, so never stale).
_CACHE: OrderedDict[str, tuple[str, bytes]] = OrderedDict()
_CACHE_MAX_BYTES = 32 * 1024 * 1024
_cache_bytes = 0
# Paths the table did not have, with when to ask again: a probe of random names
# costs one read per name a minute, not one per request.
_MISSES: OrderedDict[str, float] = OrderedDict()
_MISS_TTL_S = 60.0
_MISSES_MAX = 1024
_table_missing_warned = False


def is_hashed_asset(path: str) -> bool:
    """True when `path`'s file name carries a content hash (safe to cache forever).
    A name longer than a file system allows is no bundle file, and is refused
    before the search, which costs the square of the name's length."""
    name = path.rsplit("/", 1)[-1]
    return len(name) <= _MAX_NAME_CHARS and bool(_HASHED.search(name))


def content_type_for(path: str) -> str:
    guessed, _ = mimetypes.guess_type(path)
    if path.endswith(".js") or path.endswith(".mjs"):
        return "text/javascript"
    return guessed or "application/octet-stream"


def _warn_table_missing(e: Exception) -> None:
    global _table_missing_warned
    if not _table_missing_warned:
        _table_missing_warned = True
        log.warning("frontend assets: %s unavailable (%s); serving local files only", TABLE, e)


def local_assets(assets_dir: Path) -> list[tuple[str, Path]]:
    """`(published path, file)` for every bundle file under `assets_dir`."""
    if not assets_dir.is_dir():
        return []
    out = []
    for f in sorted(assets_dir.rglob("*")):
        if f.is_file():
            out.append((f.relative_to(assets_dir).as_posix(), f))
    return out


async def publish(assets_dir: Path, *, published_by: str = "", ch: Any = None) -> int | None:
    """Write this build's bundle files the table lacks (or holds stale). Returns
    the rows written (0: every file is there already), None when the table could
    not be read or written; never raises."""
    from server.core.ch import get_client

    files = local_assets(assets_dir)
    if not files:
        return 0
    ch = ch or get_client()
    try:
        have = await ch.query_all(
            f"SELECT path, sha256, published_at FROM {TABLE} FINAL "
            "WHERE path IN {paths:Array(String)}",
            {"paths": [p for p, _ in files]},
        )
    except Exception as e:  # noqa: BLE001 — the loop retries
        _warn_table_missing(e)
        return None
    known = {str(r["path"]): (str(r["sha256"]), r["published_at"]) for r in have}
    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    for path, f in files:
        try:
            body = f.read_bytes()
        except OSError:
            continue
        if not body:
            continue
        if len(body) > MAX_ASSET_BYTES:
            log.info("frontend assets: %s is %d bytes; not published", path, len(body))
            continue
        sha = hashlib.sha256(body).hexdigest()
        prior = known.get(path)
        if prior and prior[0] == sha and isinstance(prior[1], datetime):
            if now - prior[1].replace(tzinfo=None) < _FRESH:
                continue
        rows.append(
            {
                "path": path,
                "sha256": sha,
                "body": body,
                "size": len(body),
                "published_by": published_by,
                "published_at": now,
            }
        )
    if not rows:
        return 0
    try:
        await ch.insert(TABLE, rows, settings={"async_insert": 0})
    except Exception as e:  # noqa: BLE001 — the loop retries
        _warn_table_missing(e)
        return None
    return len(rows)


async def publish_loop(
    assets_dir: Path,
    *,
    published_by: str = "",
    ch: Any = None,
    first_done: asyncio.Event | None = None,
) -> None:
    """Publish now, then every `_REPUBLISH_S` while the replica runs; a failed
    round is retried within `storage_frontend_assets_retry_max_s`. `first_done` is set once a round
    has found every file in the table or written it there."""
    from server.core.ch import set_query_tag

    set_query_tag("frontend_assets.publish")
    retry = settings.storage_frontend_assets_retry_s
    while True:
        n = await publish(assets_dir, published_by=published_by, ch=ch)
        if n is None:
            await asyncio.sleep(retry)
            retry = min(retry * 2, settings.storage_frontend_assets_retry_max_s)
            continue
        retry = settings.storage_frontend_assets_retry_s
        if n:
            log.info("frontend assets: published %d bundle file(s)", n)
        if first_done is not None:
            first_done.set()
        await asyncio.sleep(_REPUBLISH_S)


def _remember(path: str, entry: tuple[str, bytes]) -> None:
    global _cache_bytes
    _CACHE[path] = entry
    _cache_bytes += len(entry[1])
    while _cache_bytes > _CACHE_MAX_BYTES and _CACHE:
        _, (_, old) = _CACHE.popitem(last=False)
        _cache_bytes -= len(old)


def _missed_recently(path: str) -> bool:
    until = _MISSES.get(path)
    if until is None:
        return False
    if time.monotonic() < until:
        return True
    del _MISSES[path]
    return False


def _remember_miss(path: str) -> None:
    _MISSES[path] = time.monotonic() + _MISS_TTL_S
    _MISSES.move_to_end(path)
    while len(_MISSES) > _MISSES_MAX:
        _MISSES.popitem(last=False)


async def fetch(path: str, *, ch: Any = None) -> tuple[str, bytes] | None:
    """`(content_type, body)` of a bundle file another build published, or None.
    Only a content-hashed name is looked up: no other name is another build's."""
    if path in _CACHE:
        _CACHE.move_to_end(path)
        return _CACHE[path]
    if not is_hashed_asset(path) or ".." in path.split("/") or _missed_recently(path):
        return None
    from server.core.ch import get_client, set_query_tag

    ch = ch or get_client()
    set_query_tag("frontend_assets.fetch")
    try:
        # Content-hashed, so every row of a path holds the same body: no FINAL.
        body: bytes = await ch.raw_bytes(
            f"SELECT body FROM {TABLE} WHERE path = {{p:String}} LIMIT 1 FORMAT RawBLOB",
            {"p": path},
        )
    except Exception as e:  # noqa: BLE001 — a missing table serves the 404 it always did
        _warn_table_missing(e)
        body = b""
    if not body:
        _remember_miss(path)
        return None
    entry = (content_type_for(path), body)
    _remember(path, entry)
    return entry


async def serve_asset(path: str, *, ch: Any = None) -> Response | None:
    """A Response for `/assets/<path>` from the shared table, or None (the route's 404)."""

    found = await fetch(path, ch=ch)
    if found is None:
        return None
    content_type, body = found
    return Response(
        content=body,
        media_type=content_type,
        headers={"Cache-Control": cache_control_for(path)},
    )


def cache_control_for(path: str) -> str:
    """Forever for a content-hashed name; revalidate anything else."""
    return "public, max-age=31536000, immutable" if is_hashed_asset(path) else "no-cache"


def cache_clear() -> None:
    """Test hook."""
    global _cache_bytes, _table_missing_warned
    _CACHE.clear()
    _MISSES.clear()
    _cache_bytes = 0
    _table_missing_warned = False
