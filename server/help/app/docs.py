"""help.search and help.get: the docs this build ships (`server.core.paths.DOCS_DIR`),
indexed once per process.

The index is built on the first call, in a worker thread so the event loop never waits
on it, and kept for the life of the process: the docs are part of the build. A page that
cannot be read is left out; a build with no docs at all answers `available: false`."""

from __future__ import annotations

import asyncio
import logging
import threading
from pathlib import Path

from server.core.paths import DOCS_DIR
from server.help.api.models import (
    HelpGetInput,
    HelpGetOutput,
    HelpHit,
    HelpOutlineEntry,
    HelpSearchInput,
    HelpSearchOutput,
)
from server.help.domain import index as docs_index

log = logging.getLogger(__name__)

# Pages about working on Loom's source, not using or running it.
_SKIPPED_DIRS = ("development/",)
# The most text one help.get returns; a longer section or page is cut and marked truncated.
MAX_TEXT_CHARS = 16_000  # not a setting: bounds one tool result an agent reads

_built: docs_index.Index | None = None
_tried = False
_build_lock = threading.Lock()


def pages(root: Path) -> list[tuple[str, str]]:
    """Every indexed `(path, text)` page under `root`, in path order; a page that cannot
    be read is left out, with a warning."""
    out = []
    for file in sorted(root.rglob("*.md")):
        path = file.relative_to(root).as_posix()
        if path.startswith(_SKIPPED_DIRS):
            continue
        try:
            out.append((path, file.read_text(encoding="utf-8")))
        except (OSError, UnicodeDecodeError) as e:
            log.warning("help leaves out %s, which cannot be read: %s", path, e)
    return out


def _build() -> docs_index.Index | None:
    """Read and index the docs once: the result, none included, is kept for the process,
    since the docs change only with the build."""
    global _built, _tried
    with _build_lock:
        if not _tried:
            try:
                found = pages(DOCS_DIR) if DOCS_DIR.is_dir() else []
                _built = docs_index.build(found) if found else None
                if not found:
                    log.warning("no documentation at %s: help answers unavailable", DOCS_DIR)
            except Exception as e:  # noqa: BLE001 — a docs fault answers unavailable, never a 500
                log.warning("indexing the documentation failed, help answers unavailable: %s", e)
            finally:
                # Set only once the build has ended: a call that comes in meanwhile waits
                # on the lock in its own thread instead of reading no index.
                _tried = True
        return _built


async def index() -> docs_index.Index | None:
    """This process's index, built on the first call; None when no docs can be read."""
    return _built if _tried else await asyncio.to_thread(_build)


def reset() -> None:
    """Forget the index, so the next call reads the docs again (tests)."""
    global _built, _tried
    with _build_lock:
        _built, _tried = None, False


async def handle_help_search(inp: HelpSearchInput) -> HelpSearchOutput:
    ix = await index()
    if ix is None:
        return HelpSearchOutput(available=False, hits=[])
    hits = docs_index.search(ix, inp.query, inp.limit)
    return HelpSearchOutput(
        available=True,
        hits=[
            HelpHit(
                path=h.section.path,
                title=h.section.title,
                section=h.section.section,
                anchor=h.section.anchor,
                audience=h.section.audience,
                snippet=h.snippet,
                score=h.score,
            )
            for h in hits
        ],
    )


async def handle_help_get(inp: HelpGetInput) -> HelpGetOutput:
    ix = await index()
    path = inp.path.strip().removeprefix("docs/")
    if ix is None:
        return HelpGetOutput(available=False, found=False, path=path)
    breadcrumb = docs_index.section_path(inp.section)
    anchor = inp.anchor.strip()
    section = docs_index.section_text(ix, path, breadcrumb, anchor)
    if section is None:
        return HelpGetOutput(available=True, found=False, path=path)
    text = section.text[inp.offset :]
    truncated = len(text) > MAX_TEXT_CHARS
    rest = inp.offset + MAX_TEXT_CHARS if truncated else 0
    # The outline is for finding another section: a page read, or one cut short.
    outline = not (breadcrumb or anchor) or truncated
    return HelpGetOutput(
        available=True,
        found=True,
        path=path,
        title=section.title,
        section=section.section,
        anchor=section.anchor,
        audience=section.audience,
        text=text[:MAX_TEXT_CHARS],
        truncated=truncated,
        next_offset=rest,
        sections=[
            HelpOutlineEntry(section=s.section, anchor=s.anchor)
            for s in docs_index.page_sections(ix, path)
        ]
        if outline
        else [],
    )
