"""Header context for a C/C++ source file's edge resolution: the project headers it
includes, read from the index or the mirror, bounded and cached.

A `.cpp`'s constructor initializes fields its sibling header declares; the walker
binds a member initializer only when it knows the field's type. The ingest resolves
the headers a source file reaches — the sibling `<stem>.h` first, then the sibling's
`#include` targets, then the file's own — reads each one's declarations from its
INDEXED chunks (the header as the index holds it; a header that is not indexed is not
consulted), and stops as soon as no declined site could still be helped or the cap is
reached. Texts are cached per process (the pass cache: `IProcessor.h` is included by
every processor) and forgotten when the header itself is re-ingested.
"""

from __future__ import annotations

import asyncio
import logging
import posixpath
import re
import time
from typing import Any

from server.code.app.index.ingest_stats import FileIngestStats
from server.code.domain.extract.bounded_parse import ParseTimeout
from server.code.domain.extract.edges_treesitter import HeaderDecls, remainder_ctor_sites
from server.code.domain.extract.treesitter import _INCLUDE_RE, ParsedFile
from server.settings import settings

logger = logging.getLogger("loom.code_index.ingest")


HEADER_EXTS = (".h", ".hpp", ".hh", ".hxx")
_SOURCE_EXTS = (".cpp", ".cc", ".cxx")
_HEADER_CAP = 12  # headers read per source file, sibling included
_HEADER_CACHE_MAX = 4096
_header_cache: dict[tuple[str, str, str], str | None] = {}


def headers_first(paths: list[str]) -> list[str]:
    """Header paths before source paths, order otherwise kept — so a fresh
    backfill has indexed `Foo.h` by the time `Foo.cpp` asks for it."""
    return sorted(paths, key=lambda p: 0 if p.endswith(HEADER_EXTS) else 1)


def forget_header(org: str, ns: str, path: str) -> None:
    _header_cache.pop((org, ns, path), None)


def include_candidates(from_path: str, include: str) -> list[str]:
    """Indexed paths an include target may name, nearest first: relative to
    the includer's directory, then to each ancestor directory
    (`<Processors/Chunk.h>` from `src/Processors/Transforms/X.cpp` is
    `src/Processors/Chunk.h`), then the target itself."""
    inc = include.strip().lstrip("/")
    if not inc:
        return []
    out: list[str] = []
    d = posixpath.dirname(from_path)
    while True:
        cand = posixpath.normpath(posixpath.join(d, inc)) if d else posixpath.normpath(inc)
        if cand and not cand.startswith("..") and cand not in out:
            out.append(cand)
        if not d:
            break
        d = posixpath.dirname(d)
    return out


_ELIDED_BODY_RE = re.compile(r"/\* … body elided[^*]*\*/")


def header_text_of_chunks(rows: list[dict[str, Any]]) -> str:
    """A header's text as its live chunks hold it: the chunks in line order,
    a chunk nested in one already taken (a method's own chunk inside its
    class skeleton) skipped, and the skeleton's elision marker put back as
    an empty body so the class parses as one (a declaration followed by a
    comment and no `;` would swallow the fields after it into an error)."""
    kept: list[str] = []
    last_end = -1

    def span(r: dict[str, Any]) -> tuple[int, int]:
        return int(r.get("start_line") or 0), int(r.get("end_line") or 0)

    for r in sorted(rows, key=lambda r: (span(r)[0], -span(r)[1])):
        s, e = span(r)
        if s <= last_end and e <= last_end:
            continue
        kept.append(_ELIDED_BODY_RE.sub("{}", str(r.get("content") or "")))
        last_end = max(last_end, e)
    return "\n".join(kept)


async def _header_text(repo: Any, org: str, ns: str, path: str) -> tuple[str | None, bool]:
    """(text, read from the database?) — None when the index holds no live
    chunk of the path."""
    key = (org, ns, path)
    if key in _header_cache:
        return _header_cache[key], False
    rows = await repo.chunks_of_path(org, ns, path, limit=2000)
    text = header_text_of_chunks(rows) if rows else None
    if len(_header_cache) >= _HEADER_CACHE_MAX:
        _header_cache.pop(next(iter(_header_cache)))
    _header_cache[key] = text
    return text, True


_LIBRARY_PREFIXES = ("std::", "boost::", "absl::", "Poco::", "fmt::")


def _needs_header(pf: ParsedFile, header: HeaderDecls | None) -> bool:
    """Could one more header still change a site? Yes while a declined site
    names something no header read declares, and while a site typed through
    a header names a class whose body no header read holds — the overload
    count of that body is what keeps the repo-wide rung from binding a lone
    indexed overload of a class that declares several (`ITransformingStep`'s
    `= default` copy constructor, indexed from its header before the real
    one's source). A library type is never looked for."""
    for s in remainder_ctor_sites(pf, header):
        if s.decline == "unresolved" and s.need:
            return True
        if (
            header is not None
            and not s.decline
            and s.via
            and s.written
            and not s.written.startswith(_LIBRARY_PREFIXES)
            and header.class_for(s.written) is None
        ):
            return True
    return False


async def _present_includes(
    repo: Any,
    org: str,
    ns: str,
    from_path: str,
    includes: list[str],
) -> list[str]:
    """The indexed paths the `#include` targets name, in include order; one
    membership read over every candidate form."""
    forms = [include_candidates(from_path, i) for i in includes if i.endswith(HEADER_EXTS)]
    wanted = list(dict.fromkeys(p for cands in forms for p in cands))[:500]
    if not wanted:
        return []
    present = await repo.live_paths_present(org, ns, wanted)
    out: list[str] = []
    for cands in forms:
        hit = next((p for p in cands if p in present), None)
        if hit and hit != from_path and hit not in out:
            out.append(hit)
    return out


async def header_context(
    repo: Any,
    org: str,
    ns: str,
    pf: ParsedFile,
    path: str,
    stats: FileIngestStats,
) -> HeaderDecls | None:
    """The declarations of the headers this source file reaches, for the
    sites the file alone could not bind; None when there are none, when the
    path is not a C++ source, or when no reachable header is indexed."""
    if pf.lang != "cpp" or pf.tree is None or not path.endswith(_SOURCE_EXTS):
        return None
    if not _needs_header(pf, None):
        return None
    stem = path.rsplit(".", 1)[0]
    siblings = [stem + ext for ext in HEADER_EXTS]
    present = await repo.live_paths_present(org, ns, siblings)
    queue: list[str] = [p for p in siblings if p in present]
    sibling_set = set(queue)
    includes_done = False
    hd = HeaderDecls()
    read = 0
    # No header of one source starts after its headers have spent one parse deadline,
    # so they spend up to about two
    spent = 0.0
    while read < _HEADER_CAP and spent <= settings.code_parse_timeout_seconds:
        if not queue:
            if includes_done:
                break
            includes_done = True
            # the sibling's includes first: the class's field and base types
            # live there; the source's own includes name what its bodies use
            more: list[str] = []
            for hp in sorted(sibling_set):
                more.extend(_sibling_includes.get((org, ns, hp), ()))
            more.extend(pf.includes)
            for hp in await _present_includes(repo, org, ns, path, more):
                if hp not in hd.paths and hp not in sibling_set:
                    queue.append(hp)
            continue
        hp = queue.pop(0)
        text, from_db = await _header_text(repo, org, ns, hp)
        read += 1
        stats.headers_read += int(from_db)
        if text is None:
            continue
        cost, timed_out = await asyncio.to_thread(_add_header, hd, hp, text)
        spent += cost
        if timed_out:
            _header_cache[(org, ns, hp)] = None  # read as not indexed, not parsed again
            continue
        if hp in sibling_set:
            _sibling_includes[(org, ns, hp)] = tuple(_INCLUDE_RE.findall(text))
        if not _needs_header(pf, hd):
            break
    stats.header_paths = tuple(hd.paths)
    return hd if hd.paths else None


def _add_header(hd: HeaderDecls, path: str, text: str) -> tuple[float, bool]:
    """`hd.add_source` of one header: (the CPU seconds it took, whether its parse ran
    past the deadline)."""
    start = time.thread_time()
    try:
        hd.add_source(path, text.encode("utf-8", "replace"))
    except ParseTimeout:
        return time.thread_time() - start, True
    return time.thread_time() - start, False


# the sibling header's own `#include` list, kept beside its text: the field
# types of `Foo.h`'s class are in the headers `Foo.h` includes
_sibling_includes: dict[tuple[str, str, str], tuple[str, ...]] = {}
