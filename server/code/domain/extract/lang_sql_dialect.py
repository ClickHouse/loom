"""The dialect a SQL file is written in, read from its own text.

The shell lanes record a `dialect` facet when a shell script names the client that runs
the SQL (`psql -c`, `clickhouse-client -q`). A `.sql` file names no client, so its dialect
is read from the shapes all over its text: `ENGINE =
MergeTree` and `SETTINGS` are ClickHouse's, `$$ … $$ LANGUAGE plpgsql` and `::text`
casts are PostgreSQL's, `AUTO_INCREMENT` and `DELIMITER` are MySQL's, `GO` lines and
`[dbo].` are SQL Server's, `VARCHAR2` and a `/` line are Oracle's, `AUTOINCREMENT` and
`PRAGMA` are SQLite's, `STRUCT<` and a backticked `project.dataset.table` are
BigQuery's, `VARIANT` and `WAREHOUSE` are Snowflake's, `read_parquet(` is DuckDB's.

Each dialect has a table of cues with weights; the file's dialect is the one whose cue
weight leads by a margin, with a confidence that says how clear the lead was (0.9 when
one dialect holds ≥ 3 weighted cues and twice the runner-up; 0.6 for a thinner lead;
nothing when nothing distinguishes the file — plain ANSI SQL is plain ANSI SQL). The
facet is file-level (ordinal 0) and named like the client table's dialects, so
`code.tests_for({facets:[{kind:"dialect",name:"postgres"}]})` reads shell islands and
SQL files alike. A client-named dialect always outranks a text-cued one: this module
only speaks where no client did.

Gold for the measurement is the repositories' own folder and file names (sakila's
`mysql-sakila-db/`, sqlc's `examples/*/postgresql/`, TimescaleDB's PostgreSQL,
ClickHouse's tests). Outside the extractor stamp with `lang_sql.py`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from server.code.domain.extract.lang_sql_model import Facet

FACET_DIALECT = "dialect"
CONF_CLEAR = 0.9
CONF_LEAN = 0.6


@dataclass(frozen=True)
class Cue:
    pattern: re.Pattern[str]
    weight: float
    once: bool = True  # count the cue once per file (else per match, capped)


def _c(pattern: str, weight: float = 1.0, *, once: bool = True, flags: int = re.I) -> Cue:
    return Cue(re.compile(pattern, flags | re.M), weight, once)


# The table. Weights: 3 = the dialect's own syntax nobody else has; 2 = a
# strong idiom; 1 = a lean (shared by two or three dialects, or common).
DIALECT_CUES: dict[str, tuple[Cue, ...]] = {
    "clickhouse": (
        _c(
            r"\bENGINE\s*=\s*(?:Replicated)?(?:Replacing|Summing|Aggregating|Collapsing|Versioned"
            r"Collapsing|Graphite)?MergeTree\b",
            3,
        ),
        _c(
            r"\bENGINE\s*=\s*(?:Memory|Log|TinyLog|StripeLog|Null|Set|Join|Distributed|Buffer"
            r"|Dictionary|Kafka|URL|File|S3|MaterializedView|View|Merge)\b",
            3,
        ),
        _c(r"^\s*SETTINGS\s+\w+\s*=|\bSETTINGS\s+\w+\s*=", 2),
        _c(r"\bFORMAT\s+(?:TSV|TabSeparated|JSONEachRow|CSV|Pretty\w*|Values|Native|Parquet)\b", 2),
        _c(r"\bsystem\.(?:parts|tables|columns|query_log|numbers|one|settings|merges)\b", 2),
        _c(r"\b(?:LowCardinality|Nullable|Array|Tuple|Map)\(", 2),
        _c(r"\b(?:UInt|Int)(?:8|16|32|64|128|256)\b|\bFloat(?:32|64)\b|\bDateTime64\b", 2),
        _c(
            r"\bPREWHERE\b|\bFROM\s+[\w.]+\s+FINAL\b|\bSAMPLE\s+\d|\bASOF\s+JOIN\b"
            r"|\bARRAY\s+JOIN\b",
            2,
        ),  # `final as (` is a CTE name in dbt projects
        _c(r"^\s*--\s*Tags:", 3),
        _c(r"\{CLICKHOUSE_DATABASE", 3),
        _c(r"\bOPTIMIZE\s+TABLE\b|\bSYSTEM\s+(?:FLUSH|STOP|START|RELOAD)\b|\bON\s+CLUSTER\b", 2),
    ),
    "postgres": (
        _c(r"\$\$|\$[a-z_]+\$", 2),
        _c(r"\bLANGUAGE\s+(?:plpgsql|plpython\w*|plperl|sql)\b", 3),
        _c(r"\bRETURNS\s+(?:TRIGGER|SETOF|TABLE|void)\b", 3),
        _c(
            r"::\s*(?:text|int|integer|bigint|numeric|timestamp\w*|date|boolean|jsonb?|uuid"
            r"|regclass)\b",
            2,
        ),
        _c(r"\b(?:SERIAL|BIGSERIAL|SMALLSERIAL)\b", 3),
        _c(r"\bRETURNING\b", 1),
        _c(r"\bsearch_path\b|\bpg_catalog\.|\bpg_\w+\(", 3),
        _c(
            r"\bCREATE\s+EXTENSION\b|\bON\s+CONFLICT\b"
            r"|\bGENERATED\s+(?:ALWAYS|BY\s+DEFAULT)\s+AS\s+IDENTITY\b",
            2,
        ),
        _c(r"^\\(?:ir?|c|set|echo|copy)\b", 3, flags=0),
        _c(r"\bTIMESTAMPTZ\b|\bJSONB\b|\bBYTEA\b|\bTEXT\[\]|\bINTERVAL\s+'", 2),
        _c(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\b.*\bAS\s+\$", 2),
        _c(r"\bILIKE\b|\bTABLESPACE\b|\bVACUUM\b|\bEXPLAIN\s+ANALYZE\b", 1),
    ),
    "mysql": (
        _c(r"\bAUTO_INCREMENT\b", 3),
        _c(r"^\s*DELIMITER\s", 3),
        _c(r"\bENGINE\s*=\s*(?:InnoDB|MyISAM|MEMORY|ARCHIVE)\b", 3),
        _c(r"\bDEFAULT\s+CHARSET\s*=|\bCHARACTER\s+SET\s+\w+|\bCOLLATE\s*=?\s*utf8", 2),
        _c(r"\bUNSIGNED\b|\bTINYINT\(1\)|\bMEDIUMINT\b|\bENUM\s*\(", 2),
        _c(r"\bON\s+UPDATE\s+CURRENT_TIMESTAMP\b|\bLOCK\s+TABLES\b|\bUNLOCK\s+TABLES\b", 3),
        _c(r"`\w+`\.`\w+`|`\w+`\s*(?:int|varchar|smallint|tinyint|decimal|datetime)\b", 1),
        _c(r"\bSET\s+(?:FOREIGN_KEY_CHECKS|SQL_MODE|NAMES|@@|@\w+)", 2),
        _c(r"\bSHOW\s+(?:TABLES|DATABASES|CREATE\s+TABLE)\b|\bUSE\s+\w+\s*;", 1),
        _c(r"\bINSERT\s+IGNORE\b|\bON\s+DUPLICATE\s+KEY\s+UPDATE\b|\bREPLACE\s+INTO\b", 3),
    ),
    "tsql": (
        _c(r"^\s*GO\s*$", 3),
        _c(r"\[dbo\]\.|\bdbo\.\w+", 3),
        _c(r"\bNVARCHAR\b|\bNCHAR\b|\bUNIQUEIDENTIFIER\b|\bDATETIME2\b|\bBIT\b\s+NOT\s+NULL", 2),
        _c(r"\bIDENTITY\s*\(\s*\d+\s*,\s*\d+\s*\)", 3),
        _c(r"\bSELECT\s+TOP\s*\(?\d", 2),
        _c(r"\bSET\s+(?:ANSI_NULLS|QUOTED_IDENTIFIER|NOCOUNT)\s+(?:ON|OFF)\b", 3),
        _c(r"\bEXEC(?:UTE)?\s+sp_\w+|\bsys\.(?:objects|tables|columns)\b|\bOBJECT_ID\s*\(", 3),
        _c(
            r"\bCREATE\s+(?:CLUSTERED|NONCLUSTERED)\s+INDEX\b"
            r"|\bWITH\s*\(\s*(?:PAD_INDEX|FILLFACTOR)",
            3,
        ),
        _c(r"\bBEGIN\s+TRAN(?:SACTION)?\b|\bRAISERROR\b|\bPRINT\s+'", 2),
        _c(r"\bGETDATE\s*\(|\bISNULL\s*\(|\bLEN\s*\(", 1),
    ),
    "oracle": (
        _c(r"\bVARCHAR2\b|\bNVARCHAR2\b|\bNUMBER\s*\(\s*\d+\s*(?:,\s*\d+\s*)?\)", 3),
        _c(r"^/\s*$", 3, flags=0),
        _c(r"\bCREATE\s+SEQUENCE\b.*\b(?:NOCACHE|NOCYCLE|INCREMENT\s+BY)\b", 2),
        _c(r"\bFROM\s+DUAL\b|\bSYSDATE\b|\bNVL\s*\(|\bROWNUM\b", 3),
        _c(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:PACKAGE|TRIGGER|PROCEDURE|FUNCTION)\b.*"
            r"\b(?:IS|AS)\s*$",
            1,
        ),
        _c(r"\bBEGIN\b[\s\S]{0,400}?\bEND\s*;\s*\n\s*/", 3),
        _c(r"\bCLOB\b|\bBLOB\b\s|\bRAW\s*\(", 1),
        _c(r"\bTO_DATE\s*\(|\bTO_CHAR\s*\(|\bDECODE\s*\(", 1),
        _c(r"\bTABLESPACE\s+\w+|\bSTORAGE\s*\(", 2),
    ),
    "sqlite": (
        _c(r"\bAUTOINCREMENT\b", 3),
        _c(r"^\s*PRAGMA\s+\w+", 3),
        _c(r"\bINTEGER\s+PRIMARY\s+KEY\b", 1),  # every dialect writes it; SQLite means rowid
        _c(r"\bsqlite_master\b|\bsqlite_sequence\b|\bsqlite_version\s*\(", 3),
        _c(r"\bWITHOUT\s+ROWID\b|\bIF\s+NOT\s+EXISTS\b.*\bAUTOINCREMENT", 3),
        _c(r"\bINSERT\s+OR\s+(?:REPLACE|IGNORE)\b|\bGLOB\b|\bdatetime\s*\(\s*'now'", 2),
    ),
    "bigquery": (
        _c(r"`[\w-]+\.[\w-]+\.[\w-]+`", 3),
        _c(r"\bSTRUCT\s*<|\bARRAY\s*<", 3),
        _c(
            r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP\s+)?TABLE\b.*\bPARTITION\s+BY\s+"
            r"(?:DATE|TIMESTAMP_TRUNC)\b",
            2,
        ),
        _c(
            r"\bSAFE_CAST\s*\(|\bUNNEST\s*\(|\bGENERATE_ARRAY\s*\("
            r"|\bAPPROX_COUNT_DISTINCT\s*\(",
            2,
        ),
        _c(r"\bINT64\b|\bFLOAT64\b|\bBYTES\b\s*(?:,|\))", 2, flags=0),  # ClickHouse has Int64
        _c(r"\bOPTIONS\s*\(\s*(?:description|expiration_timestamp|labels)\s*=", 3),
    ),
    "snowflake": (
        _c(r"\bVARIANT\b|\bOBJECT\b\s*(?:,|\))|\bFLATTEN\s*\(", 3),
        _c(r"\bWAREHOUSE\s*=|\bCREATE\s+(?:OR\s+REPLACE\s+)?WAREHOUSE\b|\bUSE\s+WAREHOUSE\b", 3),
        _c(r"\bCREATE\s+(?:OR\s+REPLACE\s+)?(?:STAGE|FILE\s+FORMAT|STREAM|TASK|PIPE)\b", 3),
        _c(r"\bQUALIFY\b|\bLATERAL\s+FLATTEN\b|\bTRY_CAST\s*\(|\bPARSE_JSON\s*\(", 2),
        _c(r"@[\w.]+/|\bCOPY\s+INTO\b.*\bFILE_FORMAT\b", 3),
    ),
    # Spark SQL (the Iceberg / Delta integration tests submit it through `spark.sql`);
    # shares `ARRAY<STRUCT<…>>` with BigQuery, so only its own syntax decides
    "sparksql": (
        _c(r"\bUSING\s+(?:iceberg|delta|parquet|orc|hudi)\b", 3),
        _c(r"\bTBLPROPERTIES\s*\(|\bspark_catalog\b|\bnamed_struct\s*\(", 3),
        _c(r"\bINSERT\s+OVERWRITE\b|\bMSCK\s+REPAIR\b|\bCLUSTERED\s+BY\b", 2),
        _c(r"\bPARTITIONED\s+BY\s*\(|\bLATERAL\s+VIEW\s+explode\b", 1),
    ),
    "duckdb": (
        _c(r"\bread_parquet\s*\(|\bread_csv(?:_auto)?\s*\(|\bread_json(?:_auto)?\s*\(", 3),
        _c(r"\bINSTALL\s+\w+\s*;|\bLOAD\s+\w+\s*;", 2),
        _c(r"\bPRAGMA\s+(?:threads|memory_limit|enable_progress_bar)\b", 3),
        _c(r"\bSTRUCT_PACK\s*\(|\bLIST_VALUE\s*\(|\bUNNEST\s*\(.*\brecursive\b", 3),
        _c(r"\bSUMMARIZE\b|\bDESCRIBE\s+SELECT\b|\bFROM\s+'[^']+\.(?:parquet|csv)'", 3),
    ),
}

_CLEAR_MIN_WEIGHT = 3.0
_CLEAR_RATIO = 2.0
_LEAN_MIN_WEIGHT = 2.0
_LEAN_RATIO = 1.5


def dialect_scores(text: str) -> dict[str, float]:
    """{dialect: weighted cue score} over the file's text."""
    scores: dict[str, float] = {}
    for dialect, cues in DIALECT_CUES.items():
        total = 0.0
        for cue in cues:
            if cue.once:
                if cue.pattern.search(text):
                    total += cue.weight
            else:
                total += cue.weight * min(len(cue.pattern.findall(text)), 5)
        if total:
            scores[dialect] = total
    return scores


def detect_dialect(text: str) -> tuple[str, float]:
    """(dialect, confidence) or ('', 0.0) when the text does not lean."""
    scores = dialect_scores(text)
    if not scores:
        return "", 0.0
    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best, top = ranked[0]
    second = ranked[1][1] if len(ranked) > 1 else 0.0
    if top >= _CLEAR_MIN_WEIGHT and top >= _CLEAR_RATIO * second:
        return best, CONF_CLEAR
    if top >= _LEAN_MIN_WEIGHT and top >= _LEAN_RATIO * second:
        return best, CONF_LEAN
    return "", 0.0


def dialect_facets(text: str) -> list[Facet]:
    """The file-level `dialect` facet, or none."""
    dialect, conf = detect_dialect(text)
    return [Facet(FACET_DIALECT, dialect, 0, 1, conf)] if dialect else []


CONF_FOLDER = 0.5
# unanimous: every text-decided file in the directory names the same dialect (a 3-file /
# 80 % rule decides nothing in two-file folders, e.g. sqlc's or sakila's)
FOLDER_MIN_FILES = 1
FOLDER_MIN_SHARE = 1.0


def folder_dialects(
    decided: list[tuple[str, str, float]],
    undecided: list[str],
    *,
    min_files: int = FOLDER_MIN_FILES,
    min_share: float = FOLDER_MIN_SHARE,
) -> dict[str, str]:
    """An undecided SQL file takes the dialect its own directory's text-decided files
    agree on — `path → dialect` for the undecided paths whose directory has at least
    `min_files` decided files (confidence above CONF_FOLDER, so inherited rows never
    vote) and whose leader holds `min_share` of them. Same directory only: a
    repository's per-dialect folders (sakila's `mysql-sakila-db/`, sqlc's `postgresql/`)
    are the unit, a parent that mixes them decides nothing; the defaults ask for
    unanimity, so one decided `schema.sql` decides its `query.sql` and a folder holding
    two dialects decides nothing."""
    import posixpath
    from collections import Counter, defaultdict

    votes: dict[str, Counter[str]] = defaultdict(Counter)
    for path, dialect, conf in decided:
        if dialect and conf > CONF_FOLDER:
            votes[posixpath.dirname(path)][dialect] += 1
    out: dict[str, str] = {}
    for path in undecided:
        tally = votes.get(posixpath.dirname(path))
        if not tally:
            continue
        lead, n = tally.most_common(1)[0]
        if n >= min_files and n / sum(tally.values()) >= min_share:
            out[path] = lead
    return out


__all__ = [
    "CONF_FOLDER",
    "FOLDER_MIN_FILES",
    "FOLDER_MIN_SHARE",
    "folder_dialects",
    "CONF_CLEAR",
    "CONF_LEAN",
    "DIALECT_CUES",
    "FACET_DIALECT",
    "Cue",
    "detect_dialect",
    "dialect_facets",
    "dialect_scores",
]
