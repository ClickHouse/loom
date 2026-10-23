# ruff: noqa: E501
"""Move a loom vector table onto the `Quantized` codec.

A column's codec and type are CREATE-only, so the table is rebuilt: a twin
`loom.<t>_rebuild` from `SHOW CREATE` with the vector columns rewritten, every
row copied (an empty or all-zero vector padded to the fixed length; the
preflight refuses a real vector of another width), a delta keyed on the
ReplacingMergeTree version column (or an anti-join on the sorting key for a
plain MergeTree), a FULL OUTER JOIN consistency gate on the sorting key (with
a hash of every column, the rebuilt vectors as the twin stores them), a
distance-based recall gate, then `EXCHANGE TABLES`; the old table stays as
`loom.<t>_pre_rebuild` for `--rollback` until `--drop-pre-rebuild`, as with
scripts/ops/table_rebuild.py (the swap, the signals held over it, the next
step and the summary line are shared in server/core/schema/table_rebuild.py).

    python -P -m scripts.ops.vector_codec_rebuild --table memory_chunks --columns embedding --dry-run
    python -P -m scripts.ops.vector_codec_rebuild --table memory_chunks --columns embedding --yes
    …  --table entity_vectors --columns entity_embedding --chain LZ4 --yes     # repeated vectors: a general codec after the codes
    …  --table code_chunks --columns embedding --reuse --yes                  # continue from this run's twin
    …  --table memory_embeddings --columns embedding,memory_key_embedding --index-granularity 256 --require-patch-free --yes
    …  --table memory_embeddings --columns embedding,memory_key_embedding --method turboquant --yes
    …  --table code_summaries --columns embedding --index-granularity 256 --gate-fetch-multiplier 10 --yes
    …  --table memory_chunks --columns embedding --rollback --yes
    …  --table memory_chunks --columns embedding --drop-pre-rebuild --yes

It reads `LOOM_CH_*` like `loom migrate` and prints the ClickHouse it talks to
first. Without `--yes` a changing action prints its plan and stops.

The twin's vector columns are `Array(BFloat16) CODEC(Quantized(<method>, dim, 0))`.
`--method` is `rabitq` (one-bit codes, default), `turboquant` (two-bit) or
`int8`; the recall gate reads the twin through the quantized codes at
`--gate-fetch-multiplier`, as the search legs do.

The dimension: a coded column of the live table states it, a plain column
takes `LOOM_EMBEDDING_DIM`, which must equal the served model's width (the width
the server probes and logs at boot, and writes at), and the tool refuses while
it holds real vectors of another width. When every such vector has one other
width and the server logs it as the model's, the fix is to set
`LOOM_EMBEDDING_DIM` to it, not to re-embed. Every named column must agree.

Run it in a maintenance window with every writer and erasure stopped and
in-flight writes drained: it fails closed and never repairs the live table.
Its width refusal, its gates and its pre-swap checks are in
docs/operations/maintenance.md#compaction-and-optimize.

`--require-patch-free`: the twin-and-EXCHANGE also sheds the patch parts of
past lightweight UPDATEs (`OPTIMIZE FINAL` / `APPLY PATCHES` do not). The flag
logs the live table's active patch parts, refuses to EXCHANGE unless the twin
has none, and logs the count after.

Preconditions: one ClickHouse version (26.7+) on every replica of the
cluster `--cluster` names (`default`; `''` checks this server only), `enable_quantized_codec`
settable or held at 1 for this user (`allow_suspicious_codecs` too with `--chain`; a setting held
at 1 is never sent), every replica writable, the `SYSTEM FLUSH ASYNC INSERT QUEUE` and
`SYSTEM SYNC REPLICA` privileges on the table (the preflight runs both once), `SELECT` on
`system.zookeeper` and `system.replication_queue` on a ReplicatedMergeTree (each read after
the drain reads the replication log and queue there, and a run that cannot read them refuses), and no row
written in the last hour with an empty vector in the named columns (writers'
fixed-length placeholders must be deployed first: a coded column rejects an
empty vector). ReplicatedMergeTree runs connect directly to one replica throughout
the run: each consistency and width read after the drain also reads, in the same
statement, whether the replica answering it has fetched every part its replication
log names, and fails the run when it has not. Each consistency read of the twin
follows a `SYSTEM SYNC REPLICA ... LIGHTWEIGHT` of it, which needs that privilege on
`loom.<t>_rebuild` too. An empty table is rebuilt; the recall gate has nothing to probe.

After a swap the tool compares the rollback copy with the live table. A change only
the rollback copy holds names `--rollback`; a change the live table holds (a write
to it after the swap, or a delete in the old table after the last check) or a key that differs at one version, which either table may hold,
keeps both tables for inspection.
`--drop-pre-rebuild` runs the same comparison first, after a swap and after a
rollback, and refuses (keeping the copy) while the copy it would drop holds a change.
`--rollback` runs it before its exchange and refuses, exchanging nothing, when the live
table holds a change the copy back would lose.

The twin is the run's lock: an existing twin refuses the run unless
`--reuse` (continue from it, refused when its layout is not the one this run
builds) or `--restart` (drop it and copy again), and the swap refuses when the twin is no longer the one this run created or reused,
or when the live table was altered after the twin was created.
An `ABORT_REBUILD` file in `LOOM_REBUILD_DIR` (default `.`) stops the run
before the copy and before the swap, leaving the twin. The UUIDs of each twin
the tool creates and of the live table then go to `<table>_rebuild.uuid` there,
and the swap removes it first (refusing when it cannot): a later run names a
twin with that UUID a leftover never exchanged in while the live table still has
the recorded UUID, which any exchange changes.

Exit codes (shared with table_rebuild): 0 done or nothing to do, 2 aborted,
3 refused (widths among them), 4 needs `--yes`, 5 a gate failed (consistency,
recall, patch parts, the pre-swap checks), 7 postcondition failed, 130 interrupted.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import math
import os
import re
import shlex
import statistics
import sys
import threading
import time
import uuid
from collections.abc import Callable
from functools import cached_property
from pathlib import Path
from typing import Any

if (_ROOT := str(Path(__file__).resolve().parents[2])) not in sys.path:  # run as a file
    sys.path.insert(0, _ROOT)

from server.core.ch_names import database, unqualify
from server.core.schema import table_rebuild as tr
from server.core.schema.table_rebuild import (
    ABORTED,
    GATE_FAILED,
    OK,
    POSTCONDITION,
    REFUSED,
    UNCONFIRMED,
    Aborted,
    log,
)
from server.core.schema.vector_codec_ddl import (
    CHAIN_SETTING,
    CODEC_SETTING,
    CODEC_SETTINGS_SQL,
    CodecSettings,
    statement_settings,
)

# Fixed-arity `Quantized(method, dim, 0)` methods of ClickHouse 26.7+ (`prefix` /
# `product` take their own arguments and are not a rebuild target).
CODEC_METHODS: tuple[str, ...] = ("rabitq", "turboquant", "int8")
COPY_RECEIVE_TIMEOUT_S = 7200
# The full copy and the rollback's catch-up copy run under this limit.
# not a setting: stays below the client's read timeout
COPY_EXECUTION_TIME_S = COPY_RECEIVE_TIMEOUT_S - 200
# The default of --cluster; a printed next step names any other.
DEFAULT_CLUSTER = "default"
# not a setting: the default of this operator tool's --statement-timeout-s flag
DEFAULT_STATEMENT_TIMEOUT_S = 1800.0
# The defaults of the recall gate's flags; a printed next step names any other.
# not a setting: the defaults of this operator tool's --gate-fetch-multiplier and --probes flags
DEFAULT_GATE_FETCH_MULTIPLIER = 3.0
DEFAULT_PROBES = 20
# How far above the exact 20th distance a twin hit may read and still count.
DISTANCE_TOLERANCE = 1e-6
COUNT_SETTLE_ATTEMPTS = 8
COUNT_SETTLE_SLEEP_S = 15
# Mutation timestamps come from other replicas; allow up to one minute of clock skew.
# not a setting: a bound on replica clock skew, not an operator knob
CLOCK_SKEW_MARGIN_S = 60
# The next step when only the rollback copy holds a change; `{run}` is the run line.
KEEP_AND_ROLL_BACK = (
    "Keep writers and erasure stopped and run `{run} --rollback --yes`, which puts the old "
    "table, the change included, back in; do not drop the rollback copy"
)
KEEP_BOTH = (
    "Keep writers and erasure stopped and both tables as they are, and inspect the "
    "difference by hand before any --rollback or --drop-pre-rebuild"
)
# Why the tool cannot tell which of two tables on the target layout is the new one.
SAME_LAYOUT = (
    "are both on the target layout and their metadata times do not show which is the new "
    "one: created in the same second, or on a database engine other than Atomic or Replicated"
)
# The UUID system.tables shows for a table on a database without table UUIDs.
ZERO_UUID = "00000000-0000-0000-0000-000000000000"
# A codec clause with one level of nested parentheses: CODEC(Quantized('rabitq', 3072, 0), LZ4).
_CODEC = r"CODEC\((?:[^()]|\([^()]*\))*\)"


def pad(dim: int) -> str:
    """A fixed-length all-zero placeholder vector."""
    return f"arrayResize(CAST([] AS Array(BFloat16)), {int(dim)}, toBFloat16(0))"


def rows_settled(twin_rows: int, source_rows: int, tolerance: float = 0.005) -> bool:
    """The twin holds the source's rows (both counted under FINAL for a Replacing engine)."""
    return twin_rows >= source_rows * (1 - tolerance)


def sql_literal(v: Any) -> str:
    """A version-column value as a ClickHouse literal for the delta WHERE."""
    if isinstance(v, _dt.datetime):
        return f"toDateTime('{v:%Y-%m-%d %H:%M:%S}')"
    if isinstance(v, str):  # repr() may pick double quotes, which ClickHouse reads as an identifier
        return "'" + v.replace("\\", "\\\\").replace("'", "\\'") + "'"
    return str(v)


def key_literal(v: Any) -> str:
    """A sorting-key value as a ClickHouse literal for a repair's key list. Pure."""
    if isinstance(v, uuid.UUID):
        return f"toUUID('{v}')"
    return sql_literal(v)


REPAIR_BATCH = 500  # keys per repair INSERT: a literal list the statement carries


def patch_parts_sql(name: str) -> str:
    """Active patch parts of one schema table (the --require-patch-free gate). Pure."""
    return (
        "SELECT countIf(startsWith(name, 'patch')) FROM system.parts "
        f"WHERE database = currentDatabase() AND table = '{name}' AND active"
    )


def mutations_sql(cluster_mutations: str, since: int) -> str:
    """Count the unfinished mutations of table `{t}` in database `{db}`, and every one
    created since `since` (a Unix time): a finished ALTER UPDATE keeps the key and its
    version. Pure."""
    return (
        f"SELECT count() FROM {cluster_mutations} WHERE database = {{db:String}} "
        f"AND table = {{t:String}} AND (NOT is_done OR create_time >= toDateTime({int(since)}))"
    )


def codec_method(method: str) -> str:
    """The `Quantized` method the twin is built with; refuses a name the runner has not measured."""
    m = (method or "rabitq").strip().lower()
    if m not in CODEC_METHODS:
        raise SystemExit(f"--method must be one of {', '.join(CODEC_METHODS)}; got {method!r}")
    return m


def with_granularity(ddl: str, granularity: int) -> str:
    """`ddl` with `index_granularity = granularity` (the rescore
    reads whole granules, so a coded table pays only at small ones)."""
    if re.search(r"index_granularity = \d+", ddl):
        return re.sub(
            r"index_granularity = \d+", f"index_granularity = {int(granularity)}", ddl, count=1
        )
    if "\nSETTINGS " in ddl:
        return ddl.replace("\nSETTINGS ", f"\nSETTINGS index_granularity = {int(granularity)}, ", 1)
    return ddl.rstrip() + f"\nSETTINGS index_granularity = {int(granularity)}"


def _column_re(col: str) -> re.Pattern[str]:
    """One vector column's declaration: type, optional DEFAULT, optional CODEC."""
    return re.compile(
        rf"`{re.escape(col)}` Array\((?:Float32|BFloat16)\)((?: DEFAULT (?:(?! CODEC\().)*?)?)(?: {_CODEC})?(?=,?$)",
        re.MULTILINE,
    )


def column_decl(ddl: str, col: str) -> str:
    """The column's declaration line as `SHOW CREATE` prints it, or ''. Pure."""
    m = _column_re(col).search(ddl)
    return m.group(0) if m else ""


def column_shape(ddl: str, col: str) -> str:
    """The column's type and codec, its DEFAULT left out (`SHOW CREATE`
    normalises a default expression differently from how it was written). Pure."""
    m = _column_re(col).search(ddl)
    if not m:
        return ""
    return m.group(0).replace(m.group(1), "", 1) if m.group(1) else m.group(0)


def vector_columns(ddl: str) -> list[str]:
    """Every vector column the DDL declares, in order. Pure."""
    return re.findall(r"^\s*`(\w+)` Array\((?:Float32|BFloat16)\)", ddl, re.MULTILINE)


def vector_dim(ddl: str, col: str) -> int | None:
    """The dimension a coded column states in its codec, else None. Pure."""
    m = re.search(r"Quantized\('\w+', (\d+)", column_decl(ddl, col))
    return int(m.group(1)) if m else None


def rewrite_ddl(
    ddl: str,
    table: str,
    twin: str,
    columns: list[str],
    chain: str | None,
    method: str = "rabitq",
    granularity: int = 0,
    *,
    dim: int,
) -> str:
    """The twin's CREATE: same DDL, name swapped, each vector column re-declared
    coded (a Float32 column moves to BFloat16), `index_granularity` replaced
    when `granularity` > 0. Pure."""
    codec = f"CODEC(Quantized('{codec_method(method)}', {int(dim)}, 0)" + (
        f", {chain})" if chain else ")"
    )
    out = re.sub(rf"^CREATE TABLE {re.escape(table)}\b", f"CREATE TABLE {twin}", ddl, count=1)
    for col in columns:
        out, n = _column_re(col).subn(
            lambda m, c=col: f"`{c}` Array(BFloat16){m.group(1)} {codec}", out, count=1
        )
        if n != 1:
            raise SystemExit(f"column {col} not found as a vector column in the DDL")
    return with_granularity(out, granularity) if granularity > 0 else out


def on_target(
    ddl: str,
    columns: list[str],
    chain: str | None,
    method: str,
    dim: int,
    granularity: int = 0,
) -> bool:
    """Whether every named column already has the declaration the rebuild would give it,
    and the table the `granularity` asked for (0 = any). Pure."""
    want = rewrite_ddl(ddl, "_", "_", columns, chain, method, 0, dim=dim)
    if granularity > 0 and index_granularity(ddl) != granularity:
        return False
    return all(column_shape(ddl, c) == column_shape(want, c) for c in columns)


def index_granularity(ddl: str) -> int:
    """The table's index_granularity, 8192 when the DDL does not state it. Pure."""
    m = re.search(r"index_granularity = (\d+)", ddl)
    return int(m.group(1)) if m else 8192


def exact_vector_expr(ddl: str, column: str) -> str:
    """The source column as the recall gate's exact read ranks it: cast to BFloat16 when the
    source is Float32 (the twin stores BFloat16), so both sides score the same values. Pure."""
    if f"`{column}` Array(Float32)" in ddl:
        return f"CAST({column} AS Array(BFloat16))"
    return column


def parse_engine(ddl: str) -> tuple[str, str | None, list[str]]:
    """(engine name, version column or None, sorting key columns)."""
    m = re.search(r"ENGINE = (\w+)\(([^)]*)\)", ddl)
    engine, args = (m.group(1), m.group(2)) if m else ("", "")
    version = None
    if "Replacing" in engine:
        parts = [a.strip().strip("'") for a in args.split(",") if a.strip()]
        # Shared/Replicated engines lead with the Keeper path and replica name;
        # the version column is the next argument (an is_deleted column may follow).
        if engine.startswith(("Shared", "Replicated")):
            parts = parts[2:]
        version = parts[0] if parts else None
    ob = re.search(r"ORDER BY \((.*)\)\s*$", ddl, re.M) or re.search(r"ORDER BY (\w+)", ddl)
    key = tr.key_columns(ob.group(1)) if ob else []
    return engine, version, [k.strip("`") for k in key]


def read_final(engine: str) -> str:
    """' FINAL' where the engine is a ReplacingMergeTree, so a read sees the rows Loom
    serves; '' for a plain MergeTree. Pure."""
    return " FINAL" if "Replacing" in engine else ""


def nonzero(column: str) -> str:
    """The predicate for a vector that is not an all-zero placeholder. Pure."""
    return f"arrayExists(x -> x != 0, {column})"


def mixed_width_sql(table: str, column: str, fin: str, *, dim: int) -> str:
    """Count nonzero vectors of another width; zero placeholders can be resized."""
    return (
        f"SELECT length({column}) AS w, count() FROM loom.{table}{fin} "
        f"WHERE length({column}) != {int(dim)} AND {nonzero(column)} "
        "GROUP BY w ORDER BY w"
    )


def stored_vector(column: str, *, dim: int) -> str:
    """A source vector as the twin stores it: BFloat16 at the fixed length, an empty or
    all-zero vector of another width as the zero placeholder. Pure."""
    return f"if(length({column}) = {int(dim)}, CAST({column} AS Array(BFloat16)), {pad(dim)})"


def consistency_sql(
    src: str,
    twin: str,
    fin: str,
    version: str | None,
    key: list[str],
    hashed: list[str],
    vectors: list[str],
    *,
    dim: int,
) -> str:
    """Compare versions or per-key counts, and a hash per key of the `hashed` columns and
    the rebuilt `vectors` (the source's as the copy writes them), separating absent keys
    from mismatches. The hash catches a change that keeps the key and its version (an
    UPDATE of any column, an erasure tombstone), whether or not its patch part is still
    there; `tuple` keeps a NULL from making the hash NULL. join_use_nulls=0 supplies
    default values for the missing side. Pure."""
    ident = ", ".join(key)
    plain = [f"`{c}`" for c in hashed]

    def side(name: str, vecs: list[str]) -> str:
        row_hash = f"cityHash64(tuple({', '.join(plain + vecs)}))"
        if version:
            cols = f"{version}, {row_hash} AS __hash"
            return f"SELECT {ident}, {cols}, 1 AS __present FROM loom.{name}{fin}"
        cols = f"count() AS n, sum({row_hash}) AS __hash"
        return f"SELECT {ident}, {cols}, 1 AS __present FROM loom.{name}{fin} GROUP BY {ident}"

    if version:
        vcmp = f"(s.{version} != t.{version} OR s.__hash != t.__hash)"
    else:
        vcmp = "(s.n != t.n OR s.__hash != t.__hash)"
    src_side = side(src, [stored_vector(c, dim=dim) for c in vectors])
    twin_side = side(twin, [f"`{c}`" for c in vectors])
    return (
        f"SELECT countIf(s.__present = 1 AND t.__present = 1 AND {vcmp}) AS mismatched, "
        "countIf(t.__present = 0) AS source_only, countIf(s.__present = 0) AS twin_only "
        f"FROM ({src_side}) AS s FULL OUTER JOIN ({twin_side}) AS t USING ({ident})"
    )


def newer_sql(old: str, new: str, fin: str, version: str | None, key: list[str]) -> str:
    """Count the keys both tables hold where `old` is ahead and where `new` is: a newer
    version, or on a table without a version column more rows under the key. Pure."""
    ident = ", ".join(key)

    def side(name: str) -> str:
        if version:
            return f"SELECT {ident}, {version} AS v FROM loom.{name}{fin}"
        return f"SELECT {ident}, count() AS v FROM loom.{name}{fin} GROUP BY {ident}"

    return (
        f"SELECT countIf(o.v > n.v), countIf(o.v < n.v) FROM ({side(old)}) AS o "
        f"INNER JOIN ({side(new)}) AS n USING ({ident})"
    )


# Whether the replica answering a read has fetched every part of the tables in
# {tables:Array(String)} that their replication logs name: it has copied the whole log
# into its queue, and the queue holds nothing but merges, which keep the rows. A
# mutation fails the run through the mutation count instead. log_max_index comes from
# the newest log entry's name, so a log of one entry this replica has not pulled reads
# log_pointer = log_max_index = 0 as an empty log does; only the tables in
# {empty:Array(String)}, whose log Keeper lists with no entry, pass on those zeros.
REPLICA_SYNCED_SQL = (
    "(SELECT countIf(log_pointer > log_max_index "
    "OR (has({empty:Array(String)}, table) AND log_pointer = 0 AND queue_size = 0)) "
    "= length({tables:Array(String)}) "
    "FROM system.replicas WHERE database = currentDatabase() "
    "AND has({tables:Array(String)}, table)) "
    "AND (SELECT count() FROM system.replication_queue WHERE database = currentDatabase() "
    "AND has({tables:Array(String)}, table) AND type NOT IN ('MERGE_PARTS', 'MUTATE_PART')) = 0"
)


def tied_keys_sql(src: str, version: str, key: list[str]) -> str:
    """Count the keys whose newest version has more than one row. FINAL keeps the last
    one inserted, and the copy's parallel INSERT can insert them in another order, so
    such a key can differ between the source and the twin however often it is copied. Pure."""
    ident = ", ".join(key)
    return (
        f"SELECT countIf(n_at_max > 1) FROM (SELECT {ident}, argMax(n, {version}) AS n_at_max "
        f"FROM (SELECT {ident}, {version}, count() AS n FROM loom.{src} GROUP BY {ident}, {version}) "
        f"GROUP BY {ident})"
    )


def tied_failure(table: str, n: int) -> str:
    """The gate's message for keys with more than one row at their newest version. Pure."""
    return (
        f"GATE FAILED: {n} key(s) of loom.{table} hold more than one row at their newest "
        "version, and FINAL can keep a different one of them in the twin; no swap. Inside the "
        f"window run OPTIMIZE TABLE loom.{table} FINAL, then rerun with --restart"
    )


def twin_only_failure(n: int) -> str:
    """The gate's message for keys only the twin holds. Pure."""
    return (
        f"GATE FAILED: {n} key(s) only in the twin: rows were removed from the source after "
        "they were copied (an erasure or delete), and the swap would bring them back; no swap. "
        "Stop writes and erasures and rerun with --restart"
    )


def replace_select(columns: list[str], *, dim: int) -> str:
    """The copy's SELECT: every named column at the fixed length (an empty or all-zero
    vector padded to it; the preflight refuses a real vector of another width)."""
    vecs = ", ".join(f"{stored_vector(c, dim=dim)} AS {c}" for c in columns)
    return f"SELECT * REPLACE ({vecs})"


def twin_read(column: str, lit: str, multiplier: float) -> tuple[str, dict[str, Any]]:
    """(distance expression, settings) the recall gate reads the twin with: the
    read the search legs issue, the quantized codes at the leg's fetch
    multiplier. Pure."""
    return f"cosineDistance({column}, {lit})", {
        "vector_search_use_quantized_codes": 1,
        "vector_search_index_fetch_multiplier": multiplier,
    }


def vector_filter(column: str, *, dim: int) -> str:
    """The predicate that keeps placeholder rows out of a probe's top-20: a placeholder
    is all zeros, so its cosine distance is NaN and it would sort last. Pure."""
    return f"length({column}) = {int(dim)} AND {nonzero(column)}"


_CLUSTER_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def replicas_source(cluster: str) -> Callable[[str], str]:
    """`system.<t>` on every replica of `cluster`, or on this server for ''.
    The name is spliced into the query, so it must be a plain name."""
    if cluster and not _CLUSTER_NAME.fullmatch(cluster):
        raise ValueError(f"not a cluster name: {cluster!r}")
    if not cluster:
        return lambda t: f"system.{t}"
    return lambda t: f"clusterAllReplicas('{cluster}', system.{t})"


class ReplicaBehind(RuntimeError):
    """A read after the drain was answered by a replica that has not fetched every part,
    or the twin's sync before such a read failed."""


class VectorRebuild:
    def __init__(self, ch: Any, args: argparse.Namespace, client: Any = None) -> None:
        self.ch, self.a = ch, args
        self.client = client or (lambda: ch)
        self.t = args.table
        self.twin, self.pre = self.t + tr.TWIN_SUFFIX, self.t + tr.PRE_SUFFIX
        self.cols = [x.strip() for x in args.columns.split(",") if x.strip()]
        self.chain = args.chain or None
        self.abort = Path(args.abort_file)
        # the UUIDs of the twin this tool created and of the live table then, which a
        # later run reads
        self.twin_record = self.abort.parent / f"{self.twin}.uuid"
        self.twin_uuid = ""
        self.replicas = replicas_source(args.cluster)
        self.rows_copied = 0
        self.dim = 0
        self.fin = ""  # ' FINAL' where the live table is a ReplacingMergeTree
        self.since = 0  # the server's Unix time before the preflight's mutation check
        self.reads: dict[str, Any] = {}  # settings every read takes; sequential after the drain
        self.replicated = False  # the live table is a ReplicatedMergeTree
        self.next_step = ""  # the summary's next step, set where a check decides it

    # -- small reads -------------------------------------------------------
    def q(self, sql: str, parameters: dict[str, Any] | None = None, **st: Any) -> list[Any]:
        return list(
            self.ch.query(
                sql,
                parameters=parameters,
                settings={"max_execution_time": self.a.statement_timeout_s, **self.reads, **st},
            ).result_rows
        )

    def cmd(self, sql: str, **st: Any) -> None:
        self.ch.command(sql, settings=st or None)

    def q_drained(self, sql: str, tables: list[str], **st: Any) -> list[Any]:
        """The rows of `sql`, which reads `tables`. On a ReplicatedMergeTree the same
        statement reads whether the replica answering it has fetched every part of them,
        and ReplicaBehind is raised when it has not: SYSTEM SYNC REPLICA syncs only the
        replica that answers it, which need not be the one answering this read."""
        if not self.replicated:
            return self.q(sql, **st)
        ((host, synced, rows),) = self.q(
            f"SELECT hostName(), ({REPLICA_SYNCED_SQL}), groupArray(tuple(*)) FROM ({sql})",
            parameters={"tables": tables, "empty": self.empty_logs(tables)},
            **st,
        )
        if not synced:
            raise ReplicaBehind(
                f"a read after the drain ran on {host}, which has not fetched every part of "
                f"{', '.join(f'loom.{t}' for t in tables)}; connect directly to one replica "
                "and rerun"
            )
        # a server that names tuple elements returns each row as a dict
        return [tuple(r.values()) if isinstance(r, dict) else tuple(r) for r in rows]

    def empty_logs(self, tables: list[str]) -> list[str]:
        """The tables in `tables` whose replication log in Keeper holds no entry. Keeper
        is shared, so any replica's answer holds for the one that reads next."""
        paths = self.q(
            "SELECT table, zookeeper_path FROM system.replicas "
            "WHERE database = currentDatabase() AND has({tables:Array(String)}, table)",
            parameters={"tables": tables},
        )
        try:
            self.q("SELECT count() FROM system.replication_queue WHERE 0")
        except tr.Interrupted:
            raise
        except Exception as e:  # an unreadable queue refuses, never passes
            raise ReplicaBehind(
                f"could not read system.replication_queue ({str(e)[:160]}); the run needs "
                "SELECT on system.replication_queue"
            ) from e
        empty = []
        for table, path in paths:
            try:
                entries = self.q(
                    "SELECT count() FROM system.zookeeper WHERE path = {p:String}",
                    parameters={"p": path.rstrip("/") + "/log"},
                )[0][0]
            except tr.Interrupted:
                raise
            except Exception as e:  # an unreadable log refuses, never passes
                raise ReplicaBehind(
                    f"could not read the replication log of loom.{table} in system.zookeeper "
                    f"({str(e)[:160]}); the run needs SELECT on system.zookeeper"
                ) from e
            if not entries:
                empty.append(table)
        return empty

    @cached_property
    def codec(self) -> CodecSettings:
        """How this server takes the codec settings, read once before any is sent."""
        return CodecSettings.from_rows(self.q(CODEC_SETTINGS_SQL))

    def codec_on(self) -> dict[str, int]:
        """`enable_quantized_codec = 1` where this user may set it; held at 1, it is not sent."""
        return {CODEC_SETTING: 1} if self.codec.settable(CODEC_SETTING) else {}

    def codec_refusal(self) -> str:
        """Why this server cannot take the twin's codec, or ''."""
        if not self.codec.codec:
            return (
                f"the Quantized codec cannot be on: {CODEC_SETTING} is absent (before 26.8) "
                "or held at 0 for this user"
            )
        if self.chain and not self.codec.chain:
            return (
                f"--chain {self.chain} needs {CHAIN_SETTING}, held at 0 for this user: "
                "rebuild without --chain"
            )
        return ""

    def ddl(self, name: str) -> str:
        return unqualify(str(self.q(f"SHOW CREATE TABLE loom.{name}")[0][0]))

    def exists(self, name: str) -> bool:
        return tr.table_row(self.ch, name) is not None

    def target_state(self, name: str) -> bool | None:
        """None when `loom.<name>` is absent, else whether it is on the target layout."""
        if not self.exists(name):
            return None
        return on_target(
            self.ddl(name),
            self.cols,
            self.chain,
            self.a.method,
            self.dim or self.find_dim(),
            self.a.index_granularity,
        )

    def find_dim(self) -> int:
        """The width the twin codes. A coded column states its own. A plain column takes
        LOOM_EMBEDDING_DIM, which must equal the served model's width (writers use the
        width the server probes at boot): a coded column rejects any other width, and the
        width refusal stops the run while the column holds one."""
        from server.settings import settings

        ddl = self.ddl(self.t if self.exists(self.t) else self.pre)
        dims = {vector_dim(ddl, c) or int(settings.embedding_dim) for c in self.cols}
        if len(dims) != 1:
            raise SystemExit(f"the named columns disagree on the dimension: {sorted(dims)}")
        return dims.pop()

    def aborted(self) -> bool:
        return self.abort.exists()

    @property
    def run_line(self) -> str:
        """The command line that runs this tool on this table with this run's layout,
        gate, cluster and timeout flags, up to its action flags."""
        line = (
            f"python -P -m scripts.ops.vector_codec_rebuild --table {self.t} "
            f"--columns {','.join(self.cols)} --method {self.a.method}"
        )
        if self.chain:
            line += f" --chain {shlex.quote(self.chain)}"
        if self.a.index_granularity:
            line += f" --index-granularity {self.a.index_granularity}"
        if self.a.gate_fetch_multiplier != DEFAULT_GATE_FETCH_MULTIPLIER:
            line += f" --gate-fetch-multiplier {self.a.gate_fetch_multiplier!r}"
        if self.a.probes != DEFAULT_PROBES:
            line += f" --probes {self.a.probes}"
        if self.a.require_patch_free:
            line += " --require-patch-free"
        if self.a.cluster != DEFAULT_CLUSTER:
            line += f" --cluster {shlex.quote(self.a.cluster)}"
        if self.a.statement_timeout_s != DEFAULT_STATEMENT_TIMEOUT_S:
            line += f" --statement-timeout-s {self.a.statement_timeout_s!r}"
        if self.a.abort_file != default_abort_file():
            # the twin record lives beside the abort file
            line += f" --abort-file {shlex.quote(self.a.abort_file)}"
        return line

    def twin_after_exchange(self) -> bool | None:
        """Whether the twin may be the old table an exchange without its rename left,
        read from every vector column of the two tables rather than this run's flags (a
        rerun may name another layout or other columns). None when it is not: absent,
        never exchanged in, or under a live table with no coded vector column.
        Else whether a column coded on the live table is coded on the twin too: False
        means the twin is the old table, True means either table may be the old one."""
        if not (self.exists(self.t) and self.exists(self.twin)):
            return None
        if self.twin_never_exchanged():
            return None
        live = self.ddl(self.t)
        coded = [c for c in vector_columns(live) if vector_dim(live, c) is not None]
        if not coded:
            return None
        twin = self.ddl(self.twin)
        return any(vector_dim(twin, c) is not None for c in coded)

    def twin_of_another_layout(self) -> str:
        """Why the twin may be the old table `scripts/ops/table_rebuild.py` exchanged out
        without its rename, or ''. That tool shares the twin's name and changes only the
        sorting or partition key, so a twin keyed otherwise than the live table, not known
        to be this tool's leftover, may be the only copy of the old layout: nothing here
        drops or reuses it."""
        if not (self.exists(self.t) and self.exists(self.twin)) or self.twin_never_exchanged():
            return ""
        if tr.layout_of(self.ddl(self.twin)) == tr.layout_of(self.ddl(self.t)):
            return ""
        return (
            f"loom.{self.twin} has another sorting or partition key than loom.{self.t}, so it "
            f"may be the old table scripts/ops/table_rebuild.py exchanged out: drop nothing "
            f"and run `python -P -m scripts.ops.table_rebuild --table {self.t} --dry-run`, "
            f"whose report names the rename"
        )

    def twin_never_exchanged(self) -> bool:
        """Whether the twin was never exchanged in. Its UUID and the live table's are the
        ones this tool recorded when it created the twin: any exchange gives the live
        table's name another UUID, including a --reuse swap run from another directory,
        which leaves this record behind. Or its metadata time is later than the live
        table's: EXCHANGE TABLES keeps each table's own time on the Atomic and Replicated
        databases the live tests run it on, and the swap refuses a live table altered
        after its twin was created, so an old table left there is never the newer one.
        Without the record, on any other database
        engine and on equal times (one second's precision), neither shows anything."""
        twin, live = tr.table_row(self.ch, self.twin), tr.table_row(self.ch, self.t)
        if (
            twin is not None
            and live is not None
            and twin[1] not in ("", ZERO_UUID)
            and [twin[1], live[1]] == self.created_twin()
        ):
            return True
        if not self.exchange_keeps_times():
            return False
        return self.metadata_time(self.twin) > self.metadata_time(self.t)

    def exchange_keeps_times(self) -> bool:
        """Whether EXCHANGE TABLES keeps each table's own metadata time: on the Atomic and
        Replicated databases the live tests run it on; unknown on any other engine."""
        engine = self.q("SELECT engine FROM system.databases WHERE name = currentDatabase()")[0][0]
        return engine in ("Atomic", "Replicated")

    def created_twin(self) -> list[str]:
        """The UUIDs of the last twin this tool created for the table and of the live
        table then, from the record beside the abort file, or []."""
        try:
            return self.twin_record.read_text().split()
        except OSError:
            return []

    def report_state(self) -> str:
        if self.next_step:
            return self.next_step
        if why := self.twin_of_another_layout():
            return why
        if not self.exists(self.pre) and self.exists(self.t) and self.exists(self.twin):
            twin = self.twin_after_exchange()
            if twin is not None:
                return tr.guidance(self.t, self.run_line, True, twin, None)
            if self.twin_never_exchanged():
                # a twin never exchanged in is not the old table, whatever layout this
                # run names
                return (
                    f"not swapped; loom.{self.twin} is a leftover never exchanged in: "
                    f"`{self.run_line} --restart --yes` copies again, or "
                    f"DROP TABLE loom.{self.twin} SYNC"
                )
        return tr.guidance(
            self.t,
            self.run_line,
            self.target_state(self.t),
            self.target_state(self.twin),
            self.target_state(self.pre),
        )

    # -- preconditions ---------------------------------------------------------
    def refusal(self, ddl: str) -> str:
        """Why the rebuild cannot start, or ''."""
        vers = {
            r[1]
            for r in self.q(
                f"SELECT hostName(), version() FROM {self.replicas('one')}",
                skip_unavailable_shards=1,
            )
        }
        log("replica versions", vers)
        if len(vers) != 1 or tuple(int(x) for x in next(iter(vers)).split(".")[:2]) < (26, 7):
            return f"every replica must run one ClickHouse 26.7+; found {sorted(vers)}"
        if why := self.codec_refusal():
            return why
        ro = self.q(
            f"SELECT hostName(), is_readonly FROM {self.replicas('replicas')} "
            f"WHERE database = '{database()}' AND table='{self.t}'",
            skip_unavailable_shards=1,
        )
        if any(r[1] for r in ro):
            return f"a replica is read-only: {ro}"
        if self.exists(self.pre):
            return f"loom.{self.pre} exists: roll back with --rollback, or drop it with --drop-pre-rebuild"
        time_col = "created_at" if "`created_at`" in ddl else ("ts" if "`ts`" in ddl else None)
        if time_col:
            empty = " + ".join(f"countIf(length({c}) = 0)" for c in self.cols)
            n = self.q(
                f"SELECT {empty} FROM loom.{self.t} WHERE {time_col} > now() - INTERVAL 1 HOUR"
            )[0][0]
            log("rows written in the last hour with an empty vector:", n)
            if n:
                return "recent rows carry an empty vector: add this table to placeholders.PLACEHOLDER_COLUMNS and deploy first"
        twin = tr.table_row(self.ch, self.twin)
        if why := self.twin_of_another_layout():
            return why
        if twin is not None and not (self.a.reuse or self.a.restart):
            return (
                f"loom.{self.twin} (created {twin[2]}, {tr.row_count(self.ch, self.twin)} rows) is "
                "another run's lock or its leftover: --reuse continues from it, --restart drops it"
            )
        reused = twin is not None and self.a.reuse and tr.row_count(self.ch, self.twin) > 0
        if reused and not self.reusable(ddl):
            return (
                f"loom.{self.twin} does not have the layout this run builds (another --method, "
                "--index-granularity or --chain, or another tool's twin), and the gates "
                "would not see it: --restart --yes drops it and copies again"
            )
        if why := self.mutation_refusal(reused):
            return why
        if why := self.drain_refusal(parse_engine(ddl)[0]):
            return why
        try:
            return self.width_refusal(ddl)
        except ReplicaBehind as e:
            return str(e)

    def reusable(self, ddl: str) -> bool:
        """Whether the twin has the named columns, granularity and sorting key the twin
        this run would create from the live table's `ddl` has. The gates read the stored
        vectors and the codes, so neither sees a twin of another layout."""
        twin = self.ddl(self.twin)
        granularity = self.a.index_granularity or index_granularity(ddl)
        return (
            on_target(twin, self.cols, self.chain, self.a.method, self.dim, granularity)
            and parse_engine(twin)[1:] == parse_engine(ddl)[1:]
        )

    def mutation_refusal(self, reused: bool) -> str:
        """A stopped writer can leave mutations running on ClickHouse. The gate before the
        swap counts every mutation since this run's start, or since the reused twin was
        created, less the clock-skew margin, so the preflight refuses on the same rule
        rather than after the copy."""
        since = self.metadata_time(self.twin) if reused else self.since
        if not self.mutations_since(self.t, since):
            return ""
        if reused:
            return (
                f"loom.{self.t} has a mutation since the reused twin was created, which makes "
                "the twin stale; rerun with --restart --yes to copy again"
            )
        return (
            f"loom.{self.t} has a mutation still running or one created under a minute "
            "ago (the clock-skew margin); wait for it to finish and rerun in a minute"
        )

    def drain_refusal(self, engine: str) -> str:
        """Why this user cannot run the pre-swap drain, or '': it runs once here, so a
        missing grant refuses before the copy rather than failing after the gates."""
        try:
            self.drain(engine)
        except tr.Interrupted:
            raise
        except Exception as e:  # noqa: BLE001 - any refusal of the drain refuses the run
            return (
                f"the pre-swap drain failed ({str(e)[:160]}); one cause is a missing grant: "
                f"the run needs SYSTEM FLUSH ASYNC INSERT QUEUE and SYSTEM SYNC REPLICA on "
                f"loom.{self.t} (on a replicated engine SYSTEM SYNC REPLICA on "
                f"loom.{self.pre} too, which only the checks after the swap can run)"
            )
        return ""

    def server_now(self) -> int:
        """The server's Unix time."""
        return int(self.q("SELECT toUnixTimestamp(now())")[0][0])

    def metadata_time(self, name: str) -> int:
        """The server's Unix time when `loom.<name>` was created (for the twin, before and
        after the swap); source mutations since then count against it."""
        sql = (
            "SELECT toUnixTimestamp(metadata_modification_time) FROM system.tables "
            "WHERE database = currentDatabase() AND name = {t:String}"
        )
        return int(self.q(sql, parameters={"t": name})[0][0])

    def mutations_since(self, name: str, since: int) -> int:
        """Count the unfinished mutations of `loom.<name>` and every one created since
        `since`, less the clock-skew margin."""
        sql = mutations_sql(self.replicas("mutations"), since - CLOCK_SKEW_MARGIN_S)
        return int(self.q(sql, parameters={"db": database(), "t": name})[0][0])

    def width_refusal(self, ddl: str, table: str | None = None) -> str:
        """Why a named column of `table` (the live table by default) holds vectors the copy
        would zero, or ''."""
        from server.memory.app.reembed import REEMBED_TABLES
        from server.settings import settings

        # a coded column's codec rejects any other width, so only plain columns are scanned
        table = table or self.t
        for c in [c for c in self.cols if vector_dim(ddl, c) is None]:
            widths = self.q_drained(mixed_width_sql(table, c, self.fin, dim=self.dim), [table])
            if widths:
                found = ", ".join(f"width {w}: {n} row(s)" for w, n in widths)
                if self.t in REEMBED_TABLES:
                    how = (
                        "`loom reembed --delete-old` re-embeds only rows of another "
                        "embedding_model; a width change with the same model goes through "
                        "Settings -> Models & keys"
                    )
                else:
                    how = f"`loom reembed` does not cover loom.{self.t}; its own writer must"
                # LOOM_EMBEDDING_DIM may be stale: the server writes at the width it probes
                # from the model at boot and only logs the mismatch
                fix = ""
                if len(widths) == 1:
                    w = widths[0][0]
                    fix = (
                        f"if the served model emits width {w} (the server logs an embedding "
                        f"dim mismatch at boot), set LOOM_EMBEDDING_DIM to {w} and rerun; else "
                    )
                return (
                    f"{c} holds vectors of a width other than the target {self.dim} ({found}; "
                    f"LOOM_EMBEDDING_DIM {settings.embedding_dim}, which must equal the served "
                    f"model's width): the copy would zero them; {fix}"
                    f"re-embed to LOOM_EMBEDDING_DIM ({how}) and rerun"
                )
        return ""

    # -- the run ------------------------------------------------------------
    def run(self) -> int:
        tr.print_target(self.ch, self.t)
        if not self.exists(self.t):
            log(f"REFUSED: loom.{self.t} does not exist")
            return REFUSED
        ddl = self.ddl(self.t)
        self.dim = self.find_dim()
        engine = parse_engine(ddl)[0]
        self.fin = read_final(engine)
        self.replicated = engine.startswith("Replicated")
        log(
            "target",
            self.t,
            "dimension",
            self.dim,
            "method",
            codec_method(self.a.method),
        )
        twin_ddl = rewrite_ddl(
            ddl,
            f"loom.{self.t}",
            f"loom.{self.twin}",
            self.cols,
            self.chain,
            self.a.method,
            self.a.index_granularity,
            dim=self.dim,
        )
        if self.twin_after_exchange() is not None:
            # Any ALTER of the live table also moves its metadata time past the twin's, so
            # the time alone does not show an exchange; the codecs do. An older twin under
            # a coded live table may be the old table, the only rollback copy, whatever
            # layout or columns this run asks for, and also when the live table already has
            # the layout this run names, as after the same run's exchange: the report names
            # the rename (a plain twin) or the comparison (two coded tables). Under a live
            # table with no coded vector column the twin is a leftover.
            log("REFUSED:", self.report_state())
            return REFUSED
        if (
            on_target(
                ddl,
                self.cols,
                self.chain,
                self.a.method,
                self.dim,
                self.a.index_granularity,
            )
            and not self.a.require_patch_free
        ):
            log("already on the target layout; nothing to do")
            return OK
        if self.aborted():
            raise Aborted(f"abort file {self.abort} present before start")
        if self.a.require_patch_free:
            log("live patch parts before:", self.q(patch_parts_sql(self.t))[0][0])
        # read before the preflight's mutation check, so no mutation falls between the two checks
        self.since = self.server_now()
        why = self.refusal(ddl)
        if why:
            log("REFUSED:", why)
            return REFUSED
        if self.a.dry_run:
            log("dry run: twin DDL follows; nothing changed")
            print(twin_ddl)
            return OK
        if not self.a.yes:
            log(f"plan only: pass --yes to copy loom.{self.t} into loom.{self.twin} and swap")
            print(twin_ddl)
            return UNCONFIRMED
        try:
            return self.copy_and_swap(ddl, twin_ddl)
        except ReplicaBehind as e:
            log(f"GATE FAILED: {e}; no swap")
            return GATE_FAILED

    def create_twin(self, twin_ddl: str) -> tuple[str, bool]:
        """Create (or reuse) the twin: (why not, or '', whether it was reused with rows)."""
        row = tr.table_row(self.ch, self.twin)
        if row is not None:
            n = tr.row_count(self.ch, self.twin)
            if self.a.reuse and n:
                self.twin_uuid = row[1]
                log("reusing twin with", n, "rows")
                return "", True
            self.cmd(f"DROP TABLE loom.{self.twin} SYNC")
            log(f"dropped loom.{self.twin} of an earlier run")
        try:
            self.cmd(twin_ddl, **statement_settings(twin_ddl, self.codec))
        except Exception as e:  # noqa: BLE001 - a lost CREATE race is a refusal, anything else too
            return f"could not create loom.{self.twin} (another run?): {str(e)[:160]}", False
        row = tr.table_row(self.ch, self.twin)
        self.twin_uuid = row[1] if row else ""
        live = tr.table_row(self.ch, self.t)
        try:
            self.twin_record.write_text(f"{self.twin_uuid} {live[1] if live else ''}")
        except OSError as e:
            log(f"could not record the twin's UUID in {self.twin_record}:", e)
        log(
            "twin created:",
            self.q(
                f"SELECT name, type, compression_codec FROM system.columns WHERE database = currentDatabase() AND table='{self.twin}' AND name IN ({', '.join(repr(x) for x in self.cols)})"
            ),
        )
        return "", False

    def still_ours(self) -> bool:
        row = tr.table_row(self.ch, self.twin)
        return row is not None and row[1] == self.twin_uuid

    def delta_into(
        self, dst: str, src: str, sel: str, version: str | None, key: list[str], limit_s: float
    ) -> int:
        """Copy the rows of `src` that `dst` lacks (newer versions, or absent keys); the
        reads that find them and the INSERT each run under `limit_s` seconds."""
        if version:
            above = self.q(f"SELECT max({version}) FROM loom.{dst}", max_execution_time=limit_s)[0][
                0
            ]
            if above is None:
                return 0
            cond = f"{version} > {sql_literal(above)}"
        else:
            ident = ", ".join(key)
            cond = f"({ident}) NOT IN (SELECT {ident} FROM loom.{dst})"
        n = int(
            self.q(f"SELECT count() FROM loom.{src} WHERE {cond}", max_execution_time=limit_s)[0][0]
        )
        if n:
            self.cmd(
                f"INSERT INTO loom.{dst} {sel} FROM loom.{src} WHERE {cond}",
                **self.codec_on(),
                **self.reads,
                max_execution_time=limit_s,
            )
            self.rows_copied += n
        return n

    def repair_into(self, sel: str, version: str | None, key: list[str]) -> int:
        """Copy the live table's rows for every key the twin lacks or holds at an older
        version; a key that differs only by content or row count is left for the gate to
        fail. `delta_into` takes the rows above the twin's highest version, which misses a row
        written during a long copy with a version below a row the copy already took. The
        keys come back to the client and each INSERT names them as literals: on a shared
        cluster an `IN (subquery)` INSERT runs the subquery on other replicas, which a
        user without cluster-wide grants is refused."""
        ident, fin = ", ".join(key), self.fin
        vsel = f", {version}" if version else ""
        newer = f"s.{version} > t.{version} OR t.__present = 0" if version else "t.__present = 0"
        diverged = (
            f"SELECT {ident} FROM (SELECT {ident}{vsel} FROM loom.{self.t}{fin}) AS s "
            f"LEFT JOIN (SELECT {ident}{vsel}, 1 AS __present FROM loom.{self.twin}{fin}) AS t "
            f"USING ({ident}) WHERE {newer}"
        )
        keys = self.q(diverged, join_use_nulls=0)
        for i in range(0, len(keys), REPAIR_BATCH):
            batch = keys[i : i + REPAIR_BATCH]
            lits = ", ".join("(" + ", ".join(key_literal(v) for v in k) + ")" for k in batch)
            self.cmd(
                f"INSERT INTO loom.{self.twin} {sel} FROM loom.{self.t} WHERE ({ident}) IN ({lits})",
                **self.codec_on(),
                max_execution_time=self.a.statement_timeout_s,
            )
        self.rows_copied += len(keys)
        return len(keys)

    def monitor(self, stop: threading.Event) -> None:
        mc = self.client()
        while not stop.wait(60):
            try:
                rep = mc.query(
                    f"SELECT hostName(), is_readonly, is_session_expired FROM {self.replicas('replicas')} "
                    f"WHERE database = '{database()}' AND table IN ('{self.t}','{self.twin}')",
                    settings={"skip_unavailable_shards": 1},
                ).result_rows
                prog = mc.query(
                    f"SELECT count(), formatReadableSize(sum(bytes_on_disk)) FROM system.parts WHERE database = currentDatabase() AND table='{self.twin}' AND active"
                ).result_rows[0]
                log("monitor replicas", rep, "twin parts/bytes", prog)
                if any(r[1] == 1 for r in rep):
                    log("!!! a replica is READONLY")
            except Exception as e:  # noqa: BLE001 - the monitor only logs; the copy decides
                log("monitor err", str(e)[:120])

    def consistent(
        self, version: str | None, key: list[str], source: str = "", copy: str = ""
    ) -> tuple[int, int, int]:
        """(mismatched, source_only, copy_only) keys of `source` (the live table by default)
        against `copy` (the twin by default), whose vectors are the rebuilt ones; the
        source's vectors are hashed as the copy writes them."""
        source, copy = source or self.t, copy or self.twin
        hashed = [c for c in tr.insertable_columns(self.ch, self.t) if c not in self.cols]
        sql = consistency_sql(source, copy, self.fin, version, key, hashed, self.cols, dim=self.dim)
        row = self.q_drained(sql, [source, copy], join_use_nulls=0)[0]
        return int(row[0]), int(row[1]), int(row[2])

    def twin_consistent(self, version: str | None, key: list[str]) -> tuple[int, int, int]:
        """`consistent` for the live table and the twin. On ReplicatedMergeTree the twin is
        synced first: the copy, a delta or a repair writes log entries this replica may
        not have pulled yet, which the read would take for a replica behind."""
        if self.replicated:
            try:
                self.cmd(f"SYSTEM SYNC REPLICA loom.{self.twin} LIGHTWEIGHT")
            except tr.Interrupted:
                raise
            except Exception as e:  # a failed twin sync fails the gate, not the run
                raise ReplicaBehind(
                    f"SYSTEM SYNC REPLICA loom.{self.twin} LIGHTWEIGHT failed "
                    f"({str(e)[:160]}); one cause is a missing grant on the twin"
                ) from e
        return self.consistent(version, key)

    def gate_failure(
        self, cons: tuple[int, int, int], version: str | None, key: list[str], why: str
    ) -> str:
        """The consistency gate's message, or '' when the twin matches: keys only the twin
        holds first, then keys that differ (`why`, unless the source holds keys with more
        than one row at their newest version, which no copy can match)."""
        mismatched, source_only, twin_only = cons
        if twin_only:
            return twin_only_failure(twin_only)
        if not (mismatched or source_only):
            return ""
        if version and (n := int(self.q(tied_keys_sql(self.t, version, key))[0][0])):
            return tied_failure(self.t, n)
        return why

    def drain(self, engine: str) -> None:
        """Flush buffered inserts and synchronize this replica's parts.

        SYSTEM SYNC REPLICA syncs only the replica that answers it. On ReplicatedMergeTree
        each later consistency and width read checks that its own replica has fetched
        every part (q_drained); SharedMergeTree reads use sequential consistency when a
        different replica serves a later request. LIGHTWEIGHT on ReplicatedMergeTree: it
        waits for the fetch, attach and drop-range entries only, the ones the reads
        require, and not for a long merge that merges on the live table keep queuing.
        """
        self.cmd(f"SYSTEM FLUSH ASYNC INSERT QUEUE loom.{self.t}")
        if engine.startswith("Replicated"):
            self.cmd(f"SYSTEM SYNC REPLICA loom.{self.t} LIGHTWEIGHT")
        elif engine.startswith("Shared"):
            self.cmd(f"SYSTEM SYNC REPLICA loom.{self.t}")

    def sync_rollback_copy(self, engine: str) -> None:
        """Synchronize this replica's parts of the rollback copy before a read compares it
        (the read checks that its own replica fetched them). The copy has its own
        replication queue: a write that reached another replica before the swap belongs to
        it after the rename. LIGHTWEIGHT, because swap_in stops the copy's merges and a
        full sync would wait on a merge entry. SharedMergeTree reads it with sequential
        consistency instead."""
        if engine.startswith("Replicated"):
            self.cmd(f"SYSTEM SYNC REPLICA loom.{self.pre} LIGHTWEIGHT")

    def recall(self, ddl: str) -> list[float]:
        """Distance recall per probe: exact on the source vs the twin read as the legs read it."""
        vcol, dim, fin = self.cols[0], self.dim, self.fin
        # a fixed-length placeholder is all zeros: its cosine is NaN, never a probe; the
        # probes are the served rows, as the reads below see them
        real = vector_filter(vcol, dim=dim)
        probes = self.q(
            f"SELECT org, namespace, {vcol} FROM loom.{self.t}{fin} WHERE {real} ORDER BY rand() LIMIT {self.a.probes}"
        )
        src_v = exact_vector_expr(ddl, vcol)
        tol = DISTANCE_TOLERANCE
        drec: list[float] = []
        for org, ns, v in probes:
            lit = "[" + ",".join(f"{float(x):.6g}" for x in v) + "]"
            scope = f"org='{org}' AND namespace='{ns}'"
            ex = self.q(
                f"SELECT round(cosineDistance({src_v}, {lit}), 6) AS d FROM loom.{self.t}{fin} "
                f"WHERE {scope} AND {real} ORDER BY d LIMIT 20"
            )
            dist, st = twin_read(vcol, lit, self.a.gate_fetch_multiplier)
            tw = self.q(
                f"SELECT round({dist}, 6) FROM loom.{self.twin}{fin} "
                f"WHERE {scope} AND {real} ORDER BY {dist} LIMIT 20",
                **st,
            )
            d20 = ex[-1][0] if ex else 0.0
            # the denominator is the real neighbours the exact read found, at most 20
            drec.append(sum(1 for r in tw if r[0] <= d20 + tol) / max(1, len(ex)))
        return drec

    def copy_and_swap(self, ddl: str, twin_ddl: str) -> int:
        why, reused = self.create_twin(twin_ddl)
        if why:
            log("REFUSED:", why)
            return REFUSED
        engine, version, key = parse_engine(ddl)
        log("engine", engine, "version column", version, "sorting key", key)
        fin = self.fin
        sel = replace_select(self.cols, dim=self.dim)
        if self.aborted():
            raise Aborted("abort file present before the copy; the twin is left")
        since = self.metadata_time(self.twin) if reused else self.since
        if not reused:
            stop = threading.Event()
            threading.Thread(target=self.monitor, args=(stop,), daemon=True).start()
            t0 = time.time()
            try:
                self.cmd(
                    f"INSERT INTO loom.{self.twin} {sel} FROM loom.{self.t}",
                    **self.codec_on(),
                    max_insert_threads=4,
                    max_execution_time=COPY_EXECUTION_TIME_S,
                    max_threads=8,
                )
            finally:
                stop.set()
            self.rows_copied += tr.row_count(self.ch, self.t)
            log(f"copy done in {time.time() - t0:.0f}s")
        # Compare under FINAL (the twin's merges already collapsed duplicate versions)
        # and let the count settle: a replica can lag a fresh table's part metadata.
        src_final = int(self.q(f"SELECT count() FROM loom.{self.t}{fin}")[0][0])
        twin_n = 0
        for attempt in range(COUNT_SETTLE_ATTEMPTS):
            twin_n = int(self.q(f"SELECT count() FROM loom.{self.twin}{fin}")[0][0])
            log(f"rows{fin or ''} — twin {twin_n} source {src_final}, attempt {attempt + 1}")
            if reused or rows_settled(twin_n, src_final):
                break
            time.sleep(COUNT_SETTLE_SLEEP_S)
        if not rows_settled(twin_n, src_final):
            log("GATE FAILED: the twin is short of rows")
            return GATE_FAILED
        limit = self.a.statement_timeout_s
        log("delta-1 rows into twin:", self.delta_into(self.twin, self.t, sel, version, key, limit))
        cons = self.twin_consistent(version, key)
        log("consistency (mismatched, source_only, twin_only):", cons)
        if cons[0] or cons[1]:
            log("repair keys:", self.repair_into(sel, version, key))
            cons = self.twin_consistent(version, key)
            log("consistency after repair:", cons)
        diverged = (
            "GATE FAILED: the twin diverges (a key's version, row count or content); no "
            "swap. Stop writes and erasures and rerun with --restart"
        )
        if failure := self.gate_failure(cons, version, key, diverged):
            log(failure)
            return GATE_FAILED
        drec = self.recall(ddl)
        if drec:
            log(
                f"recall gate: distance recall mean {statistics.mean(drec):.3f} min {min(drec):.3f}"
            )
            if not (min(drec) >= 0.95 and statistics.mean(drec) >= 0.99):
                log("GATE FAILED: recall; the twin is left for inspection")
                return GATE_FAILED
        elif tr.row_count(self.ch, self.t):
            # a rerun fails the same way, so the table keeps its layout until it holds embeddings
            self.next_step = (
                f"not swapped; loom.{self.t} holds no real vectors to probe, so keep its layout: "
                f"run DROP TABLE loom.{self.twin} SYNC and skip its line until it holds embeddings"
            )
            log(f"GATE FAILED: no probe rows with a full-length vector; {self.next_step}")
            return GATE_FAILED
        else:
            # the consistency gate found the twin as empty: there is nothing to probe
            log(f"recall gate: loom.{self.t} holds no rows, nothing to probe")
        if self.a.require_patch_free:
            patches = self.q(patch_parts_sql(self.twin))[0][0]
            log("twin patch parts:", patches)
            if patches:
                log("GATE FAILED: a lightweight UPDATE landed on the twin during the copy; no swap")
                return GATE_FAILED
        if self.aborted():
            log("abort file present; no swap")
            return ABORTED
        if not self.still_ours():
            log(f"REFUSED: loom.{self.twin} is no longer the twin this run created; no swap")
            return REFUSED
        # the flush in swap_in then has nothing left to add after the checks below
        self.drain(engine)
        self.reads = dict(tr.CONSISTENT)
        self.delta_into(self.twin, self.t, sel, version, key, limit)
        # writers and erasure must be stopped; check again that nothing landed during the gates
        if why := self.width_refusal(ddl):
            log(f"GATE FAILED: {why}; no swap")
            return GATE_FAILED
        cons = self.twin_consistent(version, key)
        log("consistency before the swap:", cons)
        changed = (
            "GATE FAILED: the source changed during the gates (a write, an update or a "
            "delete), and the twin no longer matches it; no swap. Stop writes and erasures "
            "and rerun with --restart"
        )
        if failure := self.gate_failure(cons, version, key, changed):
            log(failure)
            return GATE_FAILED
        if n := self.mutations_since(self.t, since):
            log(
                f"GATE FAILED: {n} mutation(s) on loom.{self.t} since the copy started or still "
                "running (an ALTER UPDATE such as an erasure tombstone or a quarantine, or a "
                "delete), which the twin may not hold; no swap. Stop writes and erasures and "
                "rerun with --restart"
            )
            return GATE_FAILED
        # twin_never_exchanged reads a twin newer than the live table as one never
        # exchanged in; an ALTER of the live table after the twin was created would make
        # the old table the newer one once a half swap leaves it at the twin's name
        if self.metadata_time(self.t) > self.metadata_time(self.twin):
            log(
                f"REFUSED: loom.{self.t} was altered after loom.{self.twin} was created "
                "(an ALTER such as ADD INDEX or MODIFY SETTING, which the twin does not "
                "hold); no swap. Rerun with --restart"
            )
            return REFUSED
        # the record names only a twin never exchanged in: once this twin is the live
        # table, a later twin under its name may be the old table
        try:
            self.twin_record.unlink()
        except FileNotFoundError:
            pass
        except OSError as e:
            log(f"REFUSED: could not remove the twin's record {self.twin_record}: {e}; no swap")
            return REFUSED
        tr.swap_in(self.ch, self.t)
        return self.check_swapped(engine, version, key, since)

    def check_swapped(self, engine: str, version: str | None, key: list[str], since: int) -> int:
        live = self.target_state(self.t)
        log(
            "live rows",
            tr.row_count(self.ch, self.t),
            "on target",
            live,
            "columns",
            self.q(
                f"SELECT name, type, compression_codec FROM system.columns WHERE database = currentDatabase() AND table='{self.t}' AND name IN ({', '.join(repr(x) for x in self.cols)})"
            ),
        )
        if not live or not self.exists(self.pre) or self.exists(self.twin):
            log("POSTCONDITION FAILED: the names do not hold what the swap leaves")
            return POSTCONDITION
        if self.columns_differ():
            self.next_step = f"loom.{self.t} and loom.{self.pre} differ. {KEEP_BOTH}"
            log("POSTCONDITION FAILED: the rollback copy's columns differ by name")
            return POSTCONDITION
        if self.a.require_patch_free:
            log("live patch parts after:", self.q(patch_parts_sql(self.t))[0][0])
        try:
            why, rollback = self.rollback_copy_change(engine, version, key, since)
        except tr.Interrupted:
            raise
        except Exception as e:  # noqa: BLE001 - checks that cannot run keep the copy
            self.next_step = self.recheck_step()
            log(
                f"POSTCONDITION FAILED: the checks could not run ({str(e)[:160]}); "
                f"loom.{self.pre} is kept"
            )
            return POSTCONDITION
        if why:
            self.next_step = self.keep_copy(rollback)
            log(f"POSTCONDITION FAILED: {why}. {self.next_step}")
            return POSTCONDITION
        self.next_step = (
            f"keep writers and erasure stopped; run `{self.run_line} --drop-pre-rebuild "
            "--yes`, which checks the two tables again before it drops the rollback copy"
        )
        return OK

    def columns_differ(self) -> bool:
        """Whether the live table and the rollback copy name other columns. Either table
        may have changed, so neither --rollback nor the drop keeps it whole."""
        return set(tr.columns(self.ch, self.t)) != set(tr.columns(self.ch, self.pre))

    def recheck_step(self, action: str = "--drop-pre-rebuild") -> str:
        """The next step when the checks before `action` could not run."""
        return (
            "the checks could not run: fix the grant, connect directly to one replica, "
            f"or raise --statement-timeout-s, and rerun `{self.run_line} {action} "
            f"--yes`; loom.{self.pre} is kept"
        )

    def keep_copy(self, rollback: bool) -> str:
        """The next step when the two tables differ: --rollback when only the rollback copy
        holds the change, else inspection with both kept."""
        if rollback:
            step = KEEP_AND_ROLL_BACK.format(run=self.run_line)
            return f"loom.{self.pre} holds a change the new table lacks. {step}"
        return f"loom.{self.t} and loom.{self.pre} differ. {KEEP_BOTH}"

    def differences(
        self, version: str | None, key: list[str], old: str, new: str
    ) -> tuple[str, str, str]:
        """What the old-layout table `old` holds that the new-layout table `new` lacks, what
        `new` holds that `old` lacks, and the keys both hold at one version with other
        content, each as a phrase or ''. The hash shows only that such a key differs, not
        which table holds the change. Raises when the checks cannot run."""
        mismatched, old_only, new_only = self.consistent(version, key, source=old, copy=new)
        old_newer = new_newer = 0
        if mismatched:
            sql = newer_sql(old, new, self.fin, version, key)
            old_newer, new_newer = (int(n) for n in self.q_drained(sql, [old, new])[0])
        same_version = mismatched - old_newer - new_newer
        log(
            f"loom.{old} against loom.{new} (mismatched, only in the first, only in the second):",
            (mismatched, old_only, new_only),
        )
        old_ahead = new_ahead = same = ""
        if old_only or old_newer:
            old_ahead = f"{old_only} key(s) only in loom.{old}, {old_newer} newer there"
        if new_only or new_newer:
            new_ahead = f"{new_only} key(s) only in loom.{new}, {new_newer} newer there"
        if same_version:
            same = f"{same_version} key(s) differ at one version; either table may hold the change"
        return old_ahead, new_ahead, same

    def rollback_copy_change(
        self, engine: str, version: str | None, key: list[str], since: int
    ) -> tuple[str, bool]:
        """Why the rollback copy and the live table differ, or '', and whether --rollback
        restores the difference. After the swap the rollback copy is the source as writers
        left it: the live table must match it, with no mutation since `since` (the gate saw
        none before the swap). Raises when the checks cannot run."""
        self.sync_rollback_copy(engine)
        old_ahead, new_ahead, same = self.differences(version, key, self.pre, self.t)
        if new_ahead or same:
            why = []
            if new_ahead:
                why.append(
                    f"loom.{self.t} holds a change loom.{self.pre} lacks ({new_ahead}): a "
                    "write to the new table after the swap, or a delete in the old table "
                    "after the last check"
                )
            if same:
                why.append(same)
            return (
                "; ".join(why) + ". --rollback would drop a change the new table holds, "
                "since it copies back only newer versions and absent keys"
            ), False
        # a delete in the new table after the swap also leaves keys only in the copy
        if live_mutations := self.mutations_since(self.t, since):
            return (
                f"{live_mutations} mutation(s) on loom.{self.t} after the copy started "
                f"({old_ahead or 'no key'} differs): a delete or update in the new table, "
                "which --rollback would undo"
            ), False
        mutations = self.mutations_since(self.pre, since)
        log("mutations on the rollback copy:", mutations)
        if old_ahead or mutations:
            why = (
                f"the source changed after the last check ({old_ahead or 'no key'}; "
                f"{mutations} mutation(s) on loom.{self.pre}): a write to the old table "
                "after the last check, or a delete in the new table after the swap, and "
                "the new table may not hold the change"
            )
            if mutations:
                # the swap stopped the copy's merges; the rollback starts them again
                why += (
                    ". A mutation applies after the rollback, so the --drop-pre-rebuild "
                    "that follows shows the keys it touched as differing at one version"
                )
            return why, True
        # the hash zeroes a vector of another width on both sides, so an UPDATE to one
        # after the last width check reads as consistent; the width read shows it
        if why := self.width_refusal(self.ddl(self.pre), self.pre):
            return (
                f"{why}. The change landed after the last check and the new table holds it zeroed"
            ), True
        return "", False

    def rollback(self) -> int:
        tr.print_target(self.ch, self.t)
        if not self.exists(self.pre):
            log(f"REFUSED: no loom.{self.pre} to roll back to")
            return REFUSED
        self.dim = self.find_dim()
        swapped = self.live_is_new()
        if swapped is None:
            self.next_step = f"loom.{self.t} and loom.{self.pre} {SAME_LAYOUT}. {KEEP_BOTH}"
            log(f"REFUSED: {self.next_step}")
            return REFUSED
        if not swapped:
            self.next_step = (
                f"already rolled back, or this line names another layout than its swap's: "
                f"`{self.run_line} --drop-pre-rebuild --yes` checks the two tables before "
                "it drops the new-layout copy"
            )
            log(f"REFUSED: loom.{self.t} is not on the layout this line names; {self.next_step}")
            return REFUSED
        # the copy back takes only newer versions or absent keys, so a change only the
        # new table holds, or a mutation on it, would be lost
        try:
            why, restores = self.swapped_change()
        except tr.Interrupted:
            raise
        except Exception as e:  # noqa: BLE001 - checks that cannot run refuse the rollback
            self.next_step = self.recheck_step("--rollback")
            log(f"REFUSED: the checks could not run ({str(e)[:160]}); {self.next_step}")
            return REFUSED
        if why and not restores:
            self.next_step = self.keep_copy(False)
            log(f"REFUSED: {why}; nothing is exchanged. {self.next_step}")
            return REFUSED
        log(f"rollback: loom.{self.pre} back in as loom.{self.t}, the rows written since copied")
        if not self.a.yes:
            log("plan only: pass --yes to roll back")
            return UNCONFIRMED
        _, version, key = parse_engine(self.ddl(self.pre))
        self.cmd(f"EXCHANGE TABLES loom.{self.t} AND loom.{self.pre}")
        # The old layout takes the new one's vectors as they are (an INSERT casts
        # coded arrays to its plain Array column).
        cols = ", ".join(f"`{c}`" for c in tr.insertable_columns(self.ch, self.t))
        sel = f"({cols}) SELECT {cols}"
        for i in range(2):  # the second pass: writes in flight at the exchange
            if i:
                time.sleep(self.a.settle_s)
            n = self.delta_into(self.t, self.pre, sel, version, key, COPY_EXECUTION_TIME_S)
            log("rows copied back:", n)
        try:
            self.cmd(f"SYSTEM START MERGES loom.{self.t}")
        except Exception as e:  # noqa: BLE001 - merges resume on the next restart anyway
            log("could not start merges:", str(e)[:120])
        if not self.target_state(self.pre):
            log(f"POSTCONDITION FAILED: loom.{self.pre} does not hold the target layout")
            return POSTCONDITION
        log(
            f"ROLLED BACK: loom.{self.t} is the old layout again; loom.{self.pre} holds the new one"
        )
        self.next_step = (
            f"keep writers and erasure stopped; run `{self.run_line} --drop-pre-rebuild "
            "--yes`, which checks the two tables before it drops the new-layout copy"
        )
        return OK

    def live_is_new(self) -> bool | None:
        """Whether the live table is the new layout, the rollback copy the old: after a
        swap and before a rollback. When both are on the target layout (a
        --require-patch-free run rebuilds a table already on it) the run's twin is the
        newer table; None when the metadata times show neither: both created in the same
        second (one second's precision), or a database engine EXCHANGE TABLES may not keep
        them on."""
        live, pre = self.target_state(self.t), self.target_state(self.pre)
        if not (live and pre):
            return bool(live)
        if not self.exchange_keeps_times():
            return None
        live_time, pre_time = self.metadata_time(self.t), self.metadata_time(self.pre)
        if live_time == pre_time:
            return None
        return live_time > pre_time

    def drain_after_swap(self) -> tuple[str, str | None, list[str]]:
        """Drain as the postcondition does and read as it reads: (engine, version column,
        sorting key) of the live table."""
        engine, version, key = parse_engine(self.ddl(self.t))
        self.fin = read_final(engine)
        self.replicated = engine.startswith("Replicated")
        self.drain(engine)
        self.reads = dict(tr.CONSISTENT)
        return engine, version, key

    def swapped_change(self) -> tuple[str, bool]:
        """Why the rollback copy and the live table differ, read as the postcondition reads
        them, or '', and whether --rollback restores the difference. Writers stay stopped
        until the copy is dropped, so the two tables still match. Raises when the checks
        cannot run."""
        engine, version, key = self.drain_after_swap()
        # the live table is the run's twin, created when the copy started
        return self.rollback_copy_change(engine, version, key, self.metadata_time(self.t))

    def rolled_back_change(self) -> str:
        """Why the new-layout copy a rollback left may hold a change the live table lacks,
        or ''. A key both hold at one version with other content is either a same-version
        change made in the new table, which the rollback did not copy back, or a mutation
        on the old table that applied once the rollback started its merges. A mutation on
        the copy counts even when FINAL reads do not show it yet. Raises when the checks
        cannot run."""
        engine, version, key = self.drain_after_swap()
        self.sync_rollback_copy(engine)
        _, new_ahead, same = self.differences(version, key, self.t, self.pre)
        why = []
        if new_ahead:
            why.append(f"loom.{self.pre} holds a change loom.{self.t} lacks ({new_ahead})")
        if same:
            why.append(
                f"{same}: a same-version change in the new-layout copy, or a mutation the "
                f"rollback carried back that has since applied to loom.{self.t}, which "
                "another --rollback would undo"
            )
        if n := self.mutations_since(self.pre, self.metadata_time(self.pre)):
            why.append(
                f"{n} mutation(s) on loom.{self.pre} since the copy, which the rollback "
                "did not carry back"
            )
        return "; ".join(why)

    def drop_pre(self) -> int:
        tr.print_target(self.ch, self.t)
        if not self.exists(self.pre):
            if self.exists(self.twin):
                # an exchange without its rename leaves the old table under the twin's name
                self.next_step = self.report_state()
                log(f"REFUSED: loom.{self.pre} is absent and loom.{self.twin} exists")
                return REFUSED
            log(f"loom.{self.pre} is already gone")
            return OK
        self.dim = self.find_dim()
        if not (self.target_state(self.t) or self.target_state(self.pre)):
            log(f"REFUSED: neither loom.{self.t} nor loom.{self.pre} has the target layout")
            return REFUSED
        # after a swap or a rollback the copy may be the only copy of a change
        try:
            if self.columns_differ():
                self.next_step = f"loom.{self.t} and loom.{self.pre} differ. {KEEP_BOTH}"
                log(f"REFUSED: the rollback copy's columns differ by name. {KEEP_BOTH}")
                return REFUSED
            swapped = self.live_is_new()
            if swapped is None:
                self.next_step = f"loom.{self.t} and loom.{self.pre} {SAME_LAYOUT}. {KEEP_BOTH}"
                log(f"REFUSED: {self.next_step}")
                return REFUSED
            why, rollback = self.swapped_change() if swapped else (self.rolled_back_change(), False)
        except tr.Interrupted:
            raise
        except Exception as e:  # noqa: BLE001 - checks that cannot run keep the copy
            self.next_step = self.recheck_step()
            log(f"REFUSED: the checks could not run ({str(e)[:160]}); {self.next_step}")
            return REFUSED
        if why:
            self.next_step = self.keep_copy(rollback)
            log(f"REFUSED: {why}; loom.{self.pre} is kept. {self.next_step}")
            return REFUSED
        log(f"drop: loom.{self.pre} ({tr.row_count(self.ch, self.pre)} rows); cannot be undone")
        if not self.a.yes:
            log("plan only: pass --yes to drop it")
            return UNCONFIRMED
        self.cmd(f"DROP TABLE loom.{self.pre} SYNC")
        if self.exists(self.pre):
            log(f"POSTCONDITION FAILED: loom.{self.pre} is still there")
            return POSTCONDITION
        log(f"dropped loom.{self.pre}")
        return OK


def default_abort_file() -> str:
    """The abort file in LOOM_REBUILD_DIR, the current directory when it is unset."""
    return str(Path(os.environ.get("LOOM_REBUILD_DIR", ".")) / "ABORT_REBUILD")


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--table", required=True, help="e.g. memory_chunks, in LOOM_CH_DATABASE")
    ap.add_argument("--columns", required=True, help="comma list of vector columns")
    ap.add_argument(
        "--chain", default="", help="general codec after the codes, e.g. LZ4 (repeated vectors)"
    )
    ap.add_argument(
        "--method",
        default="rabitq",
        choices=CODEC_METHODS,
        help="Quantized(...) method for the twin's vector columns (turboquant = two-bit codes)",
    )
    ap.add_argument(
        "--gate-fetch-multiplier",
        type=float,
        default=DEFAULT_GATE_FETCH_MULTIPLIER,
        help="the recall gate reads the twin at the multiplier its leg serves (code_summaries' cards: 10)",
    )
    ap.add_argument(
        "--index-granularity",
        type=int,
        default=0,
        help="the twin's index_granularity (0 = keep the table's; code_summaries takes 256)",
    )
    ap.add_argument("--probes", type=int, default=DEFAULT_PROBES)
    ap.add_argument(
        "--require-patch-free",
        action="store_true",
        help="refuse to EXCHANGE unless the twin has zero patch parts; log the live count before and after",
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "check preconditions and print the twin DDL; runs the async-queue flush and "
            "SYSTEM SYNC REPLICA once to check the grants, and writes nothing else"
        ),
    )
    mode.add_argument("--rollback", action="store_true", help="exchange <t>_pre_rebuild back in")
    mode.add_argument("--drop-pre-rebuild", action="store_true", help="drop the rollback copy")
    ap.add_argument("--yes", action="store_true", help="carry out the change the plan prints")
    twin = ap.add_mutually_exclusive_group()
    twin.add_argument(
        "--reuse", action="store_true", help="continue from an existing twin with rows"
    )
    twin.add_argument("--restart", action="store_true", help="drop a twin left by an earlier run")
    ap.add_argument(
        "--cluster",
        default=DEFAULT_CLUSTER,
        help="cluster whose replicas the preflight and monitor check; '' = this server only",
    )
    ap.add_argument(
        "--settle-s", type=float, default=15.0, help="wait between rollback delta passes"
    )
    ap.add_argument(
        "--statement-timeout-s",
        type=float,
        default=DEFAULT_STATEMENT_TIMEOUT_S,
        help="seconds each read, delta and repair INSERT may run, after the swap included",
    )
    ap.add_argument("--abort-file", default=default_abort_file())
    a = ap.parse_args(argv)
    a.table = a.table.removeprefix("loom.")
    if a.cluster and not _CLUSTER_NAME.fullmatch(a.cluster):
        ap.error(f"--cluster: not a cluster name: {a.cluster!r}")
    if not math.isfinite(a.statement_timeout_s) or a.statement_timeout_s <= 0:
        ap.error("--statement-timeout-s must be finite and above 0")
    if a.probes < 1:
        ap.error("--probes must be at least 1")
    if not math.isfinite(a.gate_fetch_multiplier) or a.gate_fetch_multiplier <= 0:
        ap.error("--gate-fetch-multiplier must be finite and above 0")
    if a.statement_timeout_s >= COPY_RECEIVE_TIMEOUT_S:
        ap.error(
            f"--statement-timeout-s must be below {COPY_RECEIVE_TIMEOUT_S}, the client's read timeout"
        )
    return a


def action_of(a: argparse.Namespace) -> str:
    if a.rollback:
        return "rollback"
    if a.drop_pre_rebuild:
        return "drop-pre-rebuild"
    return "dry-run" if a.dry_run else "rebuild"


def main(argv: list[str] | None = None, ch: Any = None) -> int:
    a = parse(argv)
    client = None
    if ch is None:
        from server.core.schema import migrate

        def client() -> Any:
            return migrate.client(receive_timeout_s=COPY_RECEIVE_TIMEOUT_S)

        ch = client()
    job = VectorRebuild(ch, a, client)
    action = action_of(a)
    body = {"rollback": job.rollback, "drop-pre-rebuild": job.drop_pre}.get(action, job.run)
    return tr.run_reported(
        a.table,
        action,
        body,
        job.report_state,
        lambda: job.rows_copied,
        report_after=("rebuild", "rollback"),
    )


if __name__ == "__main__":
    sys.exit(main())
