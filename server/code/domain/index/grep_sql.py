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


def _with_source_loc(inner: str, prefix_clause: str, path_sql: str) -> str:
    """Attach the indexed LF count plus one, from the same file revision."""
    return f"""(
        SELECT chunks.*, files.loc AS source_loc
        FROM (SELECT * FROM {inner}) AS chunks
        LEFT ANY JOIN (
            SELECT path, indexed_commit, loc
            FROM loom.code_files FINAL
            WHERE org = {{org:String}} AND namespace = {{ns:String}}
              AND tombstoned_at IS NULL AND lang != 'reference'
              AND NOT endsWith(lower(path), '.ipynb')
              {prefix_clause} {path_sql}
        ) AS files ON chunks.path = files.path AND chunks.indexed_commit = files.indexed_commit
    )"""


def grep_chunks_sql(
    cond: str,
    prefix_clause: str,
    path_sql: str,
    extra_settings: str = "",
    *,
    with_endings: bool = False,
    endings_only: bool = False,
) -> str:
    """mode=matches: whole chunk rows in (path, start_line) order, chunk_id
    breaking ties so the page edge is deterministic."""
    inner = newest_live(
        "path, scope, symbol_ids, kind, start_line, end_line, content, chunk_id, embedding_model, "
        "indexed_commit",
        _where(cond, prefix_clause, path_sql),
    )
    loc_col = ""
    if with_endings:
        inner = _with_source_loc(inner, prefix_clause, path_sql)
        loc_col = ", source_loc"
    valid_positions = "WHERE start_line < ifNull(source_loc, 0)" if endings_only else ""
    return f"""
            SELECT path, scope, symbol_ids, kind, start_line, end_line, content,
                   embedding_model, toString(indexed_commit) AS indexed_commit{loc_col}
            FROM {inner}
            {valid_positions}
            ORDER BY path, start_line, chunk_id
            LIMIT {{limit:UInt32}}
            {GREP_SCAN_SETTINGS}{extra_settings}
            """


def grep_pattern_chunks_sql(
    cond: str,
    prefix_clause: str,
    path_sql: str,
    patterns: str,
    position: str,
    extra_settings: str,
    *,
    with_endings: bool = False,
) -> str:
    """First live chunk with a physical match per uncovered literal."""
    inner = newest_live(
        "path, scope, symbol_ids, kind, start_line, end_line, content, chunk_id, embedding_model, "
        "indexed_commit",
        _where(cond, prefix_clause, path_sql),
    )
    loc = "0"
    if with_endings:
        inner = _with_source_loc(inner, prefix_clause, path_sql)
        loc = "source_loc"
    raw_lines = (
        """
        , arrayMap(
            (line, i) -> concat(line, if(i < span OR trailing_lf, char(10), '')),
            physical_parts, arrayEnumerate(physical_parts)
        ) AS physical_lines_with_endings
    """
        if with_endings
        else ""
    )
    match_lines = (
        "if(endsWith(literals[i], char(10)), "
        "arraySlice(physical_lines_with_endings, 1, "
        "greatest(0, toInt64(ifNull(source_loc, 0)) - toInt64(start_line))), physical_lines)"
        if with_endings
        else "physical_lines"
    )
    kind_guard = "WHERE" if with_endings else "AND"
    return f"""
            WITH {patterns} AS literals,
                 concat('[', char(9), '-', char(13), char(28), '-', char(32),
                        char(194, 133), char(92), 'p{{Z}}]') AS source_space
            SELECT hit.1 AS path, hit.2 AS scope, hit.3 AS symbol_ids, hit.4 AS kind,
                   hit.5 AS start_line, hit.6 AS end_line, hit.7 AS content,
                   hit.8 AS embedding_model, hit.9 AS indexed_commit, hit.10 AS source_loc
            FROM (
                SELECT hit, min(pattern_index) AS first_pattern
                FROM (
                    SELECT pattern_index, argMin(
                        tuple(path, scope, symbol_ids, kind, start_line, end_line, content,
                              embedding_model, toString(indexed_commit), {loc}),
                        tuple(path, start_line, chunk_id, embedding_model)
                    ) AS hit
                    FROM (
                        SELECT *,
                               splitByChar(char(10), content) AS source_parts,
                               toInt64(end_line) - toInt64(start_line) + 1 AS span,
                               toInt64(endsWith(content, char(10))) AS trailing_lf,
                               arraySlice(source_parts, -span - trailing_lf, greatest(0, span))
                                   AS physical_parts,
                               arrayMap(
                                   line -> left(line, length(line) - endsWith(line, char(13))),
                                   physical_parts
                               ) AS physical_lines {raw_lines}
                        FROM {inner}
                        {kind_guard} kind != 'class_skeleton'
                        AND span > 0 AND length(source_parts) >= span + trailing_lf
                        AND NOT arrayExists(
                            suffix -> endsWith(content, suffix),
                            {{unmappable_endings:Array(String)}}
                        )
                        AND NOT arrayExists(line ->
                            startsWith(replaceRegexpOne(line, concat('^', source_space, '+'), ''),
                                       {{continued_line_prefix:String}})
                            OR endsWith(replaceRegexpOne(line, concat(source_space, '+$'), ''),
                                        {{continued_line_suffix:String}}),
                            source_parts
                        )
                    )
                    ARRAY JOIN arrayFilter(
                        i -> arrayExists(line -> {position}(line, literals[i]) > 0, {match_lines}),
                        arrayEnumerate(literals)
                    ) AS pattern_index
                    GROUP BY pattern_index
                )
                GROUP BY hit
            )
            ORDER BY first_pattern
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
        "path, scope, symbol_ids, kind, start_line, end_line, content, chunk_id, embedding_model,\n"
        "                       arrayCount(\n"
        "                           x -> x > 0,\n"
        "                           multiSearchAllPositionsCaseInsensitive(\n"
        "                               content, {needles:Array(String)})\n"
        "                       ) AS shingles",
        _where(cond, prefix_clause, path_sql),
    )
    return f"""
            SELECT path, scope, symbol_ids, kind, start_line, end_line, content,
                   embedding_model, shingles,
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
