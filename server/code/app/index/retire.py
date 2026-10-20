"""Retiring a repository index.

`namespace.delete` alone soft-deletes a directory row and nothing else, and
the code tables sit outside the memory erasure saga (they are the repository's
content, not anyone's memory). A retired index must leave no readable rows.

Retiring does four things in order: registry row `active = false` (the worker stops on its next
refresh), directory row `active = false` (the page and tokens stop listing it), an erasure saga over
the namespace (every code_* table keyed by (org, namespace) through the saga's count-then-mutate,
checkpointed, resumed by erasure_recovery, proven by the residue census —
`code_tables_keyed_by_namespace` below is the table scan both the saga step and the handler's count
use), and the bare mirror on disk when no other active entry of the same repository still uses it.
Reads for the namespace are refused the moment the directory row is inactive; the saga runs in the
background.
"""

from __future__ import annotations

import logging
import os
import shutil
from typing import Any

logger = logging.getLogger("loom.code_index.retire")

# Telemetry stays: what a caller searched and what was answered is the
# deployment's audit trail, keyed by namespace but not the repository's content.
KEEP_TABLES: frozenset[str] = frozenset(
    {"loom.code_search_trace", "loom.code_search_outcome", "loom.code_answers"}
)


# The table a refreshable view replaces whole on each refresh (`REFRESH ... TO`,
# no APPEND): the view's header, before its column list. ClickHouse stores the
# target under the configured database's name; the view's own filter keeps it Loom's.
_REFRESH_TARGET = r"\\bREFRESH\\b[^(]*?\\bTO\\s+(?:`[^`]+`|\\w+)\\.`?(\\w+)"
_REFRESH_APPEND = r"\\bREFRESH\\b[^(]*\\bAPPEND\\b"


async def code_tables_keyed_by_namespace(ch: Any) -> list[str]:
    """Every loom.code_* table that carries both org and namespace — read from
    the live schema so a table added tomorrow is purged without a code change.

    Views are left out: a mutation on a `TO` view is forwarded to its target. So is a
    table a refreshable view replaces whole: a mutation on it never finishes (the
    refresh exchanges the table under it), and its rows are derived from tables purged
    here, so `refresh_code_views` rebuilds it without them."""
    rows = await ch.query_all(
        f"""
        SELECT table, groupUniqArray(name) AS cols
        FROM system.columns
        WHERE database = currentDatabase() AND table LIKE 'code\\_%'
          AND table IN (
            SELECT name FROM system.tables
            WHERE database = currentDatabase() AND engine NOT LIKE '%View%'
          )
          AND table NOT IN (
            SELECT extract(create_table_query, '{_REFRESH_TARGET}')
            FROM system.tables
            WHERE database = currentDatabase() AND engine = 'MaterializedView'
              AND match(create_table_query, '{_REFRESH_TARGET}')
              AND NOT match(create_table_query, '{_REFRESH_APPEND}')
          )
        GROUP BY table
        ORDER BY table
        """
    )
    out: list[str] = []
    for r in rows:
        cols = set(r.get("cols") or [])
        full = f"loom.{r['table']}"
        if {"org", "namespace"} <= cols and full not in KEEP_TABLES and full != "loom.code_repos":
            out.append(full)
    return out


async def refresh_code_views(ch: Any) -> list[str]:
    """Start a refresh of every view that replaces a loom.code_* table whole, after a
    purge: the rebuilt table holds nothing of the purged namespaces. A refused refresh
    is logged, never raised — the view's own schedule removes the rows within its period."""
    rows = await ch.query_all(
        f"""
        SELECT name AS view
        FROM system.tables
        WHERE database = currentDatabase() AND engine = 'MaterializedView'
          AND startsWith(extract(create_table_query, '{_REFRESH_TARGET}'), 'code_')
          AND NOT match(create_table_query, '{_REFRESH_APPEND}')
        ORDER BY name
        """
    )
    started: list[str] = []
    for r in rows:
        view = str(r.get("view") or "")
        if not view:
            continue
        try:
            await ch.execute(f"SYSTEM REFRESH VIEW loom.`{view}`")
            started.append(view)
        except Exception as e:  # noqa: BLE001 — the scheduled refresh removes the rows anyway
            logger.warning("code purge: refresh of loom.%s refused (%s)", view, e)
    return started


async def deactivate_registry_row(ch: Any, org: str, namespace: str) -> bool:
    """code_repos row → active = false, every column mirrored (RMT)."""
    from server.code.store.index import CodeRepo

    return await CodeRepo(ch).deactivate_code_repo(org, namespace)


def mirror_still_used(git_dir: str, org: str, namespace: str, repos: list[Any]) -> bool:
    """True when another active entry (config or registry) shares the mirror."""
    return any(
        c.git_dir == git_dir and not (c.org == org and c.namespace == namespace) for c in repos
    )


def _under_self_service_root(git_dir: str) -> bool:
    from server.settings import settings

    root = os.path.abspath(settings.code_repos_root)
    return bool(git_dir) and os.path.abspath(git_dir).startswith(root + os.sep)


async def remove_mirror_if_orphaned(cfg: Any, repos: list[Any]) -> bool:
    """Delete the bare mirror when this was the last entry using it and it
    lives under the self-service root (an operator's mirror is theirs)."""
    import asyncio

    git_dir = cfg.git_dir
    if not await asyncio.to_thread(_under_self_service_root, git_dir):
        return False
    if mirror_still_used(git_dir, cfg.org, cfg.namespace, repos):
        return False
    if not await asyncio.to_thread(os.path.isdir, git_dir):
        return False
    await asyncio.to_thread(shutil.rmtree, git_dir, True)
    logger.info("retire %s: removed mirror %s", cfg.label, git_dir)
    return True
