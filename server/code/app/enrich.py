"""Result enrichment: what a checkout can't contain.

Attached to code.symbol (and optionally code.search top hits): the last merged PRs that
touched the symbol's file, usage examples ranked by test coverage + recency, and lessons
fused from the memory plane (public lesson namespaces, matched on the symbol name +
files facet).

Every enrichment is bounded (~150ms) and degrades to an empty block —
enrichment must never fail or slow the core answer.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable
from typing import Any

from server.code.app.ttl_cache import TtlCache
from server.code.store.index import CodeRepo

logger = logging.getLogger(__name__)

_ENRICH_TIMEOUT_S = 0.5


async def _bounded[T](coro: Awaitable[T], fallback: T) -> T:
    try:
        return await asyncio.wait_for(coro, timeout=_ENRICH_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001 — enrichment degrades, never fails
        logger.debug("code enrich degraded: %s", e)
        return fallback


async def recent_prs(
    repo: CodeRepo, org: str, ns: str, paths: list[str], *, limit: int = 5
) -> list[dict[str, Any]]:
    rows = await _bounded(repo.pr_history_for(org, ns, paths=paths, limit=limit), [])
    return [
        {
            "pr_number": int(r["pr_number"]),
            "title": str(r["title"]),
            "author": str(r["author"]),
            "merged_at": str(r["merged_at"]),
            # a revert touching this path: the area has bitten before
            "is_revert": bool(r.get("is_revert")),
        }
        for r in rows
    ]


async def usage_examples(
    repo: CodeRepo, org: str, ns: str, symbol_ids: list[str], *, limit: int = 3
) -> list[dict[str, Any]]:
    """Call sites of the symbol, test-covered callers first, newest first."""
    edges = await _bounded(
        repo.edges_for_symbols(org, ns, symbol_ids, direction="callers", limit=50),
        [],
    )
    if not edges:
        return []
    caller_paths = sorted({str(e["path"]) for e in edges})  # fixed order into the 50-cut
    tested = await _bounded(repo.test_map_for(org, ns, caller_paths), [])
    tested_paths = {str(t["src_path"]) for t in tested}
    ranked = sorted(
        edges,
        key=lambda e: (str(e["path"]) not in tested_paths, str(e["path"])),
    )
    out = []
    seen: set[tuple[str, int]] = set()
    for e in ranked:
        site = (str(e["path"]), int(e["line"]))
        if site in seen:
            continue
        seen.add(site)
        out.append(
            {
                "caller": str(e["from_qualified_name"]),
                "path": str(e["path"]),
                "line": int(e["line"]),
                "tier": str(e["tier"]),
                "test_covered": str(e["path"]) in tested_paths,
            }
        )
        if len(out) >= limit:
            break
    return out


# ---- the lesson corpus is read once per TTL, not per call ----
# A `memory_embeddings FINAL` scan of the lesson namespaces per code.symbol call costs
# ~166 ms on a 12k-row namespace, the longest leg of the call. The corpus is small and
# changes slowly, so each process keeps it in memory with a token inverted index and
# answers the lexical match locally; the scan remains the fallback when the corpus
# cannot be loaded.
_TOKEN_RE = re.compile(r"[0-9A-Za-z]+")
_LESSON_CACHE: TtlCache[
    tuple[str, tuple[str, ...]],
    tuple[list[dict[str, Any]], dict[str, list[int]], dict[str, list[int]]],
] = TtlCache(300.0, maxsize=64)


def _lesson_tokens(text: str) -> list[str]:
    """hasToken's tokenisation: maximal runs of ASCII alphanumerics, lowered."""
    return [t.lower() for t in _TOKEN_RE.findall(text or "")]


async def _lesson_corpus(
    org: str,
    nss: tuple[str, ...],
) -> tuple[list[dict[str, Any]], dict[str, list[int]], dict[str, list[int]]]:
    """(rows, token → row indexes, file → row indexes) for the lesson
    namespaces, loaded at most once per `code_lesson_corpus_ttl_seconds`
    per process; a failed load raises so the caller falls back to the scan."""
    from server.settings import settings

    key = (org, nss)
    hit = _LESSON_CACHE.get(key)
    if hit is not None:
        return hit
    from server.memory.app.code_plane_reads import lesson_corpus

    rows = await lesson_corpus(org, nss, cap=int(settings.code_lesson_corpus_max_rows))
    by_token: dict[str, list[int]] = {}
    by_file: dict[str, list[int]] = {}
    kept: list[dict[str, Any]] = []
    for r in rows:
        i = len(kept)
        kept.append(
            {
                "memory_key": str(r["memory_key"]),
                "namespace": str(r["namespace"]),
                "content_excerpt": str(r.get("content_excerpt") or ""),
            }
        )
        for t in set(_lesson_tokens(str(r.get("content_excerpt_lc") or ""))):
            by_token.setdefault(t, []).append(i)
        for f in r.get("files") or ():
            by_file.setdefault(str(f), []).append(i)
    _LESSON_CACHE.put(
        key, (kept, by_token, by_file), ttl_s=float(settings.code_lesson_corpus_ttl_seconds)
    )
    return kept, by_token, by_file


def match_lessons(
    rows: list[dict[str, Any]],
    by_token: dict[str, list[int]],
    by_file: dict[str, list[int]],
    *,
    symbol_name: str,
    paths: list[str],
    limit: int,
) -> list[dict[str, Any]]:
    """The scan's predicate over the cached corpus: rows whose excerpt
    carries every token of the symbol's bare name, or whose files facet
    names one of `paths`; shortest excerpts first (the rows are loaded in
    that order), `limit` of them. Pure."""
    needle = _lesson_tokens(symbol_name.split("::")[-1])
    idx: set[int] = set()
    if needle:
        sets = [set(by_token.get(t, ())) for t in needle]
        idx |= set.intersection(*sets) if sets else set()
    for p in paths[:20]:
        idx.update(by_file.get(p, ()))
    return [
        {
            "memory_key": rows[i]["memory_key"],
            "namespace": rows[i]["namespace"],
            "excerpt": rows[i]["content_excerpt"][:400],
        }
        for i in sorted(idx)[:limit]
    ]


async def lessons_for_symbol(
    org: str,
    ns_lessons: list[str],
    *,
    symbol_name: str,
    paths: list[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """Fuse lessons from the memory plane's public lesson namespaces: lexical match on the symbol
    name, boosted by files-facet overlap. Served from the per-process lesson corpus; the
    FINAL scan answers when the corpus cannot load. Any failure -> []."""
    if ns_lessons:
        corpus = await _bounded(_lesson_corpus(org, tuple(ns_lessons)), None)
        if corpus is not None:
            rows, by_token, by_file = corpus
            return match_lessons(
                rows, by_token, by_file, symbol_name=symbol_name, paths=paths, limit=limit
            )
        logger.debug("lesson corpus unavailable; scanning the lesson namespaces")

    async def _search() -> list[dict[str, Any]]:
        from server.memory.app.code_plane_reads import lessons_matching

        rows = await lessons_matching(
            org,
            ns_lessons,
            token=symbol_name.split("::")[-1].lower(),
            paths=paths[:20],
            limit=limit * 2,
        )
        out = []
        for r in rows[:limit]:
            out.append(
                {
                    "memory_key": str(r["memory_key"]),
                    "namespace": str(r["namespace"]),
                    "excerpt": str(r["content_excerpt"])[:400],
                }
            )
        return out

    if not ns_lessons:
        return []
    return await _bounded(_search(), [])
