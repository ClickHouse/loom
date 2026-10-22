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

The dimension comes from the live table: a coded column states it, a
plain column's real (not all-zero) vectors show it (`LOOM_EMBEDDING_DIM` when
it is one of their widths, else the most frequent width), and a column with
none takes `LOOM_EMBEDDING_DIM`. Every named column must agree.

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
`SYSTEM SYNC REPLICA` privileges on the table (the preflight runs both once), and no row
written in the last hour with an empty vector in the named columns (writers'
fixed-length placeholders must be deployed first: a coded column rejects an
empty vector). ReplicatedMergeTree runs connect directly to one replica throughout
the run: each consistency and width read after the drain also reads, in the same
statement, whether the replica answering it has fetched every part its replication
log names, and fails the run when it has not.

After a swap the tool compares the rollback copy with the live table. A change only
the rollback copy holds names `--rollback`; a change the live table holds (a write
after the swap, or other content at one version) keeps both tables for inspection.
`--drop-pre-rebuild` runs the same comparison first, after a swap and after a
rollback, and refuses (keeping the copy) while the copy it would drop holds a change.

The twin is the run's lock: an existing twin refuses the run unless
`--reuse` (continue from it) or `--restart` (drop it and copy again), and the
swap refuses when the twin is no longer the one this run created or reused.
An `ABORT_REBUILD` file in `LOOM_REBUILD_DIR` (default `.`) stops the run
before the copy and before the swap, leaving the twin.

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
    if granularity > 0:
        live = re.search(r"index_granularity = (\d+)", ddl)
        if int(live.group(1) if live else 8192) != granularity:
            return False
    return all(column_shape(ddl, c) == column_shape(want, c) for c in columns)


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


def widths_sql(table: str, column: str, fin: str) -> str:
    """Count every real vector by width, the most frequent first; zero placeholders are
    left out. Pure."""
    return (
        f"SELECT length({column}) AS w, count() AS n FROM loom.{table}{fin} "
        f"WHERE {nonzero(column)} GROUP BY w ORDER BY n DESC, w"
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
# mutation fails the run through the mutation count instead.
REPLICA_SYNCED_SQL = (
    "(SELECT countIf(log_pointer > log_max_index) = length({tables:Array(String)}) "
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
    """A read after the drain was answered by a replica that has not fetched every part."""


class VectorRebuild:
    def __init__(self, ch: Any, args: argparse.Namespace, client: Any = None) -> None:
        self.ch, self.a = ch, args
        self.client = client or (lambda: ch)
        self.t = args.table
        self.twin, self.pre = self.t + tr.TWIN_SUFFIX, self.t + tr.PRE_SUFFIX
        self.cols = [x.strip() for x in args.columns.split(",") if x.strip()]
        self.chain = args.chain or None
        self.abort = Path(args.abort_file)
        self.twin_uuid = ""
        self.replicas = replicas_source(args.cluster)
        self.rows_copied = 0
        self.dim = 0
        self.fin = ""  # ' FINAL' where the live table is a ReplacingMergeTree
        self.since = 0  # the server's Unix time before the preflight's mutation check
        self.reads: dict[str, Any] = {}  # settings every read takes; sequential after the drain
        self.replicated = False  # the live table is a ReplicatedMergeTree
        self.rollback_only = False  # a check found what only the rollback copy holds
        self.diverged = False  # a check found a change the live table or a dropped copy holds
        self.recheck = False  # the checks after the swap could not run
        self.swapped = False  # the swap and its postcondition passed

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
            parameters={"tables": tables},
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
        from server.settings import settings

        name = self.t if self.exists(self.t) else self.pre
        ddl = self.ddl(name)
        fin = read_final(parse_engine(ddl)[0])  # the width of the rows Loom serves
        dims: set[int] = set()
        for c in self.cols:
            d = vector_dim(ddl, c)
            if d is None:
                # every real vector counted, the configured width first: a read that
                # stops early would see only the first orgs in sort-key order
                widths = [int(w) for w, _ in self.q(widths_sql(name, c, fin))]
                d = int(settings.embedding_dim)
                if widths and d not in widths:
                    d = widths[0]
            dims.add(d)
        if len(dims) != 1:
            raise SystemExit(f"the named columns disagree on the dimension: {sorted(dims)}")
        return dims.pop()

    def aborted(self) -> bool:
        return self.abort.exists()

    @property
    def run_line(self) -> str:
        """The command line that runs this tool on this table with this run's layout
        flags, up to its action flags."""
        line = (
            f"python -P -m scripts.ops.vector_codec_rebuild --table {self.t} "
            f"--columns {','.join(self.cols)} --method {self.a.method}"
        )
        if self.chain:
            line += f" --chain {shlex.quote(self.chain)}"
        if self.a.index_granularity:
            line += f" --index-granularity {self.a.index_granularity}"
        return line

    def report_state(self) -> str:
        run = self.run_line
        if self.swapped:
            return (
                f"keep writers and erasure stopped; run `{run} --drop-pre-rebuild --yes`, "
                "which checks the two tables again before it drops the rollback copy"
            )
        if self.recheck:
            return (
                f"the checks could not run: fix the grant or connect directly to one replica "
                f"and rerun `{run} --drop-pre-rebuild --yes`; loom.{self.pre} is kept"
            )
        if self.rollback_only:
            return (
                f"loom.{self.pre} holds a change the new table lacks. "
                + KEEP_AND_ROLL_BACK.format(run=run)
            )
        if self.diverged:
            return f"loom.{self.t} and loom.{self.pre} differ. {KEEP_BOTH}"
        return tr.guidance(
            self.t,
            run,
            self.target_state(self.t),
            self.exists(self.twin),
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
        if twin is not None and not (self.a.reuse or self.a.restart):
            return (
                f"loom.{self.twin} (created {twin[2]}, {tr.row_count(self.ch, self.twin)} rows) is "
                "another run's lock or its leftover: --reuse continues from it, --restart drops it"
            )
        if why := self.mutation_refusal():
            return why
        if why := self.drain_refusal(parse_engine(ddl)[0]):
            return why
        try:
            return self.width_refusal(ddl)
        except ReplicaBehind as e:
            return str(e)

    def mutation_refusal(self) -> str:
        """A stopped writer can leave mutations running on ClickHouse. The gate before the
        swap counts every mutation since `self.since`, less the clock-skew margin, so the
        preflight refuses on the same rule rather than after the copy."""
        if self.mutations_since(self.t, self.since):
            return (
                f"loom.{self.t} has a mutation still running or one that finished under a "
                "minute ago; wait for it to finish and rerun in a minute"
            )
        return ""

    def drain_refusal(self, engine: str) -> str:
        """Why this user cannot run the pre-swap drain, or '': it runs once here, so a
        missing grant refuses before the copy rather than failing after the gates."""
        try:
            self.drain(engine)
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
        from server.settings import settings

        # a coded column's codec rejects any other width, so only plain columns are scanned
        table = table or self.t
        for c in [c for c in self.cols if vector_dim(ddl, c) is None]:
            widths = self.q_drained(mixed_width_sql(table, c, self.fin, dim=self.dim), [table])
            if widths:
                found = ", ".join(f"width {w}: {n} row(s)" for w, n in widths)
                return (
                    f"{c} holds vectors of more than one width (target {self.dim}; {found}; "
                    f"LOOM_EMBEDDING_DIM {settings.embedding_dim}): the copy would zero all but "
                    f"width {self.dim}; re-embed to LOOM_EMBEDDING_DIM (`loom reembed "
                    "--delete-old`, for memory_embeddings, entity_vectors and "
                    "conversation_embeddings) and rerun"
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
        """Copy the rows of `src` that `dst` lacks (newer versions, or absent keys), the
        INSERT under `limit_s` seconds."""
        if version:
            above = self.q(f"SELECT max({version}) FROM loom.{dst}")[0][0]
            if above is None:
                return 0
            cond = f"{version} > {sql_literal(above)}"
        else:
            ident = ", ".join(key)
            cond = f"({ident}) NOT IN (SELECT {ident} FROM loom.{dst})"
        n = int(self.q(f"SELECT count() FROM loom.{src} WHERE {cond}")[0][0])
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
        different replica serves a later request.
        """
        self.cmd(f"SYSTEM FLUSH ASYNC INSERT QUEUE loom.{self.t}")
        if engine.startswith(("Replicated", "Shared")):
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
        # a fixed-length placeholder is all zeros: its cosine is NaN, never a probe
        real = vector_filter(vcol, dim=dim)
        probes = self.q(
            f"SELECT org, namespace, {vcol} FROM loom.{self.t} WHERE {real} ORDER BY rand() LIMIT {self.a.probes}"
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
        cons = self.consistent(version, key)
        log("consistency (mismatched, source_only, twin_only):", cons)
        if cons[0] or cons[1]:
            log("repair keys:", self.repair_into(sel, version, key))
            cons = self.consistent(version, key)
            log("consistency after repair:", cons)
        diverged = (
            "GATE FAILED: the twin diverges (a key's version, row count or content); no "
            "swap. Stop writes and erasures and rerun with --restart"
        )
        if failure := self.gate_failure(cons, version, key, diverged):
            log(failure)
            return GATE_FAILED
        drec = self.recall(ddl)
        if not drec:
            log("GATE FAILED: no probe rows with a full-length vector")
            return GATE_FAILED
        log(f"recall gate: distance recall mean {statistics.mean(drec):.3f} min {min(drec):.3f}")
        if not (min(drec) >= 0.95 and statistics.mean(drec) >= 0.99):
            log("GATE FAILED: recall; the twin is left for inspection")
            return GATE_FAILED
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
        cons = self.consistent(version, key)
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
        if set(tr.columns(self.ch, self.t)) != set(tr.columns(self.ch, self.pre)):
            # either table may have changed, so neither --rollback nor the drop keeps it whole
            self.diverged = True
            log("POSTCONDITION FAILED: the rollback copy's columns differ by name")
            return POSTCONDITION
        if self.a.require_patch_free:
            log("live patch parts after:", self.q(patch_parts_sql(self.t))[0][0])
        try:
            why, rollback = self.rollback_copy_change(engine, version, key, since)
        except Exception as e:  # noqa: BLE001 - checks that cannot run keep the copy
            self.recheck = True
            log(
                f"POSTCONDITION FAILED: the checks could not run ({str(e)[:160]}); "
                f"loom.{self.pre} is kept"
            )
            return POSTCONDITION
        if why:
            log(f"POSTCONDITION FAILED: {why}. {self.keep_copy(rollback)}")
            return POSTCONDITION
        self.swapped = True
        return OK

    def keep_copy(self, rollback: bool) -> str:
        """The next step when the two tables differ, recorded for the summary: --rollback
        when only the rollback copy holds the change, else inspection with both kept."""
        if rollback:
            self.rollback_only = True
            return KEEP_AND_ROLL_BACK.format(run=self.run_line)
        self.diverged = True
        return KEEP_BOTH

    def differences(
        self, version: str | None, key: list[str], old: str, new: str
    ) -> tuple[str, str]:
        """What the old-layout table `old` holds that the new-layout table `new` lacks, and
        what `new` holds that `old` lacks, each as a phrase or ''. A key both hold at one
        version with other content counts for `new`: copying back newer versions and absent
        keys cannot restore it. Raises when the checks cannot run."""
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
        old_ahead = new_ahead = ""
        if old_only or old_newer:
            old_ahead = f"{old_only} key(s) only in loom.{old}, {old_newer} newer there"
        if new_only or new_newer or same_version:
            new_ahead = (
                f"{new_only} key(s) only in loom.{new}, {new_newer} newer there, "
                f"{same_version} with other content at one version"
            )
        return old_ahead, new_ahead

    def rollback_copy_change(
        self, engine: str, version: str | None, key: list[str], since: int
    ) -> tuple[str, bool]:
        """Why the rollback copy and the live table differ, or '', and whether --rollback
        restores the difference. After the swap the rollback copy is the source as writers
        left it: the live table must match it, with no mutation since `since` (the gate saw
        none before the swap). Raises when the checks cannot run."""
        self.sync_rollback_copy(engine)
        old_ahead, new_ahead = self.differences(version, key, self.pre, self.t)
        if new_ahead:
            return (
                f"loom.{self.t} holds a change loom.{self.pre} lacks ({new_ahead}): a write "
                "after the swap, or other content at one version, which --rollback would "
                "drop, since it copies back only newer versions and absent keys"
            ), False
        mutations = self.mutations_since(self.pre, since)
        log("mutations on the rollback copy:", mutations)
        if old_ahead or mutations:
            return (
                f"the source changed after the last check ({old_ahead or 'no key'}; "
                f"{mutations} mutation(s) on loom.{self.pre}), and the new table may not "
                "hold the change"
            ), True
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
        return OK

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
        """Why the new-layout copy a rollback left holds a change the live table lacks (a
        same-version change made in the new table, which the rollback did not copy back),
        or ''. Raises when the checks cannot run."""
        engine, version, key = self.drain_after_swap()
        self.sync_rollback_copy(engine)
        _, new_ahead = self.differences(version, key, self.t, self.pre)
        if new_ahead:
            return f"loom.{self.pre} holds a change loom.{self.t} lacks ({new_ahead})"
        return ""

    def drop_pre(self) -> int:
        tr.print_target(self.ch, self.t)
        if not self.exists(self.pre):
            log(f"loom.{self.pre} is already gone")
            return OK
        self.dim = self.find_dim()
        live, pre = self.target_state(self.t), self.target_state(self.pre)
        if not (live or pre):
            log(f"REFUSED: neither loom.{self.t} nor loom.{self.pre} has the target layout")
            return REFUSED
        # after a swap or a rollback the copy may be the only copy of a change
        if bool(live) != bool(pre):
            try:
                why, rollback = (
                    self.swapped_change() if live else (self.rolled_back_change(), False)
                )
            except Exception as e:  # noqa: BLE001 - checks that cannot run keep the copy
                self.recheck = True
                log(
                    f"REFUSED: the checks could not run ({str(e)[:160]}): fix the grant or "
                    "connect directly to one replica and rerun --drop-pre-rebuild; "
                    f"loom.{self.pre} is kept"
                )
                return REFUSED
            if why:
                log(f"REFUSED: {why}; loom.{self.pre} is kept. {self.keep_copy(rollback)}")
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
        default=3.0,
        help="the recall gate reads the twin at the multiplier its leg serves (code_summaries' cards: 10)",
    )
    ap.add_argument(
        "--index-granularity",
        type=int,
        default=0,
        help="the twin's index_granularity (0 = keep the table's; code_summaries takes 256)",
    )
    ap.add_argument("--probes", type=int, default=20)
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
        default="default",
        help="cluster whose replicas the preflight and monitor check; '' = this server only",
    )
    ap.add_argument(
        "--settle-s", type=float, default=15.0, help="wait between rollback delta passes"
    )
    ap.add_argument(
        "--statement-timeout-s",
        type=float,
        default=1800.0,
        help="seconds each read, delta and repair INSERT may run, after the swap included",
    )
    ap.add_argument(
        "--abort-file", default=str(Path(os.environ.get("LOOM_REBUILD_DIR", ".")) / "ABORT_REBUILD")
    )
    a = ap.parse_args(argv)
    a.table = a.table.removeprefix("loom.")
    if a.cluster and not _CLUSTER_NAME.fullmatch(a.cluster):
        ap.error(f"--cluster: not a cluster name: {a.cluster!r}")
    if not math.isfinite(a.statement_timeout_s) or a.statement_timeout_s <= 0:
        ap.error("--statement-timeout-s must be finite and above 0")
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
    return tr.run_reported(a.table, action, body, job.report_state, lambda: job.rows_copied)


if __name__ == "__main__":
    sys.exit(main())
