"""The migration and query tools' directives in SQL files.

A repository's SQL is mostly not a test suite. It is migrations and named queries, and
the tools that run them leave a small grammar in comments and filenames that says what
each statement IS: goose's `-- +goose Up` / `Down`, dbmate's `-- migrate:up` / `down`,
Liquibase's formatted-SQL `--changeset author:id` and `--rollback`, Sqitch's `-- Deploy`
/ `Revert` / `Verify` headers, Flyway's `V1_2__desc.sql` / `U…` / `R__…` filenames,
Atlas's and dbmate's `20240101120000_desc.sql`, Prisma's
`migrations/<ts>_<name>/migration.sql`, sqlc's `-- name: GetAuthor :one`, and psql's
`\\i` / `\\ir` includes. ClickHouse's `-- Tags:` header is the same kind of thing
(`lang_sql` reads it as `Header`).

This module reads that grammar into the plane's existing shapes: a `tool`
facet and a `migration` (version) facet on the file, a `direction` facet
per statement (`up`, `down`, `rollback`, `deploy`, `revert`, `verify`,
`undo`, `repeatable`), `query` symbols for sqlc's named queries and
`changeset` symbols for Liquibase's changesets (each spanning its
statements), and the psql include targets as `includes` edges. The
splitter's part — kept-whole `StatementBegin … StatementEnd` blocks, the
client-side terminators — lives in `lang_sql_split.SplitMode`.

Outside the extractor stamp with `lang_sql.py` (see
`tests/unit/test_code_extractor_stamp.py`): the rows it adds ride the statement-record
path of SQL tests, applied by `loom code backfill --paths-file` over the SQL tree, never
by a heal.
"""

from __future__ import annotations

import bisect
import posixpath
import re
from dataclasses import dataclass, field

from server.code.domain.extract.lang_python import module_scope
from server.code.domain.extract.lang_sql_model import Facet, Statement
from server.code.domain.extract.treesitter import Symbol

FACET_TOOL = "tool"
FACET_MIGRATION = "migration"
FACET_DIRECTION = "direction"
KIND_QUERY = "query"
KIND_CHANGESET = "changeset"

TOOL_GOOSE = "goose"
TOOL_DBMATE = "dbmate"
TOOL_LIQUIBASE = "liquibase"
TOOL_FLYWAY = "flyway"
TOOL_ATLAS = "atlas"
TOOL_PRISMA = "prisma"
TOOL_SQLC = "sqlc"
TOOL_SQITCH = "sqitch"
TOOL_CLICKHOUSE_TEST = "clickhouse-test"

_GOOSE_RE = re.compile(r"^\s*--\s*\+goose\s+(Up|Down|NO TRANSACTION|ENVSUB ON|ENVSUB OFF)\b", re.I)
_DBMATE_RE = re.compile(r"^\s*--\s*migrate:(up|down)\b(.*)$", re.I)
_LIQUIBASE_HEAD_RE = re.compile(r"^\s*--\s*liquibase formatted sql", re.I)
_CHANGESET_RE = re.compile(
    r"^\s*--\s*changeset\s+(?P<author>[^:\s]+):(?P<id>\S+)(?P<attrs>.*)$", re.I
)
_ROLLBACK_RE = re.compile(r"^\s*--\s*rollback\b", re.I)
_SQLC_NAME_RE = re.compile(r"^\s*--\s*name:\s*(?P<name>[A-Za-z_]\w*)\s+(?P<cmd>:\w+)", re.I)
# `-- Deploy flipr:users to pg` / `-- Revert flipr:users from pg` / `-- Verify flipr:users on pg`;
# an older template writes `-- Deploy users` alone: accepted under a deploy/ revert/ verify/ dir
_SQITCH_RE = re.compile(
    r"^[^\S\n]*--\s*(Deploy|Revert|Verify)\s+[\w.-]+:[\w./-]+\s+(?:to|from|on)\s+\w+", re.I | re.M
)
_SQITCH_BARE_RE = re.compile(r"^[^\S\n]*--\s*(Deploy|Revert|Verify)\s+[\w.:/-]+\s*$", re.I | re.M)
_ATLAS_RE = re.compile(r"^\s*--\s*atlas:", re.I)
_FLYWAY_NAME_RE = re.compile(
    r"(^|/)(?P<kind>[VUR])(?P<version>[0-9][0-9_.]*)?__(?P<desc>[^/]+)\.sql$", re.I
)
_TIMESTAMP_NAME_RE = re.compile(r"(^|/)(?P<version>\d{8,14})_[^/]+\.sql$")
_NUMBER_NAME_RE = re.compile(r"(^|/)(?P<version>\d{1,8})_[^/]+\.sql$")
_PRISMA_PATH_RE = re.compile(r"(^|/)migrations/(?P<version>\d{14})_[^/]+/migration\.sql$")
_PSQL_INCLUDE_RE = re.compile(r"^[^\S\n]*\\(?:i|ir|include|include_relative)\s+(?P<path>\S+)", re.M)
_SQITCH_DIRS = {"deploy": "deploy", "revert": "revert", "verify": "verify"}


@dataclass
class DirectiveUnits:
    tool: str = ""
    version: str = ""
    symbols: list[Symbol] = field(default_factory=list)
    facets: list[Facet] = field(default_factory=list)
    includes: list[str] = field(default_factory=list)
    directions: dict[int, str] = field(default_factory=dict)  # statement ordinal -> direction

    def prefix_parts(self) -> list[str]:
        parts: list[str] = []
        if self.tool and self.tool != TOOL_CLICKHOUSE_TEST:  # the `-- Tags:` line already says it
            parts.append(self.tool + (" " + self.version if self.version else ""))
        return parts


def _lines_with_numbers(text: str) -> list[tuple[int, str]]:
    return list(enumerate(text.split("\n"), start=1))


def detect_tool(path: str, text: str) -> tuple[str, str]:
    """(tool, version) from the file's directives first, then its name."""
    head = text[:4000]
    if _LIQUIBASE_HEAD_RE.search(head) or _CHANGESET_RE.search(text):
        return TOOL_LIQUIBASE, _version_from_name(path)
    if re.search(r"(?m)^[^\S\n]*--\s*\+goose\b", text):
        return TOOL_GOOSE, _version_from_name(path)
    if re.search(r"(?m)^[^\S\n]*--\s*migrate:(up|down)\b", text):
        return TOOL_DBMATE, _version_from_name(path)
    if re.search(r"(?m)^[^\S\n]*--\s*name:\s*[A-Za-z_]\w*\s+:\w+", text):
        return TOOL_SQLC, ""
    if _SQITCH_RE.search(text) or (_sqitch_direction(path) and _SQITCH_BARE_RE.search(text)):
        return TOOL_SQITCH, ""
    if _ATLAS_RE.search(text) or "atlas:" in head:
        return TOOL_ATLAS, _version_from_name(path)
    pm = _PRISMA_PATH_RE.search(path)
    if pm:
        return TOOL_PRISMA, pm.group("version")
    fm = _FLYWAY_NAME_RE.search(path)
    if fm and ("migration" in path.lower() or fm.group("kind").upper() in "VU"):
        return TOOL_FLYWAY, fm.group("version") or ""
    if "-- Tags:" in head or "--Tags:" in head:
        return TOOL_CLICKHOUSE_TEST, ""
    return "", ""


def _version_from_name(path: str) -> str:
    fm = _FLYWAY_NAME_RE.search(path)
    if fm:
        return fm.group("version") or ""
    for rx in (_PRISMA_PATH_RE, _TIMESTAMP_NAME_RE, _NUMBER_NAME_RE):
        m = rx.search(path)
        if m:
            return m.group("version")
    return ""


def _flyway_direction(path: str) -> str:
    fm = _FLYWAY_NAME_RE.search(path)
    if not fm:
        return ""
    return {"V": "up", "U": "undo", "R": "repeatable"}[fm.group("kind").upper()]


def _sqitch_direction(path: str) -> str:
    parts = path.lower().split("/")
    for seg in reversed(parts[:-1]):
        if seg in _SQITCH_DIRS:
            return _SQITCH_DIRS[seg]
    return ""


def _directions_by_line(path: str, text: str, tool: str) -> tuple[dict[int, str], str]:
    """{line: direction that starts there} from the file's directives, and
    the file-level direction its name or path implies (Flyway's `V`/`U`/`R`,
    Sqitch's `deploy/` `revert/` `verify/`, Prisma's and Atlas's always-up)."""
    starts: dict[int, str] = {}
    for ln_no, line in _lines_with_numbers(text):
        gm = _GOOSE_RE.match(line)
        if gm and gm.group(1).lower() in ("up", "down"):
            starts[ln_no] = gm.group(1).lower()
            continue
        dm = _DBMATE_RE.match(line)
        if dm:
            starts[ln_no] = dm.group(1).lower()
            continue
        sm = _SQITCH_RE.match(line) or (
            _SQITCH_BARE_RE.match(line) if tool == TOOL_SQITCH else None
        )
        if sm:
            starts[ln_no] = {"deploy": "deploy", "revert": "revert", "verify": "verify"}[
                sm.group(1).lower()
            ]
    whole = ""
    if tool == TOOL_FLYWAY:
        whole = _flyway_direction(path)
    elif tool == TOOL_SQITCH:
        whole = _sqitch_direction(path)
    elif tool in (TOOL_PRISMA, TOOL_ATLAS):
        whole = "up"
    return starts, whole


def _statement_directions(
    statements: list[Statement], starts: dict[int, str], whole: str
) -> dict[int, str]:
    out: dict[int, str] = {}
    current = whole
    marks = sorted(starts.items())
    k = 0
    for st in statements:
        while k < len(marks) and marks[k][0] <= st.end_line:
            current = marks[k][1]
            k += 1
        if current:
            out[st.ordinal] = current
    return out


def _named_query_symbols(
    path: str, mod: str, text: str, statements: list[Statement]
) -> list[Symbol]:
    """sqlc: `-- name: GetAuthor :one` names the statement that follows."""
    out: list[Symbol] = []
    with_body = sorted((s for s in statements if s.body), key=lambda s: s.start_line)
    ends = [s.end_line for s in with_body]  # rising: statements do not overlap
    for ln_no, line in _lines_with_numbers(text):
        m = _SQLC_NAME_RE.match(line)
        if not m:
            continue
        k = bisect.bisect_left(ends, ln_no)
        if k == len(with_body):
            continue
        st = with_body[k]
        name = m.group("name")
        out.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind=KIND_QUERY,
                signature=f"{name} {m.group('cmd')} · {st.kind}",
                path=path,
                start_line=min(ln_no, st.start_line),
                end_line=st.end_line,
                doc_comment=st.comments,
            )
        )
    return out


def _changeset_symbols(path: str, mod: str, text: str, statements: list[Statement]) -> list[Symbol]:
    """Liquibase formatted SQL: `--changeset author:id` opens a changeset
    that runs to the next one."""
    numbered = _lines_with_numbers(text)
    heads = [(ln_no, m) for ln_no, line in numbered if (m := _CHANGESET_RE.match(line))]
    out: list[Symbol] = []
    total_lines = len(numbered)
    by_start = sorted(statements, key=lambda s: s.start_line)
    starts = [s.start_line for s in by_start]
    for k, (ln_no, m) in enumerate(heads):
        end = heads[k + 1][0] - 1 if k + 1 < len(heads) else total_lines
        # the last statement that starts by `end` ends last, as statements do not overlap
        j = bisect.bisect_right(starts, end) - 1
        reach = by_start[j].end_line if j >= 0 and by_start[j].end_line >= ln_no else ln_no
        last = min(reach, end)
        while last > ln_no and not numbered[last - 1][1].strip():
            last -= 1  # the blank lines before the next changeset are nobody's
        name = f"{m.group('author')}:{m.group('id')}"
        attrs = m.group("attrs").strip()
        out.append(
            Symbol(
                qualified_name=f"{mod}::{name}",
                name=name,
                kind=KIND_CHANGESET,
                signature=f"changeset {name}" + (f" {attrs}" if attrs else ""),
                path=path,
                start_line=ln_no,
                end_line=max(ln_no, last),
            )
        )
    return out


def _rollback_lines(text: str) -> list[int]:
    return [ln_no for ln_no, line in _lines_with_numbers(text) if _ROLLBACK_RE.match(line)]


def psql_includes(path: str, text: str) -> list[str]:
    """`\\i file` / `\\ir file` targets as repository paths, resolved against
    the file's directory (`\\ir` is defined that way; `\\i` is relative to
    psql's working directory, which the tree does not carry — the file's
    own directory is the usual one)."""
    out: list[str] = []
    base = posixpath.dirname(path)
    for m in _PSQL_INCLUDE_RE.finditer(text):
        target = m.group("path").strip("'\"")
        if not target or target.startswith("/") or "$" in target or ":" in target:
            continue
        resolved = posixpath.normpath(posixpath.join(base, target))
        if not resolved.startswith("../"):
            out.append(resolved)
    return list(dict.fromkeys(out))


def directive_units(path: str, text: str, statements: list[Statement]) -> DirectiveUnits:
    """Everything the directives say about this file and its statements."""
    tool, version = detect_tool(path, text)
    units = DirectiveUnits(tool=tool, version=version)
    mod = module_scope(path)
    if tool and tool != TOOL_CLICKHOUSE_TEST:
        units.facets.append(Facet(FACET_TOOL, tool, 0, 1, 1.0))
        if version:
            units.facets.append(Facet(FACET_MIGRATION, version, 0, 1, 1.0))
    starts, whole = _directions_by_line(path, text, tool)
    units.directions = _statement_directions(statements, starts, whole)
    for st in statements:
        d = units.directions.get(st.ordinal)
        if d:
            units.facets.append(Facet(FACET_DIRECTION, d, st.ordinal, st.start_line, 1.0))
    if tool == TOOL_LIQUIBASE:
        units.symbols.extend(_changeset_symbols(path, mod, text, statements))
        by_start = sorted(statements, key=lambda s: s.start_line)
        ends = [s.end_line for s in by_start]
        for ln_no in _rollback_lines(text):
            # `--rollback` follows the statement it undoes; the splitter reads
            # it as the NEXT statement's leading comment, so look back first
            k = bisect.bisect_left(ends, ln_no)
            owner: Statement | None = by_start[k - 1] if k else None
            if owner is None and by_start and by_start[0].start_line <= ln_no:
                owner = by_start[0]
            if owner is not None:
                units.facets.append(Facet(FACET_DIRECTION, "rollback", owner.ordinal, ln_no, 1.0))
    if tool == TOOL_SQLC:
        units.symbols.extend(_named_query_symbols(path, mod, text, statements))
    units.includes = psql_includes(path, text)
    return units


__all__ = [
    "FACET_DIRECTION",
    "FACET_MIGRATION",
    "FACET_TOOL",
    "KIND_CHANGESET",
    "KIND_QUERY",
    "TOOL_ATLAS",
    "TOOL_CLICKHOUSE_TEST",
    "TOOL_DBMATE",
    "TOOL_FLYWAY",
    "TOOL_GOOSE",
    "TOOL_LIQUIBASE",
    "TOOL_PRISMA",
    "TOOL_SQITCH",
    "TOOL_SQLC",
    "DirectiveUnits",
    "detect_tool",
    "directive_units",
    "psql_includes",
]
