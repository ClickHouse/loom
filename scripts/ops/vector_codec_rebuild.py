# ruff: noqa: E501
"""Move a loom vector table onto the `Quantized` codec.

A column's codec and type are CREATE-only, so the table is rebuilt: a twin
`loom.<t>_rebuild` from `SHOW CREATE` with the vector columns rewritten, every
row copied (a short vector padded to the fixed length), a delta keyed on the
ReplacingMergeTree version column (or an anti-join on the sorting key for a
plain MergeTree), a FULL OUTER JOIN consistency gate on the sorting key, a
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
plain column's vectors show it (the most common length), and an empty table
takes `LOOM_EMBEDDING_DIM`. Every named column must agree.

The gates, each of which leaves the table as it is:

- Widths (before the twin, `--dry-run` included): a named column holding
  non-empty vectors of another length, among the rows Loom serves (under FINAL
  on a ReplacingMergeTree), refuses the run with the widths and their row
  counts, since the copy would replace them with zero vectors; re-embed those
  rows first. An empty vector is copied as a zero placeholder.
- Consistency: a key whose newest version differs, or that the source holds
  and the twin lacks, is repaired key by key and fails the run if it remains.
  A key only the twin holds fails it too: rows were removed from the source
  during the copy (an erasure or delete) and the swap would bring them back;
  stop writes and rerun with `--restart`.
- Recall: both reads rank real vectors only (a zero placeholder's cosine
  distance is NaN), and each probe's recall is over the exact neighbours it
  found, at most 20.

`--require-patch-free`: the twin-and-EXCHANGE also sheds the patch parts of
past lightweight UPDATEs (`OPTIMIZE FINAL` / `APPLY PATCHES` do not). The flag
logs the live table's active patch parts, refuses to EXCHANGE unless the twin
has none, and logs the count after.

Preconditions: one ClickHouse version (26.7+) on every replica of the
cluster `--cluster` names (`default`; `''` checks this server only), `enable_quantized_codec`
settable or held at 1 for this user (`allow_suspicious_codecs` too with `--chain`; a setting held
at 1 is never sent), every replica writable,
and no row written in the last hour with an empty vector in the named columns
(writers' fixed-length placeholders must be deployed first: a coded column
rejects an empty vector).

The twin is the run's lock: an existing twin refuses the run unless
`--reuse` (continue from it) or `--restart` (drop it and copy again), and the
swap refuses when the twin is no longer the one this run created or reused.
An `ABORT_REBUILD` file in `LOOM_REBUILD_DIR` (default `.`) stops the run
before the copy and before the swap, leaving the twin.

Exit codes (shared with table_rebuild): 0 done or nothing to do, 2 aborted,
3 refused, 4 needs `--yes`, 5 a gate failed (consistency, recall, patch
parts), 7 postcondition failed, 130 interrupted.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import re
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
# How far above the exact 20th distance a twin hit may read and still count.
DISTANCE_TOLERANCE = 1e-6
COUNT_SETTLE_ATTEMPTS = 8
COUNT_SETTLE_SLEEP_S = 15
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


def mixed_width_sql(table: str, column: str, fin: str, *, dim: int) -> str:
    """(width, rows) of the non-empty vectors in `column` whose length is not `dim`:
    the copy would replace each with a zero vector. Pure."""
    return (
        f"SELECT length({column}) AS w, count() FROM loom.{table}{fin} "
        f"WHERE length({column}) NOT IN (0, {int(dim)}) GROUP BY w ORDER BY w"
    )


def replace_select(columns: list[str], *, dim: int) -> str:
    """The copy's SELECT: every named column padded to the fixed length (an empty vector
    becomes a zero placeholder; the preflight refuses any other width)."""

    def vec(c: str) -> str:
        return f"if(length({c}) = {int(dim)}, {c}, {pad(dim)})"

    return "SELECT * REPLACE (" + ", ".join(f"{vec(c)} AS {c}" for c in columns) + ")"


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
    return f"length({column}) = {int(dim)} AND {column}[1] != 0"


_CLUSTER_NAME = re.compile(r"[A-Za-z0-9_.-]+")


def replicas_source(cluster: str) -> Callable[[str], str]:
    """`system.<t>` on every replica of `cluster`, or on this server for ''.
    The name is spliced into the query, so it must be a plain name."""
    if cluster and not _CLUSTER_NAME.fullmatch(cluster):
        raise ValueError(f"not a cluster name: {cluster!r}")
    if not cluster:
        return lambda t: f"system.{t}"
    return lambda t: f"clusterAllReplicas('{cluster}', system.{t})"


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

    # -- small reads -------------------------------------------------------
    def q(self, sql: str, **st: Any) -> list[Any]:
        return list(self.ch.query(sql, settings=st or None).result_rows)

    def cmd(self, sql: str, **st: Any) -> None:
        self.ch.command(sql, settings=st or None)

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
        dims: set[int] = set()
        for c in self.cols:
            d = vector_dim(ddl, c)
            if d is None:
                seen = self.q(f"SELECT topK(1)(length({c})) FROM loom.{name} WHERE length({c}) > 0")
                d = int(seen[0][0][0]) if seen and seen[0][0] else int(settings.embedding_dim)
            dims.add(d)
        if len(dims) != 1:
            raise SystemExit(f"the named columns disagree on the dimension: {sorted(dims)}")
        return dims.pop()

    def aborted(self) -> bool:
        return self.abort.exists()

    def report_state(self) -> str:
        return tr.guidance(
            self.t,
            f"python -P -m scripts.ops.vector_codec_rebuild --table {self.t} --columns {','.join(self.cols)}",
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
        fin = read_final(parse_engine(ddl)[0])
        for c in self.cols:
            widths = self.q(mixed_width_sql(self.t, c, fin, dim=self.dim), max_execution_time=1800)
            if widths:
                found = ", ".join(f"{n} rows of width {w}" for w, n in widths)
                return (
                    f"{c} holds vectors of another width than {self.dim} ({found}): the copy "
                    "would zero them; re-embed those rows first"
                )
        twin = tr.table_row(self.ch, self.twin)
        if twin is not None and not (self.a.reuse or self.a.restart):
            return (
                f"loom.{self.twin} (created {twin[2]}, {tr.row_count(self.ch, self.twin)} rows) is "
                "another run's lock or its leftover: --reuse continues from it, --restart drops it"
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
        return self.copy_and_swap(ddl, twin_ddl)

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

    def delta_into(self, dst: str, src: str, sel: str, version: str | None, key: list[str]) -> int:
        """Copy the rows of `src` that `dst` lacks: newer versions, or absent keys."""
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
                f"INSERT INTO loom.{dst} {sel} FROM loom.{src} WHERE {cond}", **self.codec_on()
            )
            self.rows_copied += n
        return n

    def repair_into(
        self, dst: str, src: str, sel: str, fin: str, version: str | None, key: list[str]
    ) -> int:
        """Copy the rows of `src` for every key whose newest version `dst` lacks: a newer
        version in `src`, or a key `dst` does not have. `delta_into` takes the rows above
        `dst`'s highest version, which misses a row written during a long copy with a
        version below a row the copy already took; this compares key by key, the way
        `consistent` does, and copies exactly the keys that diverge. The keys come back to
        the client and each INSERT names them as literals: on a shared cluster an
        `IN (subquery)` INSERT runs the subquery on other replicas, which a user
        without cluster-wide grants is refused."""
        ident = ", ".join(key)
        vsel = f", {version}" if version else ""
        newer = f"s.{version} > t.{version} OR t.__present = 0" if version else "t.__present = 0"
        diverged = (
            f"SELECT {ident} FROM (SELECT {ident}{vsel} FROM loom.{src}{fin}) AS s "
            f"LEFT JOIN (SELECT {ident}{vsel}, 1 AS __present FROM loom.{dst}{fin}) AS t "
            f"USING ({ident}) WHERE {newer}"
        )
        keys = self.q(diverged, join_use_nulls=0, max_execution_time=1800)
        for i in range(0, len(keys), REPAIR_BATCH):
            batch = keys[i : i + REPAIR_BATCH]
            lits = ", ".join("(" + ", ".join(key_literal(v) for v in k) + ")" for k in batch)
            self.cmd(
                f"INSERT INTO loom.{dst} {sel} FROM loom.{src} WHERE ({ident}) IN ({lits})",
                **self.codec_on(),
                max_execution_time=1800,
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

    def consistent(self, fin: str, version: str | None, key: list[str]) -> tuple[int, int, int]:
        ident = ", ".join(key)
        vsel = f", {version}" if version else ""
        vcmp = f"s.{version} != t.{version}" if version else "0"
        vnull_s = f"s.{version} = 0 OR s.{version} IS NULL" if version else "s.__present = 0"
        vnull_t = f"t.{version} = 0 OR t.{version} IS NULL" if version else "t.__present = 0"
        row = self.q(
            f"""
          SELECT countIf({vcmp}) AS mismatched, countIf({vnull_t}) AS source_only, countIf({vnull_s}) AS twin_only
          FROM (SELECT {ident}{vsel}, 1 AS __present FROM loom.{self.t}{fin}) AS s
          FULL OUTER JOIN (SELECT {ident}{vsel}, 1 AS __present FROM loom.{self.twin}{fin}) AS t USING ({ident})
        """,
            join_use_nulls=0,
            max_execution_time=1800,
        )[0]
        return int(row[0]), int(row[1]), int(row[2])

    def recall(self, ddl: str, fin: str) -> list[float]:
        """Distance recall per probe: exact on the source vs the twin read as the legs read it."""
        vcol, dim = self.cols[0], self.dim
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
        fin = read_final(engine)
        sel = replace_select(self.cols, dim=self.dim)
        if self.aborted():
            raise Aborted("abort file present before the copy; the twin is left")
        if not reused:
            stop = threading.Event()
            threading.Thread(target=self.monitor, args=(stop,), daemon=True).start()
            t0 = time.time()
            try:
                self.cmd(
                    f"INSERT INTO loom.{self.twin} {sel} FROM loom.{self.t}",
                    **self.codec_on(),
                    max_insert_threads=4,
                    max_execution_time=7000,
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
        log("delta-1 rows into twin:", self.delta_into(self.twin, self.t, sel, version, key))
        cons = self.consistent(fin, version, key)
        log("consistency (mismatched, source_only, twin_only):", cons)
        if cons[0] or cons[1]:
            log("repair keys:", self.repair_into(self.twin, self.t, sel, fin, version, key))
            cons = self.consistent(fin, version, key)
            log("consistency after repair:", cons)
        if cons[0] or cons[1]:
            log("GATE FAILED: the twin diverges; no swap")
            return GATE_FAILED
        if cons[2]:
            log(
                f"GATE FAILED: {cons[2]} keys are in the twin only: rows were removed from the "
                "source during the copy (an erasure or delete), and the swap would bring them back; "
                "no swap. Stop writes and rerun with --restart"
            )
            return GATE_FAILED
        drec = self.recall(ddl, fin)
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
        self.delta_into(self.twin, self.t, sel, version, key)
        tr.swap_in(self.ch, self.t)
        for _ in range(2):  # writes in flight at the exchange landed in the old table
            time.sleep(self.a.settle_s)
            log("post-swap delta rows:", self.delta_into(self.t, self.pre, sel, version, key))
            log(
                "post-swap repair keys:",
                self.repair_into(self.t, self.pre, sel, fin, version, key),
            )
        return self.check_swapped()

    def check_swapped(self) -> int:
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
            log("POSTCONDITION FAILED: the rollback copy's columns differ by name")
            return POSTCONDITION
        return OK

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
            log("rows copied back:", self.delta_into(self.t, self.pre, sel, version, key))
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

    def drop_pre(self) -> int:
        tr.print_target(self.ch, self.t)
        if not self.exists(self.pre):
            log(f"loom.{self.pre} is already gone")
            return OK
        self.dim = self.find_dim()
        if not (self.target_state(self.t) or self.target_state(self.pre)):
            log(f"REFUSED: neither loom.{self.t} nor loom.{self.pre} has the target layout")
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
        help="check preconditions, print the twin DDL, write nothing",
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
    ap.add_argument("--settle-s", type=float, default=15.0, help="wait before each post-swap delta")
    ap.add_argument(
        "--abort-file", default=str(Path(os.environ.get("LOOM_REBUILD_DIR", ".")) / "ABORT_REBUILD")
    )
    a = ap.parse_args(argv)
    a.table = a.table.removeprefix("loom.")
    if a.cluster and not _CLUSTER_NAME.fullmatch(a.cluster):
        ap.error(f"--cluster: not a cluster name: {a.cluster!r}")
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
