"""code.grep's chunk statements without FINAL and without the n-gram index.

The three statements code.grep runs per call (`grep_chunks` for matches, `grep_files`
for files and presence, `grep_message_chunks` for message) read `loom.code_chunks FINAL`
behind the `text(ngrams(3))` index. Common trigrams have posting lists close to the size
of the `content` column itself, so a 30-character literal reads ~28 of them, most cold
from object storage: 0.2-3 s of `FilteringMarksWithSecondaryKeysMicroseconds` to prune a
namespace whose `content` scan costs 50-400 ms from the filesystem cache.

These builders scan instead: the exact matcher (no `lower(content) LIKE` guard,
`ignore_data_skipping_indices` so a regex's `match()` does not consult the index either; an
alternation's branch needles go in front of `match()`, see `grep_plan.content_predicate`), and each
key's newest version picked by `ORDER BY version DESC LIMIT 1 BY <sorting key tail>` in place of
FINAL, then tombstoned keys dropped. That equals FINAL-then-filter because every version of a
`code_chunks` key carries the same `content` (`VERSION_INVARIANT_COLS`): the content predicate
admits all versions of a key or none, so the newest admitted version is the newest version. The pick
is by `(embedding_model, path, chunk_id)` — the sorting key after the `(org, namespace)` the WHERE
fixes — so a chunk under two labels stays two rows, as under FINAL. A FINAL-free statement also
rides the query condition cache, which FINAL reads do not.

Every builder takes the already-rendered WHERE fragments of the caller:
`cond` (a content predicate without leading AND), `prefix_clause` and
`path_sql` (each "" or starting with AND), and optional `extra_settings`
(", name = value" items, e.g. a request deadline's break-on-timeout bound).
"""

from __future__ import annotations

# The one SETTINGS clause of the scan statements: the content index is not
# consulted (the WHERE carries no guard; `match()` would otherwise read it).
GREP_SCAN_SETTINGS = "SETTINGS ignore_data_skipping_indices = 'idx_code_chunks_content_ng'"

# The sorting key after (org, namespace): FINAL collapses rows by it.
NEWEST_BY = "embedding_model, path, chunk_id"


def newest_live(columns: str, where: str) -> str:
    """A derived table of `columns` for each matching key's newest version,
    tombstones dropped. `where` is the full predicate after WHERE."""
    return f"""(
                SELECT {columns}, tombstoned_at
                FROM loom.code_chunks
                WHERE {where}
                ORDER BY version DESC
                LIMIT 1 BY {NEWEST_BY}
            )
            WHERE tombstoned_at IS NULL"""


def _where(cond: str, prefix_clause: str, path_sql: str) -> str:
    return (
        "org = {org:String} AND namespace = {ns:String}\n"
        f"                  AND {cond} {prefix_clause} {path_sql}"
    )


def grep_chunks_sql(cond: str, prefix_clause: str, path_sql: str, extra_settings: str = "") -> str:
    """mode=matches: whole chunk rows in (path, start_line) order, chunk_id
    breaking ties so the page edge is deterministic."""
    inner = newest_live(
        "path, scope, symbol_ids, start_line, end_line, content, chunk_id",
        _where(cond, prefix_clause, path_sql),
    )
    return f"""
            SELECT path, scope, symbol_ids, start_line, end_line, content
            FROM {inner}
            ORDER BY path, start_line, chunk_id
            LIMIT {{limit:UInt32}}
            {GREP_SCAN_SETTINGS}{extra_settings}
            """


def grep_files_sql(cond: str, prefix_clause: str, path_sql: str, extra_settings: str = "") -> str:
    """mode=files: one row per matching path with its chunk count and first
    line, plus `total` — the distinct matching paths before LIMIT (a window
    over the grouped rows, not a second statement)."""
    inner = newest_live("path, start_line", _where(cond, prefix_clause, path_sql))
    return f"""
            SELECT path, count() AS chunks, min(start_line) AS first_line,
                   count() OVER () AS total
            FROM {inner}
            GROUP BY path
            ORDER BY path
            LIMIT {{limit:UInt32}}
            {GREP_SCAN_SETTINGS}{extra_settings}
            """


def grep_message_sql(prefix_clause: str, path_sql: str, extra_settings: str = "") -> str:
    """mode=message: chunks carrying a raising/logging keyword AND any needle,
    most shingles first, with the distinct admitted files as a window."""
    cond = (
        "multiSearchAnyCaseInsensitive(content, {needles:Array(String)})\n"
        "                  AND multiSearchAny(content, {keywords:Array(String)})"
    )
    inner = newest_live(
        "path, scope, symbol_ids, start_line, end_line, content, chunk_id,\n"
        "                       arrayCount(\n"
        "                           x -> x > 0,\n"
        "                           multiSearchAllPositionsCaseInsensitive(\n"
        "                               content, {needles:Array(String)})\n"
        "                       ) AS shingles",
        _where(cond, prefix_clause, path_sql),
    )
    return f"""
            SELECT path, scope, symbol_ids, start_line, end_line, content, shingles,
                   uniqExact(path) OVER () AS admitted_files
            FROM {inner}
            ORDER BY shingles DESC, path, start_line, chunk_id
            LIMIT {{limit:UInt32}}
            {GREP_SCAN_SETTINGS}{extra_settings}
            """


__all__ = [
    "GREP_SCAN_SETTINGS",
    "NEWEST_BY",
    "grep_chunks_sql",
    "grep_files_sql",
    "grep_message_sql",
    "newest_live",
]
