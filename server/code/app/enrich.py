"""Result enrichment: what a checkout can't contain.

Attached to code.symbol (and optionally code.search top hits): the last merged PRs that
touched the symbol's file, usage examples ranked by test coverage + recency, and the
memories linked to the symbol (`server.memory.app.code_links`).

Every enrichment is bounded (`code_enrich_timeout_s`) and degrades to an empty block —
enrichment must never fail or slow the core answer.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable
from typing import Any

from server.code.store.index import CodeRepo
from server.settings import settings

logger = logging.getLogger(__name__)


async def bounded[T](coro: Awaitable[T], fallback: T, seconds: float | None = None) -> T:
    """`coro`'s answer within `seconds` (`code_enrich_timeout_s` when None), else
    `fallback`."""
    try:
        return await asyncio.wait_for(
            coro, timeout=settings.code_enrich_timeout_s if seconds is None else seconds
        )
    except Exception as e:  # noqa: BLE001 — enrichment degrades, never fails
        logger.debug("code enrich degraded: %s", e)
        return fallback


async def recent_prs(
    repo: CodeRepo, org: str, ns: str, paths: list[str], *, limit: int = 5
) -> list[dict[str, Any]]:
    rows = await bounded(repo.pr_history_for(org, ns, paths=paths, limit=limit), [])
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
    edges = await bounded(
        repo.edges_for_symbols(org, ns, symbol_ids, direction="callers", limit=50),
        [],
    )
    if not edges:
        return []
    caller_paths = sorted({str(e["path"]) for e in edges})  # fixed order into the 50-cut
    tested = await bounded(repo.test_map_for(org, ns, caller_paths), [])
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


async def lessons_for_symbol(
    org: str,
    ns: str,
    *,
    symbol_name: str,
    paths: list[str],
    extra_namespaces: list[str],
    limit: int = 3,
) -> list[dict[str, Any]]:
    """The memories linked to the symbol, from the caller's lesson scope: the readable
    `extra_namespaces` the call names (over MCP its memory home) and the repository's
    team namespace.
    Each carries `code_state`: `changed` when this branch's definition differs from the
    one the memory was written against. Any failure -> []."""
    from server.memory.app import code_plane_reads

    async def read() -> list[dict[str, Any]]:
        nss = await code_plane_reads.lesson_namespaces(org, ns, extra_namespaces)
        return await code_plane_reads.lessons_for_symbol(
            org,
            ns,
            qualified_name=symbol_name,
            paths=paths,
            namespaces=nss,
            limit=limit,
        )

    return await bounded(read(), [])
