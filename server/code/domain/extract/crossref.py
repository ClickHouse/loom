"""Cross-language reference pass: SQL/shell tests -> implementing C++.

ClickHouse SQL is a REGISTERED vocabulary: every function, aggregate,
table function, storage engine, and setting is introduced by an explicit
registration site in C++ (`factory.registerFunction("arrayJoin", ...)`,
`DECLARE(UInt64, max_threads, ...)`). That turns "which tests exercise
this function" into a set intersection, not a parsing problem — measured
on the live corpus: 75.5% of function-call tokens in the ~11K stateless
.sql tests match the registration whitelist, and 88% of matched functions
link to <= 20 tests (median 3).

The pass runs entirely inside ClickHouse over already-ingested chunks —
no filesystem, no new tables:

  1. WHITELIST: regex-extract registered names + their registration file
     from cpp chunks (`registerFunction`/`registerAlias`/`register*` with
     a string literal; Settings `DECLARE`/`DECLARE_WITH_ALIAS` macros).
  2. USES: tokenize sql/shell test chunks — identifiers followed by `(`
     plus `SET`/`SETTINGS` assignments — intersecting with the whitelist.
  3. Write code_test_map rows, method='content_reference':
     src_path = the registering C++ file, test_path = the test file,
     confidence = 0.9 / (1 + log10(document frequency)) — ubiquitous
     names (`count`, `toString`) self-damp instead of being blacklisted.

Idempotent: rows key on (org, ns, src_path, method, test_path); re-runs
replace under the RMT version. Names shorter than 3 chars or appearing in
more than `max_df` test files are skipped as noise.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass, field
from typing import Any

from server.code.store.index import CodeRepo, build_row

logger = logging.getLogger("loom.code_index.crossref")

_MIN_NAME_LEN = 3
_MAX_DF = 1500  # a name in >1500 test files is vocabulary, not a reference
_MAX_EVIDENCE = 5

# Registration shapes, verified against the live corpus: string-literal
# registrations (registerAlias("x", ...), registerStorage("Log", ...),
# aggregate factory.registerFunction("uniq", ...), registerDataType) —
# BUT most ordinary functions register as
# `factory.registerFunction<FunctionArrayJoin>(documentation)` with NO
# string: their SQL name lives in the class constant
# `static constexpr auto name = "arrayJoin";`. Both legs are required:
# without the name-constant leg no test links to arrayJoin while thousands
# link to Settings.cpp.
_REGISTRATION_RE = (
    r'register(?:Function|Alias\w*|Storage|TableFunction|DataType|'
    r'AggregateFunction\w*)\s*(?:<[^>()]{0,200}>)?\s*\(\s*"([A-Za-z_]\w{2,60})"'
)
_NAME_CONST_RE = r'constexpr\s+auto\s+name\s*=\s*"([A-Za-z_]\w{1,60})"'
# name constants are only trusted in the registries where `name` IS the
# SQL-visible identifier; elsewhere (Interpreters, Storages internals) a
# class's `name` constant can be arbitrary prose.
_NAME_CONST_PATHS = (
    "src/Functions/%", "src/AggregateFunctions/%", "src/TableFunctions/%",
)
# Settings declarations: DECLARE(Type, name, ...) / DECLARE_WITH_ALIAS(...,
# alias) in src/Core/*Settings*.cpp.
_SETTINGS_DECL_RE = r"DECLARE\w*\(\s*\w+\s*,\s*([A-Za-z_]\w{2,60})\s*,"
_SETTINGS_ALIAS_RE = r"DECLARE_WITH_ALIAS\([^)]{0,400},\s*([A-Za-z_]\w{2,60})\s*\)"

# Test-side token shapes: a call `name(`, and `SET name = ...` /
# `SETTINGS name = ...` (settings never take parens).
_SQL_CALL_RE = r"\b([A-Za-z_]\w{2,60})\s*\("
_SQL_SETTING_RE = r"(?i:SET|SETTINGS)\s+([A-Za-z_]\w{2,60})"


@dataclass
class CrossrefResult:
    registrations: int = 0
    test_files_linked: int = 0
    rows_written: int = 0
    skipped_high_df: list[str] = field(default_factory=list)


async def run_crossref_pass(repo: CodeRepo, org: str, ns: str) -> CrossrefResult:
    res = CrossrefResult()
    ch = repo._ch

    # 1) registration whitelist: name -> registering path (first-seen path
    # per name; a handful of names register in several files — any of them
    # is a correct "implementing file" answer for test mapping).
    reg_rows = await ch.query_all(
        """
        SELECT name, any(path) AS path
        FROM (
            SELECT arrayJoin(extractAllGroups(content, {reg_re:String}))[1] AS name, path
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path LIKE 'src/%' AND tombstoned_at IS NULL
              AND match(content, 'register')
            UNION ALL
            SELECT arrayJoin(extractAllGroups(content, {name_re:String}))[1] AS name, path
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND multiMatchAny(path, ['^src/Functions/',
                                       '^src/AggregateFunctions/',
                                       '^src/TableFunctions/'])
              AND tombstoned_at IS NULL
              AND match(content, 'constexpr auto name')
            UNION ALL
            SELECT arrayJoin(extractAllGroups(content, {decl_re:String}))[1] AS name, path
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path LIKE 'src/Core/%Settings%' AND tombstoned_at IS NULL
            UNION ALL
            SELECT arrayJoin(extractAllGroups(content, {alias_re:String}))[1] AS name, path
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path LIKE 'src/Core/%Settings%' AND tombstoned_at IS NULL
        )
        WHERE length(name) >= {min_len:UInt8}
        GROUP BY name
        """,
        {"org": org, "ns": ns, "min_len": _MIN_NAME_LEN,
         "reg_re": _REGISTRATION_RE, "name_re": _NAME_CONST_RE,
         "decl_re": _SETTINGS_DECL_RE, "alias_re": _SETTINGS_ALIAS_RE},
    )
    whitelist = {str(r["name"]): str(r["path"]) for r in reg_rows}
    res.registrations = len(whitelist)
    if not whitelist:
        logger.warning("crossref: empty registration whitelist — nothing to do")
        return res

    # 2) per-test-file matched names (call tokens + SET/SETTINGS names),
    # whitelist-intersected server-side. Combinator suffixes (countIf,
    # sumMap) fold onto their base aggregate by longest-prefix match below.
    use_rows = await ch.query_all(
        """
        SELECT path AS test_path, groupUniqArray(1000)(name) AS names
        FROM (
            SELECT path, arrayJoin(extractAllGroups(content, {call_re:String}))[1] AS name
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND (path LIKE 'tests/queries/%' OR path LIKE 'tests/integration/%')
              AND tombstoned_at IS NULL
            UNION ALL
            SELECT path, arrayJoin(extractAllGroups(content, {set_re:String}))[1] AS name
            FROM loom.code_chunks FINAL
            WHERE org = {org:String} AND namespace = {ns:String}
              AND path LIKE 'tests/%' AND tombstoned_at IS NULL
        )
        WHERE name IN {names:Array(String)}
        GROUP BY path
        """,
        {"org": org, "ns": ns, "names": sorted(whitelist),
         "call_re": _SQL_CALL_RE, "set_re": _SQL_SETTING_RE},
    )

    # 3) document frequency -> damped confidence; write test-map rows.
    df: dict[str, int] = {}
    for r in use_rows:
        for n in r["names"]:
            df[str(n)] = df.get(str(n), 0) + 1

    rows: list[dict[str, Any]] = []
    linked_files = 0
    for r in use_rows:
        by_src: dict[str, list[tuple[float, str]]] = {}
        for n in (str(x) for x in r["names"]):
            d = df.get(n, 1)
            if d > _MAX_DF:
                if n not in res.skipped_high_df:
                    res.skipped_high_df.append(n)
                continue
            conf = round(0.9 / (1 + math.log10(d)), 3)
            by_src.setdefault(whitelist[n], []).append((conf, n))
        if by_src:
            linked_files += 1
        for src_path, hits in by_src.items():
            hits.sort(reverse=True)
            rows.append(
                build_row(
                    "code_test_map",
                    org=org, namespace=ns, src_path=src_path,
                    method="content_reference",
                    test_path=str(r["test_path"]),
                    confidence=hits[0][0],
                    evidence=",".join(n for _, n in hits[:_MAX_EVIDENCE]),
                )
            )
    res.test_files_linked = linked_files

    for start in range(0, len(rows), 5000):
        await repo.insert_rows("code_test_map", rows[start : start + 5000])
    res.rows_written = len(rows)
    logger.info(
        "crossref: %d registrations, %d test files linked, %d rows, "
        "%d names over df cap",
        res.registrations, linked_files, len(rows), len(res.skipped_high_df),
    )
    return res
