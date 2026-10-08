"""The graph evidence block beside a hit (code.enclosing, code.grep, code.symbol):
one-hop callers, callees, tests and citing documents of the anchors, and the documents
that cite a definition.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from server.code.app.spans import code_span, span_attrs
from server.code.app.symbol.coverage import _scip_coverage
from server.code.domain.index.epistemic import classify
from server.code.store.index import CodeRepo
from server.code.store.index import prefer_scip_edges as _prefer_scip_edges

logger = logging.getLogger(__name__)


# The graph evidence block: the agent's lexical hits become graph anchors and a
# confidence-filtered one-hop expansion is shown beside them. Bounded anchors: the callee
# read is the bloom-index shape on code_edges, so a tree-wide grep for a common word must
# not fan out into hundreds of them.
_NEIGHBOUR_MAX_ANCHORS = 20


_NEIGHBOUR_SKIP_KINDS = frozenset({"class", "struct", "enum"})


# The parser tier's repo-wide name guesses (0.6) are where `parse` / `check`
# collisions live; the block is evidence, so it carries only edges the
# resolver pinned to one definition (same-file 1.0, enclosing class 0.8) or
# the compiler saw (1.0).
_NEIGHBOUR_MIN_CONFIDENCE = 0.8


# A document's citation of a symbol is a `reads` edge at 0.7 (a unique bare name or a
# qualified suffix); the block and code.symbol show it as `docs` / `documented_in`. Only
# edges written FROM a document count: SQL statements also write `reads` edges (to tables).
_DOCS_MIN_CONFIDENCE = 0.6


_DOC_SUFFIXES = (".md", ".mdx", ".markdown", ".rst", ".adoc")


def is_doc_path(path: str) -> bool:
    return path.lower().endswith(_DOC_SUFFIXES)


_DOCUMENTED_IN_MAX = 5


async def documented_in_for(
    repo: CodeRepo, org: str, ns: str, rows: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """The sections whose text cites one of `rows` (every tier's id of the resolved
    definitions), from the Markdown lane's `reads` edges: one keyed read on
    `to_symbol_id`; distinct sections in the read's confidence order, at most five.
    Returns [] for a namespace without documents, at the cost of that one read."""
    ids = list(
        dict.fromkeys(str(r.get("symbol_id") or "") for r in rows[:20] if r.get("symbol_id"))
    )
    if not ids:
        return []
    edges = await repo.edges_for_symbols(
        org,
        ns,
        ids,
        direction="callers",
        edge_kinds=["reads"],
        limit=_DOCUMENTED_IN_MAX * 4,
        min_confidence=_DOCS_MIN_CONFIDENCE,
        per_match=_DOCUMENTED_IN_MAX * 2,
    )
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for e in edges:
        path = str(e.get("path") or "")
        section = str(e.get("from_qualified_name") or "")
        if not is_doc_path(path) or not section or section in seen:
            continue
        seen.add(section)
        out.append({"section": section, "path": path, "line": int(e.get("line") or 0)})
        if len(out) >= _DOCUMENTED_IN_MAX:
            break
    return out


def _is_plumbing_callee(qualified: str) -> bool:
    """A constructor (`A::B::B`), an operator overload or a logger fetch is a call every
    function makes and no agent navigates to: unfiltered, a third of the callee slots went
    to `DB::Exception::Exception`, `FormatStringHelperImpl::…`, `operator[]`,
    `getLogger`. Callers are not filtered: a constructor that calls the anchor is a real
    place in the code."""

    parts = qualified.split("::")
    last = parts[-1]
    if len(parts) >= 2 and last == parts[-2]:
        return True
    return last.startswith("operator") or last in {"getLogger", "get_logger"}


def _prefer_runnable_tests(paths: list[str], k: int) -> list[str]:
    """`k` test paths, runnable files ahead of `.reference` expectations and
    one entry per test stem — the map links both `X.sql` and `X.reference`
    to a source file, and the block should name the test, not its output."""
    ordered = sorted(paths, key=lambda p: p.endswith(".reference"))  # stable
    out: list[str] = []
    stems: set[str] = set()
    for p in ordered:
        stem = p.rsplit("/", 1)[-1].rsplit(".", 1)[0]
        if stem in stems:
            continue
        stems.add(stem)
        out.append(p)
        if len(out) >= k:
            break
    return out


async def _soft(coro: Any, default: Any) -> Any:
    """Await an additive read (a neighbour, documented-in or enrichment block) fail-open: a
    missing block never fails the definitions it decorates."""
    try:
        return await coro
    except Exception as e:  # noqa: BLE001 — additive block, the definitions still ship
        logger.debug("code neighbour/enrichment read skipped: %s", e)
        return default


def module_of(path: str) -> str:
    """The anchor's module for the block: its top two directory segments
    (`src/Interpreters/Foo.cpp` -> `src/Interpreters`, `src/Foo.cpp` -> `src`)."""
    parts = [p for p in path.split("/")[:-1] if p]
    return "/".join(parts[:2])


async def _attach_enclosing_neighbours(
    repo: CodeRepo,
    org: str,
    ns: str,
    results: list[dict[str, Any]],
    function_rows: list[dict[str, Any]],
    k: int,
) -> tuple[list[dict[str, Any]], bool, int]:
    """Per distinct enclosing function (first-appearance order, at most 20): up to `k`
    callers, `k` callees, `k` mapped tests, the module and the epistemic envelope. One
    edge read per direction with the per-anchor cap in SQL, one test-map read, one
    declined-sites read and the compiler-coverage read, gathered, each fail-open. Returns
    (entries, anchors_capped, tokens)."""
    from server.code.domain.external_symbols import is_external_symbol
    from server.code.domain.units import estimate_tokens

    order: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in results:
        sid = str(r.get("symbol_id") or "")
        if not sid or sid in seen or str(r.get("kind") or "") in _NEIGHBOUR_SKIP_KINDS:
            continue
        seen.add(sid)
        order.append(r)
    capped = len(order) > _NEIGHBOUR_MAX_ANCHORS
    anchors = order[:_NEIGHBOUR_MAX_ANCHORS]
    if not anchors:
        return [], capped, 0

    # The two tiers hash one definition to different ids, and edges are
    # keyed by the id of the tier that saw the call: every id the rows in
    # hand carry for an anchor's qualified name is matched, no extra read.
    ids_by_name: dict[str, list[str]] = {}
    for r in function_rows:
        ids_by_name.setdefault(str(r["qualified_name"]), []).append(str(r["symbol_id"]))
    anchor_of: dict[str, str] = {}
    anchor_name: dict[str, str] = {}
    for a in anchors:
        aid = str(a["symbol_id"])
        anchor_name[aid] = str(a["qualified_name"])
        for sid in [aid, *ids_by_name.get(str(a["qualified_name"]), [])]:
            anchor_of.setdefault(sid, aid)
    ids = list(anchor_of)
    paths = list(dict.fromkeys(str(a["path"]) for a in anchors))
    names = [n for n in dict.fromkeys(str(a.get("symbol") or "") for a in anchors) if n]

    async def edges(direction: str) -> list[dict[str, Any]]:
        rows = await repo.edges_for_symbols(
            org,
            ns,
            ids,
            direction=direction,
            edge_kinds=["calls"],
            limit=len(ids) * k * 4,
            min_confidence=_NEIGHBOUR_MIN_CONFIDENCE,
            # headroom per anchor for the rows the tier collapse and the
            # external / plumbing filters below drop (callees lose more);
            # the SQL order (tier, confidence) decides which survive the cap
            per_match=k * (3 if direction == "callees" else 2),
        )
        return _prefer_scip_edges(rows)

    async def citing_docs() -> list[dict[str, Any]]:
        # The documents whose sections cite the anchor: `reads` edges written by the
        # Markdown lane, matched on the anchor's ids like callers (to_symbol_id is in the
        # sort key).

        rows = await repo.edges_for_symbols(
            org,
            ns,
            ids,
            direction="callers",
            edge_kinds=["reads"],
            limit=len(ids) * k * 2,
            min_confidence=_DOCS_MIN_CONFIDENCE,
            per_match=k * 2,
        )
        return [r for r in rows if is_doc_path(str(r.get("path") or ""))]

    with code_span("enclosing.neighbours", results=len(anchors), depth=k) as sp:
        callers, callees, docs, tmap, misses, scip_paths = await asyncio.gather(
            _soft(edges("callers"), []),
            _soft(edges("callees"), []),
            _soft(citing_docs(), []),
            _soft(repo.test_map_for(org, ns, paths, limit=len(paths) * k * 2), []),
            _soft(repo.call_misses_for_names(org, ns, names), {}),
            _scip_coverage(repo, org, ns, paths, anchors),
        )
        span_attrs(sp, edges=len(callers) + len(callees), rows=len(tmap), docs=len(docs))

    def fold(
        rows: list[dict[str, Any]], match_col: str, name_col: str, *, plumbing: bool
    ) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for r in rows:
            aid = anchor_of.get(str(r[match_col]))
            if aid is None:
                continue
            name = str(r[name_col])
            if is_external_symbol(name) or name == anchor_name[aid]:
                continue
            if not plumbing and _is_plumbing_callee(name):
                continue
            lst = out.setdefault(aid, [])
            if name not in lst and len(lst) < k:
                lst.append(name)
        return out

    by_callers = fold(callers, "to_symbol_id", "from_qualified_name", plumbing=True)
    by_callees = fold(callees, "from_symbol_id", "to_qualified_name", plumbing=False)
    by_docs = fold(docs, "to_symbol_id", "from_qualified_name", plumbing=True)
    raw_tests: dict[str, list[str]] = {}
    for r in tmap:  # confidence-ordered by the read
        lst = raw_tests.setdefault(str(r["src_path"]), [])
        tp = str(r["test_path"])
        if tp not in lst:
            lst.append(tp)
    tests_by_path = {p: _prefer_runnable_tests(v, k) for p, v in raw_tests.items()}

    entries: list[dict[str, Any]] = []
    tokens = 0
    for a in anchors:
        aid = str(a["symbol_id"])
        path = str(a["path"])
        m = misses.get(str(a.get("symbol") or ""), {})
        epistemic, causes = classify(
            unresolved=int(m.get("unresolved", 0)),
            ambiguous=int(m.get("ambiguous", 0)),
            seed_paths=[path],
            scip_paths=scip_paths,
        )
        entry = {
            "symbol_id": aid,
            "qualified_name": anchor_name[aid],
            "path": path,
            "module": module_of(path),
            "callers": by_callers.get(aid, []),
            "callees": by_callees.get(aid, []),
            "tests": tests_by_path.get(path, []),
            "docs": by_docs.get(aid, []),
            "epistemic": epistemic,
            "causes": causes,
        }
        tokens += estimate_tokens(json.dumps(entry))
        entries.append(entry)
    return entries, capped, tokens
