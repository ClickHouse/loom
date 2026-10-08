"""code.enclosing: the innermost definition containing `path:line`, with its
body, the document sections for a prose file and the neighbour block.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from server.code.api.models_navigation import CodeEnclosingInput, CodeEnclosingOutput
from server.code.app.common import (
    index_as_of,
    repo_relative,
    require_code_intel,
    resolve_code_identity,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.neighbours import _attach_enclosing_neighbours
from server.code.app.symbol.resolve import _innermost, symbol_body
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)


_ENCLOSING_LOC = re.compile(r"^(?P<path>[^:\s]+):(?P<line>\d+)$")


# The symbol kinds a `path:line` resolves into (SQL statements included); shared with
# code.grep's neighbours block.
ENCLOSING_KINDS: tuple[str, ...] = (
    "function",
    "method",
    "fixture",
    "statement",
    "job",
    "target",
    "stage",
    "query",
    "changeset",
)


async def handle_code_enclosing(input: CodeEnclosingInput) -> CodeEnclosingOutput:
    """Batched `path:line` -> enclosing function, one symbol-table read per 20
    distinct paths; a line inside no function falls back to the class or
    struct whose body holds it (`kind` says which). Fail-soft per location:
    an unparseable or unindexed location yields symbol "" rather than
    failing the batch, because the caller (the grep shim) is decorating
    exact output it must never lose."""
    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()

    parsed: list[tuple[str, int] | None] = []
    for loc in input.locations:
        m = _ENCLOSING_LOC.match(loc.strip())
        parsed.append((repo_relative(m.group("path")), int(m.group("line"))) if m else None)
    paths = list(dict.fromkeys(p for p, _ in (x for x in parsed if x)))

    with code_span("enclosing.symbols", paths=len(paths)) as sp:
        # a SQL test's line encloses a statement record
        rows = await repo.functions_for_paths(org, ns, paths, limit=2000, kinds=ENCLOSING_KINDS)
        span_attrs(sp, rows=len(rows))
    by_path: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_path.setdefault(str(r["path"]), []).append(r)

    # Second pass, only for lines no function contains: the class / struct
    # whose body holds the line (a member declaration in a header). One
    # extra read for just those paths; a batch of .cpp hits pays nothing.
    unresolved_paths = list(
        dict.fromkeys(
            p
            for p, line in (x for x in parsed if x)
            if _innermost(by_path.get(p, []), line) is None
        )
    )
    types_by_path: dict[str, list[dict[str, Any]]] = {}
    if unresolved_paths:
        with code_span("enclosing.types", paths=len(unresolved_paths)) as sp:
            trows = await repo.types_for_paths(org, ns, unresolved_paths, limit=2000)
            span_attrs(sp, rows=len(trows))
        for r in trows:
            types_by_path.setdefault(str(r["path"]), []).append(r)
    # A line of a document or config file is enclosed by its heading's section (the chunk
    # the index cut there), not by nothing.
    sections_by_path = await _doc_sections(
        repo, org, ns, [p for p in unresolved_paths if _types_miss(p, parsed, types_by_path)]
    )

    results: list[dict[str, Any]] = []
    resolved = 0
    for loc, pl in zip(input.locations, parsed, strict=True):
        if pl is None:
            results.append(
                {
                    "path": loc,
                    "line": 0,
                    "symbol": "",
                    "qualified_name": "",
                    "signature": "",
                    "tier": "",
                    "kind": "",
                    "start_line": 0,
                    "end_line": 0,
                }
            )
            continue
        path, line = pl
        hit = _innermost(by_path.get(path, []), line)
        if hit is None:
            hit = _innermost(types_by_path.get(path, []), line)
        if hit is None:
            hit = _innermost(sections_by_path.get(path, []), line)
        if hit is None:
            results.append(
                {
                    "path": path,
                    "line": line,
                    "symbol": "",
                    "symbol_id": "",
                    "qualified_name": "",
                    "signature": "",
                    "tier": "",
                    "kind": "",
                    "start_line": 0,
                    "end_line": 0,
                }
            )
            continue
        resolved += 1
        results.append(
            {
                "path": path,
                "line": line,
                "symbol": str(hit.get("name") or str(hit["qualified_name"]).rsplit("::", 1)[-1]),
                "symbol_id": str(hit.get("symbol_id") or ""),
                "qualified_name": str(hit["qualified_name"]),
                "signature": str(hit.get("signature") or "")[:300],
                "tier": str(hit.get("tier") or ""),
                "kind": str(hit.get("kind") or "function"),
                "start_line": int(hit.get("start_line") or 0),
                "end_line": int(hit.get("end_line") or 0),
            }
        )

    bodies_returned = bodies_elided = tokens = 0
    if input.include_body and resolved:
        bodies_returned, bodies_elided, tokens = await _attach_enclosing_bodies(
            repo, org, ns, results, input.token_budget
        )

    want_neighbours = bool(input.neighbours)  # unset = off
    neighbours: list[dict[str, Any]] = []
    anchors_capped = False
    neighbour_tokens = 0
    if want_neighbours and resolved:
        neighbours, anchors_capped, neighbour_tokens = await _attach_enclosing_neighbours(
            repo, org, ns, results, rows, input.neighbour_k
        )

    took_ms = int((time.monotonic() - started) * 1000)
    set_code_span_attributes(
        route="enclosing",
        results=resolved,
        seeds=len(input.locations),
        chunks_matched=len(by_path),
        abstain=resolved == 0,
        tokens_returned=tokens + neighbour_tokens,
        retrieval_risk="low" if resolved else "abstain",
        consumer=input.consumer,
    )
    return CodeEnclosingOutput(
        results=results,
        resolved=resolved,
        paths_indexed=len(set(by_path) | set(sections_by_path)),
        paths_total=len(paths),
        took_ms=took_ms,
        as_of=await index_as_of(repo, org, ns),
        bodies_returned=bodies_returned,
        bodies_elided=bodies_elided,
        tokens_returned=tokens + neighbour_tokens,
        neighbours=neighbours,
        anchors=len(neighbours),
        anchors_capped=anchors_capped,
        neighbour_tokens=neighbour_tokens,
    )


# Files whose lines have no function to sit in; their index chunks are heading sections
# (Markdown, reST) or key blocks (config).
_SECTION_SUFFIXES = (
    ".md",
    ".mdx",
    ".markdown",
    ".rst",
    ".txt",
    ".adoc",
    ".yml",
    ".yaml",
    ".toml",
    ".json",
    ".ini",
    ".cfg",
)


_PROSE_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".txt", ".adoc")


def _types_miss(
    path: str, parsed: list[tuple[str, int] | None], types_by_path: dict[str, list[dict[str, Any]]]
) -> bool:
    """A document/config path with a line that no class body holds either."""
    if not path.lower().endswith(_SECTION_SUFFIXES):
        return False
    return any(
        p == path and _innermost(types_by_path.get(p, []), line) is None
        for p, line in (x for x in parsed if x)
    )


def section_rows(spans: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    """Chunk spans by path (`chunk_spans_for_paths`) -> enclosing rows. The
    heading is the chunk's last scope segment (`doc::Title::Context` ->
    `Context`); a prose file's span is a `section`, a config file's a `block`."""
    out: dict[str, list[dict[str, Any]]] = {}
    for path, chunks in spans.items():
        prose = path.lower().endswith(_PROSE_SUFFIXES)
        for c in chunks:
            scope = str(c.get("scope") or "")
            out.setdefault(path, []).append(
                {
                    "path": path,
                    # a config chunk has no scope: name the block by its file
                    "name": scope.rsplit("::", 1)[-1]
                    if "::" in scope
                    else (scope or f"{path.rsplit('/', 1)[-1]} block"),
                    "qualified_name": scope,
                    "symbol_id": "",
                    "signature": "",
                    "tier": "chunk",
                    "kind": "section" if prose else "block",
                    "start_line": int(c.get("start_line") or 0),
                    "end_line": int(c.get("end_line") or 0),
                }
            )
    return out


async def _doc_sections(
    repo: CodeRepo, org: str, ns: str, paths: list[str]
) -> dict[str, list[dict[str, Any]]]:
    if not paths:
        return {}
    try:
        with code_span("enclosing.sections", paths=len(paths)) as sp:
            chunks = await repo.chunk_spans_for_paths(org, ns, paths)
            span_attrs(sp, rows=sum(len(v) for v in chunks.values()))
    except Exception:
        logger.warning("code.enclosing section read failed", exc_info=True)
        return {}
    return section_rows(chunks)


_ENCLOSING_BODY_MAX_SYMBOLS = 50


_ENCLOSING_BODY_CONCURRENCY = 8


async def _attach_enclosing_bodies(
    repo: CodeRepo, org: str, ns: str, results: list[dict[str, Any]], token_budget: int
) -> tuple[int, int, int]:
    """Fetch each distinct enclosing function's body once and attach it to
    every hit inside it, filling by first appearance until the budget is
    spent. Whole units only (a body is never cut): a function that does not
    fit ships `body == ""`, `body_elided`, and the code.symbol call that
    fetches it. Returns (bodies_returned, bodies_elided, tokens)."""
    from server.code.domain.units import estimate_tokens

    # distinct symbols in first-appearance order — ten hits in one function
    # cost one body read; hits beyond the cap are elided, never dropped
    order: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in results:
        sid = str(r.get("symbol_id") or "")
        if sid and sid not in seen:
            seen.add(sid)
            order.append(r)
    fetch = order[:_ENCLOSING_BODY_MAX_SYMBOLS]

    sem = asyncio.Semaphore(_ENCLOSING_BODY_CONCURRENCY)

    async def one(r: dict[str, Any]) -> str:
        async with sem:
            try:
                return await symbol_body(repo, org, ns, r)
            except Exception:  # noqa: BLE001 — fail-soft: the name still ships
                logger.warning("code.enclosing body fetch failed for %s", r.get("symbol_id"))
                return ""

    with code_span("enclosing.bodies", results=len(fetch)):
        bodies = await asyncio.gather(*(one(r) for r in fetch))

    served: dict[str, str] = {}
    used = 0
    for r, body in zip(fetch, bodies, strict=True):
        cost = estimate_tokens(body)
        if body and used + cost <= token_budget:
            used += cost
            served[str(r["symbol_id"])] = body

    returned = elided = 0
    for r in results:
        sid = str(r.get("symbol_id") or "")
        if not sid:
            continue
        body = served.get(sid, "")
        r["body"] = body
        r["body_elided"] = not body
        r["next"] = "" if body else f'code.symbol(symbol_id="{sid}")'
    for r in order:
        if str(r["symbol_id"]) in served:
            returned += 1
        else:
            elided += 1
    return returned, elided, used
