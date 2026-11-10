"""The tracker record contract, and whether a source meets it.

Every `tracker.*` statement is written against a fixed set of tables and columns
(`docs/features/tracker/record-contract.md`). A team onboards by exposing views that match
it in its own ClickHouse; loom does not learn a team's schema. Before a
source serves, its schema is read once per process per TTL through the
source's own credential and compared with the contract here:

- `REQUIRED` tables and columns: missing = the source does not serve
  (`ContractFailed` → `tracker_unavailable`, with the names of what is missing);
- `OPTIONAL` tables: absent = the ops that read them say `not_provided`;
- `FEATURES`: named optional column groups (the vector leg's `embedding`,
  the bisect scan's `diff`) reported present or absent;
- `ALIASES`: the accepted forms of a contract column when
  the source lacks the column itself — an expression over columns the
  source does have, verified like a column and reported apart. The
  statements keep their names; `ContractReport.rewrite` projects the
  aliases onto the table reference once, in `TrackerSource._rows`.

Types are compared by family — `LowCardinality(String)`, `Nullable(String)`
and `String` are one family; `DateTime64(3)` is a `DateTime`; `Array(x)` is
compared on `x` — because a view over another store legitimately differs in
codec and nullability, never in shape.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from typing import Any

# --- the contract ------------------------------------------------------------

_ITEM_BASE: dict[str, str] = {
    "id": "UUID",
    "source_id": "String",
    "number": "UInt",
    "url": "String",
    "title": "String",
    "body": "String",
    "author": "String",
    "primary_component": "String",
    "severity_level": "String",
    "is_private": "UInt",
    "created_at": "DateTime",
    "updated_at": "DateTime",
    "closed_at": "DateTime",
}

REQUIRED: dict[str, dict[str, str]] = {
    "github_issues": {
        **_ITEM_BASE,
        "labels": "Array(String)",
        "assignees": "Array(String)",
        "state_reason": "String",
        "affected_versions": "Array(String)",
        "fix_versions": "Array(String)",
        "is_flaky_test": "UInt",
        "is_crash_ci": "UInt",
        "is_performance": "UInt",
        "error_code": "UInt",
        # the severity board counts open issues with no linked test
        "has_test": "UInt",
        # the triage columns the item read selects unconditionally; a source
        # without a triage bot defaults them in its view ('' / [])
        "triage_bisect_outcome": "String",
        "triage_reproduces_on": "Array(String)",
        "triage_clean_on": "Array(String)",
        "triage_introduced_commit": "String",
        "triage_component": "String",
        "triage_confidence": "String",
    },
    "github_prs": {
        **_ITEM_BASE,
        "pr_number": "UInt",
        "state": "String",
        "is_merged": "UInt",
        "has_test": "UInt",
        "test_types": "Array(String)",
        "reviewers": "Array(String)",
        "review_requests_test": "UInt",
        "ci_result": "String",
        "changelog_category": "String",
        "is_backport_pr": "UInt",
        "is_cherrypick_pr": "UInt",
        "is_sync_pr": "UInt",
        "upstream_pr_number": "UInt",
        "minor_version": "String",
        "base_branch": "String",
        "additions": "UInt",
        "deletions": "UInt",
        "time_to_merge_days": "UInt",
        "src_components": "Array(String)",
    },
    "edges": {
        "from_id": "UUID",
        "from_type": "String",
        "to_id": "UUID",
        "to_type": "String",
        "rel_type": "String",
        "created_at": "DateTime",
    },
}

# tables an op reads only when the source provides them; absent = `not_provided`
OPTIONAL: dict[str, dict[str, str]] = {
    "core_incidents": {
        "id": "UUID",
        "source_id": "String",
        "title": "String",
        "body": "String",
        "primary_component": "String",
        "severity_level": "String",
        "stack_functions": "Array(String)",
        "error_code": "UInt",
        "created_at": "DateTime",
    },
    "support_cases": {"id": "UUID", "source_id": "String", "title": "String"},
    "slack_threads": {"source_id": "String", "title": "String", "body": "String"},
    "component_risk_snapshot": {"computed_at": "DateTime", "component": "String"},
    "release_items": {"release_version": "String", "issue_number": "UInt"},
    "release_health_snapshot": {"computed_at": "DateTime", "release_version": "String"},
    "version_history": {"commit_sha": "String", "version": "String"},
    "version_history_private": {"commit_sha": "String", "version": "String"},
    "checks": {"check_name": "String", "test_name": "String"},
    "cloud_instances": {"instance_id": "UInt"},
    "rel_instance_mentions": {"from_id": "UUID", "instance_id": "UInt"},
    "slack_incidents": {"incident_id": "String"},
}

# optional column groups on required tables, named for the op that needs them
FEATURES: dict[str, dict[str, dict[str, str]]] = {
    "vector": {
        "github_issues": {"embedding": "Array(Float)"},
        "github_prs": {"embedding": "Array(Float)"},
    },
    "bisect": {"github_prs": {"diff": "String", "affected_file_paths": "Array(String)"}},
    "merge_time": {"github_prs": {"merged_at": "DateTime"}},
}


# --- aliases -----------------------------------------------


@dataclass(frozen=True)
class Alias:
    """One accepted form of a contract column when the source lacks the column
    itself: `expr` yields the column over the live table's columns named in
    `needs` (live column → family), verified like any column. An alias with
    an empty `needs` is a *default*: the source does not provide the column,
    every row reads `expr`, and the report says so apart — nobody mistakes a
    default for data."""

    expr: str
    needs: dict[str, str] = field(default_factory=dict)
    note: str = ""


# `metadata` is the source's JSON column: a path it lacks reads NULL, never
# an error, so an alias over it is safe once the column's family is JSON.
_HAS_TEST_FROM_JSON = Alias(
    "toUInt8(metadata.has_test.:Bool)",
    {"metadata": "JSON"},
    "the source keeps `has_test` (NULL = unknown) under metadata",
)
# A tracker without a triage bot serves empty triage columns.
_NO_TRIAGE = Alias("''", note="no triage columns")
_NO_TRIAGE_LIST = Alias("CAST([] AS Array(String))", note="no triage columns")
ALIASES: dict[str, dict[str, tuple[Alias, ...]]] = {
    "github_issues": {
        "triage_bisect_outcome": (_NO_TRIAGE,),
        "triage_reproduces_on": (_NO_TRIAGE_LIST,),
        "triage_clean_on": (_NO_TRIAGE_LIST,),
        "triage_introduced_commit": (_NO_TRIAGE,),
        "triage_component": (_NO_TRIAGE,),
        "triage_confidence": (_NO_TRIAGE,),
        "severity_level": (
            Alias("severity", {"severity": "String"}, "the source names it `severity`"),
        ),
        "has_test": (_HAS_TEST_FROM_JSON, Alias("CAST(NULL AS Nullable(UInt8))", note="unknown")),
    },
    "github_prs": {
        "has_test": (
            _HAS_TEST_FROM_JSON,
            Alias(
                "toUInt8(tests_added > 0)",
                {"tests_added": "UInt"},
                "tests added is a count; > 0 approximates a touched tests/ path",
            ),
        ),
        "upstream_pr_number": (
            Alias(
                "ifNull(toUInt32(metadata.original_pr_number.:Int64), 0)",
                {"metadata": "JSON"},
                "the original PR a backport repeats (94% of titles agree)",
            ),
            Alias("0", note="the backport read keeps its `Backport #N to` title match"),
        ),
        "review_requests_test": (
            Alias("CAST(NULL AS Nullable(UInt8))", note="not served upstream; unknown"),
        ),
        "severity_level": (Alias("''", note="pull requests carry no severity upstream"),),
        "test_types": (Alias("CAST([] AS Array(String))", note="no test classification"),),
        "src_components": (Alias("CAST([] AS Array(String))", note="no src/ component paths"),),
        # as a mapped tracker derives it (`mapping.py`): a merged PR's close is its merge
        "merged_at": (
            Alias(
                "if(is_merged = 1, closed_at, toDateTime64(0, 3))",
                {"is_merged": "UInt", "closed_at": "DateTime"},
                "a merged PR's closed_at",
            ),
        ),
    },
    "core_incidents": {
        "severity_level": (Alias("''", note="incidents carry no severity upstream"),),
    },
    # the cross-source bug view the answer route reads (not a contract table)
    "qa_bugs": {
        "severity_level": (
            Alias("severity", {"severity": "String"}, "the source names it `severity`"),
        ),
        "has_test": (_HAS_TEST_FROM_JSON, Alias("CAST(NULL AS Nullable(UInt8))")),
    },
}

_WRAPPERS = ("LowCardinality(", "Nullable(")


def _unwrap(actual: str) -> str:
    t = (actual or "").strip()
    changed = True
    while changed:
        changed = False
        for w in _WRAPPERS:
            if t.startswith(w) and t.endswith(")"):
                t = t[len(w) : -1].strip()
                changed = True
    return t


def family_ok(expected: str, actual: str) -> bool:
    """Whether a live column type satisfies a contract family."""
    a = _unwrap(actual)
    e = expected.strip()
    if e.startswith("Array(") and e.endswith(")"):
        return a.startswith("Array(") and a.endswith(")") and family_ok(e[6:-1], a[6:-1])
    if e == "String":
        return a == "String" or a.startswith("FixedString(") or a.startswith("Enum")
    if e == "UInt":
        return a.startswith("UInt") or a.startswith("Int") or a == "Bool"
    if e == "Int":
        return a.startswith("Int") or a.startswith("UInt")
    if e == "Float":
        return a.startswith("Float") or a.startswith("BFloat")
    if e == "DateTime":
        return a.startswith("DateTime") or a.startswith("Date")
    if e == "UUID":
        return a == "UUID"
    if e == "JSON":
        return a == "JSON" or a.startswith("JSON(") or a.startswith("Object(")
    return a == e


# --- the report -----------------------------------------------------------------


@dataclass
class ContractReport:
    source: str
    database: str
    ok: bool = True
    missing_tables: list[str] = field(default_factory=list)
    missing_columns: dict[str, list[str]] = field(default_factory=dict)
    type_mismatches: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    optional_present: list[str] = field(default_factory=list)
    optional_incomplete: dict[str, list[str]] = field(default_factory=dict)
    features: dict[str, bool] = field(default_factory=dict)
    # Contract columns the source expresses another way
    # (table → column → expression), and those it does not provide at all
    aliased: dict[str, dict[str, str]] = field(default_factory=dict)
    defaulted: dict[str, dict[str, str]] = field(default_factory=dict)
    # the live columns of every table with a projection, named one by one:
    # `SELECT *` omits ALIAS and MATERIALIZED columns (`is_reverted`,
    # `merged_at`, `number` upstream) unless a setting says otherwise
    columns: dict[str, list[str]] = field(default_factory=dict)
    checked_at: float = 0.0
    # the live schema the report was diffed against (never on the wire; the
    # CREATE VIEW drafts are written from it)
    described: dict[str, dict[str, str]] = field(default_factory=dict, repr=False)
    # A mapped tracker — logical table → (projection, the same
    # read with FINAL); logical tables the statements read that the mapping
    # does not name are empty stubs; the columns each mapped table defaults
    mode: str = "views"
    mapped: dict[str, tuple[str, str]] = field(default_factory=dict, repr=False)
    stubbed: list[str] = field(default_factory=list)
    mapping_defaulted: dict[str, list[str]] = field(default_factory=dict)
    mapping_errors: list[str] = field(default_factory=list)

    def provides(self, table: str) -> bool:
        if self.mode == "mapping":
            return table in self.mapped
        return table in REQUIRED or table in self.optional_present

    def has_column(self, table: str, column: str) -> bool:
        """Whether the tracker holds real data for `table.column` — not a
        default loom fills in."""
        if self.mode == "mapping":
            return table in self.mapped and column not in self.mapping_defaulted.get(table, [])
        if table in self.missing_tables or column in self.missing_columns.get(table, []):
            return False
        if column in self.defaulted.get(table, {}):
            return False
        have = self.described.get(table)
        if have is None:  # a report built without its schema (a test, a remembered verdict)
            return table in REQUIRED or table in self.optional_present
        return column in have or column in self.aliased.get(table, {})

    def capabilities(self) -> dict[str, bool]:
        """What this tracker's data lets loom answer."""
        c = self.has_column
        issues = c("github_issues", "source_id") and c("github_issues", "title")
        prs = c("github_prs", "source_id") and c("github_prs", "title")
        return {
            "items": issues,
            "duplicate_check": issues and c("github_issues", "body"),
            "duplicate_calibration": issues
            and c("github_issues", "state_reason")
            and self.provides("edges"),
            "pull_requests": prs,
            "links": self.provides("edges") and c("edges", "rel_type"),
            "severity_board": issues
            and (c("github_issues", "severity_level") or c("github_issues", "primary_component")),
            "bisect": prs
            and (c("github_prs", "merged_at") or c("github_prs", "closed_at"))
            and (c("github_prs", "affected_file_paths") or c("github_prs", "diff")),
            "incidents": self.provides("core_incidents"),
            "ci_checks": self.provides("checks"),
        }

    def summary(self) -> str:
        if self.mode == "mapping":
            if self.mapping_errors:
                return "mapping: " + "; ".join(self.mapping_errors[:6])
            if self.missing_columns:
                return "mapping: map " + "; ".join(
                    f"{t}.{{{', '.join(cols)}}}" for t, cols in self.missing_columns.items()
                )
            return "ok (mapped: " + ", ".join(sorted(self.mapped)) + ")"
        parts: list[str] = []
        if self.missing_tables:
            parts.append("missing tables " + ", ".join(self.missing_tables))
        for t, cols in self.missing_columns.items():
            parts.append(f"missing columns {t}.{{{', '.join(cols)}}}")
        for t, mismatched in self.type_mismatches.items():
            parts.append(
                f"type mismatch {t}: "
                + ", ".join(f"{c} is {have} not {want}" for c, (want, have) in mismatched.items())
            )
        if not parts:
            parts.append("ok")
        for label, groups in (("aliased", self.aliased), ("defaulted", self.defaulted)):
            if groups:
                parts.append(
                    f"{label} "
                    + ", ".join(f"{t}.{{{', '.join(cols)}}}" for t, cols in groups.items())
                )
        return "; ".join(parts)

    # -- the aliases, applied ---------------------------------

    def projection(self, table: str) -> dict[str, str]:
        """column → expression for every alias and default on `table`."""
        return {**self.aliased.get(table, {}), **self.defaulted.get(table, {})}

    def table_expr(self, database: str, table: str, *, final: bool) -> str:
        """`db.table [FINAL]`, or the same wrapped in a projection that adds
        the aliased columns, the live columns named one by one. The analyzer
        prunes the projection to the columns the statement reads (checked
        live: `EXPLAIN header=1` reads `title, pr_number,
        metadata.has_test.:Bool` for a three-column statement over the
        wrapped table), so the wrap costs no read."""
        base = f"{database}.{table}" + (" FINAL" if final else "")
        cols = self.projection(table)
        if not cols:
            return base
        live = [c for c in self.columns.get(table, []) if c not in cols] or ["*"]
        inner = ", ".join([*live, *(f"{expr} AS {col}" for col, expr in cols.items())])
        return f"(SELECT {inner} FROM {base})"

    def rewrite(self, sql: str, database: str) -> str:
        """Every `database.table [FINAL]` reference to a table with aliases,
        replaced by its `table_expr`. Applied once per statement, in
        `TrackerSource._rows`, after the gate cached this report; a source with
        no aliases gets the statement back unchanged."""
        if self.mapped or self.stubbed:
            sql = self._rewrite_mapped(sql, database)
        for table in {*self.aliased, *self.defaulted}:
            if not self.projection(table):
                continue
            pat = re.compile(rf"(?<![\w.]){re.escape(database)}\.{re.escape(table)}\b(\s+FINAL\b)?")

            def _wrap(m: re.Match[str], t: str = table) -> str:
                return self.table_expr(database, t, final=bool(m.group(1)))

            sql = pat.sub(_wrap, sql)
        return sql

    def _rewrite_mapped(self, sql: str, database: str) -> str:
        """Each logical table reference, replaced by the
        tracker's mapped projection (FINAL kept as the mapping allows) or,
        for a table it does not map, by an empty stub."""
        from server.tracker.domain.mapping import stub_sql

        targets = {**{t: None for t in self.stubbed}, **self.mapped}
        for table, pair in targets.items():
            pat = re.compile(rf"(?<![\w.]){re.escape(database)}\.{re.escape(table)}\b(\s+FINAL\b)?")

            def _sub(m: re.Match[str], t: str = table, p: tuple[str, str] | None = pair) -> str:
                if p is None:
                    return stub_sql(t)
                return p[1] if m.group(1) else p[0]

            sql = pat.sub(_sub, sql)
        return sql

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "database": self.database,
            "ok": self.ok,
            "mode": self.mode,
            "capabilities": self.capabilities(),
            "mapping_defaulted": {t: list(c) for t, c in self.mapping_defaulted.items()},
            "mapping_errors": list(self.mapping_errors),
            "missing_tables": list(self.missing_tables),
            "missing_columns": dict(self.missing_columns),
            "type_mismatches": dict(self.type_mismatches),
            "optional_present": list(self.optional_present),
            "optional_incomplete": dict(self.optional_incomplete),
            "features": dict(self.features),
            "aliased": {t: dict(c) for t, c in self.aliased.items()},
            "defaulted": {t: dict(c) for t, c in self.defaulted.items()},
            "projected_columns": {t: len(c) for t, c in self.columns.items()},
            "summary": self.summary(),
        }


Described = dict[str, dict[str, str]]  # table → column → live type


def _check_columns(
    want: dict[str, str], have: dict[str, str]
) -> tuple[list[str], dict[str, list[str]]]:
    missing = [c for c in want if c not in have]
    mismatched = {
        c: [want[c], have[c]] for c in want if c in have and not family_ok(want[c], have[c])
    }
    return missing, mismatched


def _resolve(
    rep: ContractReport, table: str, missing: list[str], have: dict[str, str]
) -> list[str]:
    """The columns of `missing` no alias covers; the rest land on the report
    (the first alias whose `needs` the live table meets)."""
    still: list[str] = []
    for col in missing:
        for alias in ALIASES.get(table, {}).get(col, ()):
            if all(n in have and family_ok(fam, have[n]) for n, fam in alias.needs.items()):
                target = rep.aliased if alias.needs else rep.defaulted
                target.setdefault(table, {})[col] = alias.expr
                rep.columns[table] = list(have)
                break
        else:
            still.append(col)
    return still


def _renamed(
    rep: ContractReport, described: Described, columns: dict[str, dict[str, str]]
) -> Described:
    """`described` with each configured rename's live column also listed under
    its contract name, and the rename recorded as an alias on `rep`."""
    out = dict(described)
    for table, renames in columns.items():
        have = described.get(table)
        if have is None:
            continue
        view = dict(have)
        for col, live in renames.items():
            if col in have or live not in have:
                continue
            view[col] = have[live]
            rep.aliased.setdefault(table, {})[col] = f"`{live}`"
            rep.columns[table] = list(have)
        out[table] = view
    return out


def diff(
    source: str,
    database: str,
    described: Described,
    columns: dict[str, dict[str, str]] | None = None,
) -> ContractReport:
    """Compare a described schema with the contract — pure. `columns` is the
    source's configured renames, table → {contract column: live column}: a live
    column serves the contract column it names, verified like the column itself."""
    rep = ContractReport(
        source=source, database=database, checked_at=time.monotonic(), described=described
    )
    described = _renamed(rep, described, columns or {})
    for table, want in REQUIRED.items():
        have = described.get(table)
        if have is None:
            rep.missing_tables.append(table)
            continue
        missing, mismatched = _check_columns(want, have)
        missing = _resolve(rep, table, missing, have)
        if missing:
            rep.missing_columns[table] = missing
        if mismatched:
            rep.type_mismatches[table] = mismatched
    for table, want in OPTIONAL.items():
        have = described.get(table)
        if have is None:
            continue
        missing, mismatched = _check_columns(want, have)
        missing = _resolve(rep, table, missing, have)
        if missing or mismatched:
            rep.optional_incomplete[table] = [f"{c} missing" for c in missing] + [
                f"{c} is {have_t} not {want_t}" for c, (want_t, have_t) in mismatched.items()
            ]
        else:
            rep.optional_present.append(table)
    for table, cols in ALIASES.items():  # tables outside the contract a statement reads
        have = described.get(table)
        if have is None or table in REQUIRED or table in OPTIONAL:
            continue
        _resolve(rep, table, [c for c in cols if c not in have], have)
    for name, tables in FEATURES.items():
        rep.features[name] = all(
            not _check_columns(cols, described.get(t) or {})[0]
            and not _check_columns(cols, described.get(t) or {})[1]
            and t in described
            for t, cols in tables.items()
        )
        # A feature column the source lacks reads its alias, or else its type's default:
        # the statements that read it answer as for a row without it instead of failing,
        # and the feature stays absent (`has_column` is False for a default).
        for table, wanted in tables.items():
            have = described.get(table)
            if have is None:
                continue
            for col in _resolve(rep, table, [c for c in wanted if c not in have], have):
                rep.defaulted.setdefault(table, {})[col] = default_expr(wanted[col])
                rep.columns[table] = list(have)
    rep.ok = not rep.missing_tables and not rep.missing_columns and not rep.type_mismatches
    return rep


def diff_mapped(
    source: str,
    database: str,
    mapping: Any,
    live: dict[str, dict[str, str]],
    engines: dict[str, str] | None = None,
) -> ContractReport:
    """A mapped tracker's report — pure. The mapping is
    checked over the live schema of the tables it names; every logical table
    it maps is rendered (plain and FINAL), every other logical table becomes
    an empty stub. `ok` needs no mapping error and the core of each mapped
    table (`columns.CORE`), `github_issues` at least."""
    from server.tracker.domain import mapping as m
    from server.tracker.domain.columns import COLUMNS_READ

    rep = ContractReport(
        source=source,
        database=database,
        checked_at=time.monotonic(),
        mode="mapping",
        described=dict(live),
    )
    chk = m.check(mapping, database, live)
    rep.mapping_errors = list(chk.errors)
    rep.missing_columns = {t: list(c) for t, c in chk.core_missing.items()}
    rep.mapping_defaulted = {t: list(c) for t, c in chk.defaulted.items()}
    for logical, tmap in mapping.tables.items():
        if logical in chk.described and not any(
            e.startswith((f"{logical}:", f"{logical}.")) for e in chk.errors
        ):
            try:
                rep.mapped[logical] = (
                    m.table_sql(logical, tmap, database, live, final=False),
                    m.table_sql(logical, tmap, database, live, final=True),
                )
            except m.MappingError as e:
                rep.mapping_errors.append(str(e))
    rep.stubbed = sorted(t for t in COLUMNS_READ if t not in rep.mapped)
    rep.optional_present = sorted(t for t in rep.mapped if t in OPTIONAL)
    rep.ok = not rep.mapping_errors and not rep.missing_columns and "github_issues" in rep.mapped
    return rep


# --- the views a failing source needs ---------------------------

# The concrete type a family is written as in a view, and the default a
# column the source does not hold reads. The contract compares families, so
# any member works; these are the widest ones a statement never overflows.
_CONCRETE: dict[str, str] = {
    "String": "String",
    "UInt": "UInt64",
    "Int": "Int64",
    "Float": "Float64",
    "DateTime": "DateTime64(3)",
    "UUID": "UUID",
    "JSON": "JSON",
}
_DEFAULTS: dict[str, str] = {
    "String": "''",
    "UInt": "toUInt64(0)",
    "Int": "toInt64(0)",
    "Float": "toFloat64(0)",
    "DateTime": "toDateTime64(0, 3)",
    "UUID": "toUUID('00000000-0000-0000-0000-000000000000')",
    "JSON": "CAST('{}' AS JSON)",
}
# columns a default would make wrong rather than empty: an item's identity and
# the text every search leg reads, and an edge's two ends
IDENTITY_COLUMNS: frozenset[str] = frozenset(
    {
        "id",
        "source_id",
        "number",
        "pr_number",
        "url",
        "title",
        "body",
        "created_at",
        "from_id",
        "from_type",
        "to_id",
        "to_type",
        "rel_type",
    }
)


# An item's id is uuid5(NAMESPACE_DNS, "<kind>:<source_id>")
# (namespaces.doc_id) — loom derives it locally and joins `edges` on it, so a
# view must produce exactly that value. ClickHouse has no uuid5(); this is the
# RFC 4122 v5 construction in SQL (SHA-1 of namespace bytes + name, version and
# variant bits set), checked equal to Python's uuid.uuid5 on ClickHouse 26.5.
_NAMESPACE_DNS_HEX = "6ba7b8109dad11d180b400c04fd430c8"


def uuid5_sql(name_expr: str) -> str:
    """The ClickHouse expression of uuid5(NAMESPACE_DNS, <name_expr>)."""
    h = f"SHA1(concat(unhex('{_NAMESPACE_DNS_HEX}'), {name_expr}))"
    return (
        f"toUUID(UUIDNumToString(toFixedString(concat(substring({h}, 1, 6), "
        f"char(bitOr(bitAnd(reinterpretAsUInt8(substring({h}, 7, 1)), 15), 80)), "
        f"substring({h}, 8, 1), "
        f"char(bitOr(bitAnd(reinterpretAsUInt8(substring({h}, 9, 1)), 63), 128)), "
        f"substring({h}, 10, 7)), 16)))"
    )


# the kinds an item table's rows are, public and private (namespaces.item_kinds)
_ITEM_KIND = {"github_issues": "github_issue", "github_prs": "github_pr"}


UUID5_FUNCTION = "loom_uuid5"


def uuid5_function_sql() -> str:
    """The one-time SQL function a team's views call for ids (idempotent)."""
    return f"CREATE FUNCTION IF NOT EXISTS {UUID5_FUNCTION} AS (name) -> {uuid5_sql('name')};"


def derived_id_sql(table: str) -> str | None:
    """`id` for an item table, from the view's own `source_id` and
    `is_private` (aliases in the same SELECT); None for other tables."""
    kind = _ITEM_KIND.get(table)
    if kind is None:
        return None
    return f"{UUID5_FUNCTION}(concat(if(is_private = 1, '{kind}_private:', '{kind}:'), source_id))"


def concrete_type(family: str) -> str:
    f = family.strip()
    if f.startswith("Array(") and f.endswith(")"):
        return f"Array({concrete_type(f[6:-1])})"
    return _CONCRETE.get(f, f)


def default_expr(family: str) -> str:
    f = family.strip()
    if f.startswith("Array(") and f.endswith(")"):
        return f"CAST([] AS {concrete_type(f)})"
    return _DEFAULTS.get(f, f"CAST(NULL AS Nullable({concrete_type(f)}))")


# --- the contract as a person reads it ---------------------------

# What a column holds, in the words of docs/features/tracker/record-contract.md — shown
# beside the column before anyone connects, so the view's author knows what
# to map. A column without a note is self-explanatory from its name.
COLUMN_NOTES: dict[str, str] = {
    "id": "derived from source_id — the template's expression is exact; keep it",
    "source_id": "'Org/repo#number' — the repository must be one the tracker lists",
    "number": "the issue or PR number",
    "pr_number": "the PR number (= number)",
    "url": "a link a person can open",
    "title": "the item's title — every search reads it",
    "body": "the item's text — the duplicate check reads it",
    "author": "who opened it",
    "labels": "the item's labels",
    "assignees": "who it is assigned to",
    "primary_component": "your team's component taxonomy; '' when none",
    "severity_level": "your severity scale; '' when none",
    "state_reason": "completed / not_planned / duplicate / ''",
    "state": "open / closed / merged",
    "is_private": "1 for a private-repository row",
    "created_at": "when it was opened",
    "updated_at": "when it last changed",
    "closed_at": "when it closed; epoch 0 while open",
    "has_test": "a linked test exists; NULL when unknown",
    "error_code": "the server error code the report names; 0 when none",
    "is_flaky_test": "0/1; 0 when unknown",
    "is_crash_ci": "0/1; 0 when unknown",
    "is_performance": "0/1; 0 when unknown",
    "affected_versions": "versions the issue affects",
    "fix_versions": "versions that carry the fix",
    "is_merged": "0/1",
    "is_backport_pr": "0/1 — derivative PRs are left out of activity reads",
    "is_cherrypick_pr": "0/1",
    "is_sync_pr": "0/1",
    "upstream_pr_number": "the PR a backport repeats; 0 when none",
    "from_id": "the item an edge starts at — the template derives it from that item's "
    "'Org/repo#number'",
    "to_id": "the item an edge ends at — derived the same way",
    "from_type": "github_issue / github_pr / core_incident / support_case (+ _private)",
    "to_type": "as from_type",
    "rel_type": "fixes / related_to / duplicate_of / caused_by / backport_of / reverts …",
}
TABLE_NOTES: dict[str, str] = {
    "github_issues": "one row per issue",
    "github_prs": "one row per pull request",
    "edges": "the links between items: which PR fixes which issue, duplicates, backports",
    "core_incidents": "production incidents, with stack frames for the frames leg",
    "checks": "CI checks and the tests they ran",
}
_TRIAGE_PREFIX = "triage_"


def catalogue(database: str = "<database>") -> dict[str, Any]:
    """The contract as the register page shows it before anyone connects:
    each table — required or optional — with its columns
    (family, the concrete type a view writes, whether a default is safe, a
    note), and a `CREATE OR REPLACE VIEW` template per table with every
    column at its typed default. Pure; the same for every caller."""
    tables: list[dict[str, Any]] = []
    for required, group in ((True, REQUIRED), (False, OPTIONAL)):
        for table, cols in group.items():
            tables.append(
                {
                    "table": table,
                    "required": required,
                    "note": TABLE_NOTES.get(table, ""),
                    "columns": [
                        {
                            "name": c,
                            "family": fam,
                            "type": concrete_type(fam),
                            "must_map": c in IDENTITY_COLUMNS,
                            "note": COLUMN_NOTES.get(c, "")
                            or (
                                "a triage-bot column; keep the default without one"
                                if c.startswith(_TRIAGE_PREFIX)
                                else ""
                            ),
                        }
                        for c, fam in cols.items()
                    ],
                }
            )
    blank = diff("<source>", database, {})
    templates = view_templates(blank, {}, tables=[t["table"] for t in tables])
    return {"database": database, "tables": tables, "templates": templates}


def view_templates(
    rep: ContractReport, described: Described | None = None, *, tables: list[str] | None = None
) -> dict[str, str]:
    """table → a `CREATE OR REPLACE VIEW` that meets the contract, for every
    required table the report fails on — pure, from the report and the live
    schema it was diffed against.

    Loom decides the contract (the columns its statements
    read); the source's owner decides how their tables map onto it, in their
    own ClickHouse, as views. This is the first draft of that mapping: a
    column the table has passes through, a type the family refuses is CAST,
    a missing column reads its typed default — marked `MUST map` when a
    default would make the row wrong (identity, text, an edge's ends) rather
    than merely empty. `<your_table>` is the owner's to fill in; loom never
    runs this SQL."""
    out: dict[str, str] = {}
    failing = (
        tables
        if tables is not None
        else [*rep.missing_tables, *rep.missing_columns, *rep.type_mismatches]
    )
    for table in dict.fromkeys(failing):
        want = REQUIRED.get(table) or (OPTIONAL.get(table) if tables is not None else None)
        if want is None:
            continue
        have = (rep.described if described is None else described).get(table) or {}
        covered = rep.projection(table)
        items: list[tuple[str, str]] = []  # (expression, comment)
        for col, fam in want.items():
            if col in covered:
                continue  # an alias loom applies itself
            if col in have and family_ok(fam, have[col]):
                items.append((col, ""))
            elif col in have:
                items.append(
                    (
                        f"CAST({col} AS {concrete_type(fam)}) AS {col}",
                        f"is {have[col]}, the contract reads {fam}",
                    )
                )
            elif col == "id" and derived_id_sql(table):
                # Derived from source_id — exactly what loom computes
                items.append(
                    (
                        f"{derived_id_sql(table)} AS id",
                        "derived from source_id and is_private — keep as is",
                    )
                )
            elif table == "edges" and col in ("from_id", "to_id"):
                end = col.split("_")[0]
                expr = f"{UUID5_FUNCTION}(concat({end}_type, ':', <{end}_source_id>))"
                items.append(
                    (
                        f"{expr} AS {col}",
                        f"MUST map: replace <{end}_source_id> with the {end} item's "
                        "'Org/repo#number'",
                    )
                )
            else:
                why = (
                    "MUST map: a default makes the row wrong"
                    if col in IDENTITY_COLUMNS
                    else "map it, or keep the default"
                )
                items.append((f"{default_expr(fam)} AS {col}", why))
        # The separator goes before the comment — after it,
        # the comma is commented out and the statement does not parse
        body = "\n".join(
            f"    {expr}{',' if i < len(items) - 1 else ''}" + (f"  -- {note}" if note else "")
            for i, (expr, note) in enumerate(items)
        )
        # DEFINER — the view reads with its creator's rights, so
        # loom's read-only user needs SELECT on the view only; a plain view reads
        # with the reader's rights and every read through it is ACCESS_DENIED
        uses_fn = UUID5_FUNCTION in body
        out[table] = (
            (
                f"-- ids: loom derives uuid5 ids locally; this function makes the same ones\n"
                f"{uuid5_function_sql()}\n\n"
                if uses_fn
                else ""
            )
            + f"-- <your_table>: the table the record lives in, never {rep.database}.{table}\n"
            f"CREATE OR REPLACE VIEW {rep.database}.{table}\n"
            "DEFINER = CURRENT_USER SQL SECURITY DEFINER AS\n"
            f"SELECT\n{body}\n"
            "FROM <your_table>;"
        )
    return out


class ContractFailed(RuntimeError):
    """The source does not meet the contract (the message names what is missing)."""
