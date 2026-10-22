"""Move a table with rows onto the sort or partition key a migration declares.

A migration declares a target layout with `-- loom: rebuild-if-empty <table>`
and a `CREATE TABLE loom.<table>` statement (`server/core/schema/table_rebuild.py`, which
owns the parsing, the layout comparison and the column check). The runner
rebuilds an empty table itself and refuses one with rows. This tool moves the
rows first, so the runner finds the target layout and records the file
without copying anything.

    python -P -m scripts.ops.table_rebuild --table code_test_facets --dry-run
    python -P -m scripts.ops.table_rebuild --table code_test_facets --yes
    python -P -m scripts.ops.table_rebuild --table code_test_facets --rollback --yes
    python -P -m scripts.ops.table_rebuild --table code_test_facets --drop-pre-rebuild --yes

It reads `LOOM_CH_*` like `loom migrate` and prints the ClickHouse it talks
to before anything else. Without `--yes` a changing action prints its plan
and stops. The steps of a rebuild:

0. Preflight: the server answers, the table is a ReplacingMergeTree with a
   `version` column, its disks have room for a second copy, and no earlier
   run's twin or rollback copy is in the way.
1. Create the twin `loom.<t>_rebuild` from the target statement and check it
   has exactly the live table's columns. The twin is the run's lock: a plain
   CREATE fails when another run made one, and the swap checks the twin is
   still the one this run created.
2. Copy every row, not FINAL, part by part, a part over `--batch-rows` in
   `_part_offset` ranges, with `--max-insert-threads` threads. The engine's
   version column decides which row wins, in the twin as in the live table.
3. Writes landing meanwhile sit in parts the copy has not seen: a merge or an
   insert makes a new part name. Each delta pass copies the parts not copied
   yet, until one comes back small. A row copied twice is a duplicate of the
   same version, which the engine collapses.
4. Verify: the row count under FINAL on both sides, and on a sample of keys
   the newest version of every key (its version and a hash of every column).
   A key written after the pass is skipped as in flight, and a mismatch gets
   another delta pass before it fails the run.
5. Refuse to swap when a mutation (an erasure purge) or a lightweight update
   touched the live table after the copy started: the twin may hold rows it
   removed; an `ALTER` that ClickHouse runs as a mutation (dropping, renaming
   or retyping a column, dropping an index) is refused here too. Rerun with
   `--restart`, after restating a schema `ALTER` in the migration's target.
   Refuse it too (exit 3) when an `ALTER` that is not a mutation (an index,
   a setting, a TTL, a column's default or codec) moved the live table's
   metadata time since the twin's columns were checked: a rebuild does not
   carry it over either. An added column fails the next copy pass (exit 1, no
   swap), or exit 3 once the last pass ran.
6. A last delta pass, then `EXCHANGE TABLES`, then the old table becomes
   `loom.<t>_pre_rebuild`, and its merges are stopped. Two delta passes
   `--settle-s` apart copy what in-flight writers put into it after the swap.
7. Check the result: the live table has the target layout and the rollback
   copy exists with the same columns.

A run is not resumable: an interrupted copy leaves the twin, and `--restart`
drops it and copies again. `--restart` drops a twin whether its run died or
is still going, so check no other run is live first (the plan prints when the
twin was created).

Materialized views follow the name: a view reading the table and a view
writing to it both act on whichever table holds the name after the exchange.

`--rollback` exchanges `<t>_pre_rebuild` back in and copies the rows written
since the swap into it. `--drop-pre-rebuild` drops the rollback copy once the
new layout has served long enough. `ABORT_REBUILD` in `LOOM_REBUILD_DIR` (or
`--abort-file`) stops the run between batches and before the swap; the twin is
left for inspection and `--restart` drops it. An interrupted or failed run
prints what each of the three names holds and the next step.

Exit codes: 0 done or nothing to do, 2 aborted, 3 refused, 4 needs `--yes`,
5 verification failed, 6 touched by a mutation, 7 postcondition failed,
130 interrupted.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Any

if (_ROOT := str(Path(__file__).resolve().parents[2])) not in sys.path:  # run as a file
    sys.path.insert(0, _ROOT)

from server.core.paths import MIGRATIONS_DIR
from server.core.schema import table_rebuild as tr
from server.core.schema.table_rebuild import (
    ABORTED,
    GATE_FAILED,
    OK,
    POSTCONDITION,
    REFUSED,
    TOUCHED,
    UNCONFIRMED,
    Aborted,
    log,
)

DELTA_SMALL_ROWS = 10_000
DELTA_MAX_PASSES = 8
VERIFY_ATTEMPTS = 5
DISK_HEADROOM = 1.1  # a second copy plus the parts merges write meanwhile

_FREE_SPACE_SQL = (
    "SELECT sum(toFloat64(free_space)) FROM system.disks WHERE name IN ("
    "SELECT arrayJoin(disks) FROM system.storage_policies WHERE policy_name = ("
    "SELECT storage_policy FROM system.tables "
    "WHERE database = currentDatabase() AND name = {t:String}))"
)


def batches(rows: int, size: int) -> list[tuple[int, int]]:
    """`_part_offset` ranges [lo, hi) covering a part of `rows` rows. Pure."""
    size = max(1, int(size))
    return [(lo, min(rows, lo + size)) for lo in range(0, max(rows, 1), size)]


def new_parts(now: dict[str, int], copied: set[str]) -> dict[str, int]:
    """The parts of `now` the copy has not read yet. Pure."""
    return {p: n for p, n in now.items() if p not in copied}


Row = tuple[tuple[Any, ...], int, int]  # (key, version, row hash) under FINAL


def compare_sample(live: list[Row], twin: list[Row], in_flight_above: int) -> list[tuple[Any, ...]]:
    """Keys whose newest (version, row hash) differ between the two sides,
    skipping keys the live table wrote after `in_flight_above`. A key that
    comes back twice from one side is a difference too: the two engines do
    not merge the same rows. Pure."""
    bad: set[tuple[Any, ...]] = set()
    sides: list[dict[tuple[Any, ...], tuple[int, int]]] = [{}, {}]
    for side, rows in zip(sides, (live, twin), strict=True):
        for key, version, digest in rows:
            if key in side:
                bad.add(key)
            side[key] = (version, digest)
    have, want = sides
    for key in set(have) | set(want):
        row = have.get(key)
        if row is not None and row[0] > in_flight_above:
            continue
        if row != want.get(key):
            bad.add(key)
    return sorted(bad, key=repr)


def counts_agree(live: int, twin: int, written_since: int) -> bool:
    """Every key the twin holds is live, and the live table holds at most one
    new key per row written since the copy's last pass. Pure."""
    return 0 <= live - twin <= written_since


def disk_refusal(table_bytes: int, free_bytes: float | None) -> str:
    """Why the disks cannot take a second copy of the table, or ''. Pure."""
    if free_bytes is None or free_bytes >= table_bytes * DISK_HEADROOM:
        return ""
    return (
        f"the table's disks have {free_bytes / 2**30:.1f} GiB free; a copy of its "
        f"{table_bytes / 2**30:.1f} GiB needs {DISK_HEADROOM:.1f}x that"
    )


class Rebuild:
    def __init__(self, ch: Any, args: argparse.Namespace) -> None:
        self.ch, self.a = ch, args
        self.t = args.table
        self.twin, self.pre = self.t + tr.TWIN_SUFFIX, self.t + tr.PRE_SUFFIX
        self.file, self.stmt = tr.find_target(self.t, Path(args.migrations_dir))
        self.target = tr.layout_of(self.stmt)
        self.key = tr.key_columns(self.target.sorting_key)
        self.copied: set[str] = set()
        self.rows_copied = 0
        self.twin_uuid = ""
        self.live_meta: Any = None
        self.mod = 1
        self.abort = Path(args.abort_file) if args.abort_file else None

    # -- small reads -------------------------------------------------------
    def q(self, sql: str, **params: Any) -> list[Any]:
        return list(self.ch.query(sql, parameters=params, settings=tr.CONSISTENT).result_rows)

    def exists(self, name: str) -> bool:
        return tr.live_layout(self.ch, name) is not None

    def table_row(self, name: str) -> tuple[str, str, Any] | None:
        return tr.table_row(self.ch, name)

    def parts(self, name: str) -> dict[str, int]:
        return {
            str(p): int(n)
            for p, n in self.q(f"SELECT _part, count() FROM loom.{name} GROUP BY _part")
        }

    def count_final(self, name: str) -> int:
        return int(self.q(f"SELECT count() FROM loom.{name} FINAL")[0][0])

    def max_version(self, name: str) -> int:
        return int(self.q(f"SELECT max(version) FROM loom.{name}")[0][0] or 0)

    def aborted(self) -> bool:
        return bool(self.abort and self.abort.exists())

    # -- copy ---------------------------------------------------------------
    def copy_part(self, src: str, dst: str, part: str, rows: int) -> None:
        sql = tr.copy_sql(
            src,
            dst,
            tr.insertable_columns(self.ch, self.t),
            "_part = {p:String} AND _part_offset >= {lo:UInt64} AND _part_offset < {hi:UInt64}",
        )
        for lo, hi in batches(rows, self.a.batch_rows):
            if self.aborted():
                raise Aborted(f"abort file {self.abort} present; stopped mid-copy")
            self.ch.command(
                sql,
                parameters={"p": part, "lo": lo, "hi": hi},
                settings={
                    **tr.CONSISTENT,
                    "max_insert_threads": self.a.max_insert_threads,
                    "max_threads": max(2, self.a.max_insert_threads),
                    "insert_deduplicate": 0,
                },
            )
            time.sleep(self.a.pause_s)
        self.copied.add(part)
        self.rows_copied += rows

    def copy_new(self, src: str, dst: str) -> int:
        """Copy the parts of `src` not copied yet; the rows they held."""
        todo = new_parts(self.parts(src), self.copied)
        for part, rows in sorted(todo.items()):
            self.copy_part(src, dst, part, rows)
        return sum(todo.values())

    def converge(self) -> None:
        for i in range(DELTA_MAX_PASSES):
            n = self.copy_new(self.t, self.twin)
            log(f"delta pass {i + 1}: {n} rows in parts not copied yet")
            if n <= DELTA_SMALL_ROWS:
                return
        log("delta passes did not settle; verifying anyway")

    # -- guards and checks ----------------------------------------------------
    def patch_parts(self) -> set[str]:
        return {
            str(r[0])
            for r in self.q(
                "SELECT name FROM system.parts "
                "WHERE database = currentDatabase() AND table = {t:String} "
                "AND active AND startsWith(name, 'patch')",
                t=self.t,
            )
        }

    def mutations(self, since: Any = None) -> int:
        """Mutations of the live table still running, or (with `since`) created since."""
        cond = "create_time >= {s:DateTime}" if since is not None else "NOT is_done"
        return int(
            self.q(
                "SELECT count() FROM system.mutations WHERE database = currentDatabase() "
                f"AND table = {{t:String}} AND {cond}",
                t=self.t,
                **({"s": since} if since is not None else {}),
            )[0][0]
        )

    def touched(self, started: Any, patches: set[str]) -> bool:
        """A mutation or a lightweight update reached the live table since `started`."""
        return bool(self.mutations(started) or self.patch_parts() - patches)

    def sample(self, name: str) -> list[Row]:
        key = ", ".join(self.key)
        cols = ", ".join(f"`{c}`" for c in tr.columns(self.ch, self.t))
        digest = f"cityHash64(toString(tuple({cols})))"
        rows = self.q(
            f"SELECT {key}, version, {digest} FROM loom.{name} FINAL "
            f"WHERE cityHash64({key}) % {{m:UInt64}} = 0",
            m=self.mod,
        )
        return [(tuple(r[: len(self.key)]), int(r[-2]), int(r[-1])) for r in rows]

    def verified(self) -> bool:
        self.mod = max(1, self.count_final(self.t) // max(1, self.a.sample_keys))
        for attempt in range(1, VERIFY_ATTEMPTS + 1):
            above = self.max_version(self.t)  # before the pass: rows up to it are copied
            self.copy_new(self.t, self.twin)
            twin_n, twin_s = self.count_final(self.twin), self.sample(self.twin)
            live_s, live_n = self.sample(self.t), self.count_final(self.t)
            since = int(self.q(f"SELECT count() FROM loom.{self.t} WHERE version > {above}")[0][0])
            bad = compare_sample(live_s, twin_s, above)
            log(
                f"verify {attempt}: FINAL rows live {live_n} twin {twin_n}, {since} rows written "
                f"since the pass; {len(bad)} of {len(live_s)} sampled keys differ"
            )
            if counts_agree(live_n, twin_n, since) and not bad:
                return True
            if not since:
                log("no write landed during the check, so the difference is real:", bad[:5])
                return False
        return False

    # -- preflight ------------------------------------------------------------
    def print_target(self) -> None:
        tr.print_target(self.ch, self.t)

    def preflight(self) -> str:
        """Why the live table cannot be rebuilt by this tool, or ''."""
        row = self.table_row(self.t)
        if row is None:
            return f"loom.{self.t} does not exist"
        if "ReplacingMergeTree" not in row[0]:
            return f"loom.{self.t} is a {row[0]}; the copy and the gate assume a ReplacingMergeTree"
        if "version" not in tr.columns(self.ch, self.t):
            return f"loom.{self.t} has no `version` column to order the delta passes by"
        size = int(
            self.q(
                "SELECT sum(bytes_on_disk) FROM system.parts "
                "WHERE database = currentDatabase() AND table = {t:String} AND active",
                t=self.t,
            )[0][0]
            or 0
        )
        free = self.q(_FREE_SPACE_SQL, t=self.t)
        free_bytes = float(free[0][0]) if free and free[0][0] is not None else None
        free_text = "unknown" if free_bytes is None else f"{free_bytes / 2**30:.1f} GiB"
        log(f"  on disk {size / 2**30:.2f} GiB; free on its disks {free_text}")
        return disk_refusal(size, free_bytes)

    def refuse(self) -> str:
        """Why this run cannot start, or ''."""
        if self.exists(self.pre):
            return f"loom.{self.pre} exists: roll back, or drop it with --drop-pre-rebuild"
        twin = self.table_row(self.twin)
        if twin is not None and not self.a.restart:
            return (
                f"loom.{self.twin} (created {twin[2]}, {tr.row_count(self.ch, self.twin)} rows) "
                "is another run's lock or its leftover: pass --restart once no run is live"
            )
        if self.mutations():
            return f"loom.{self.t} has an unfinished mutation; wait for it"
        return ""

    def plan(self) -> None:
        live = tr.live_layout(self.ch, self.t)
        parts = self.parts(self.t)
        rows = sum(parts.values())
        log(f"loom.{self.t}: target from {self.file}")
        log(
            f"  live   sorting key ({live.sorting_key if live else '-'}) "
            f"partition ({live.partition_key if live else '-'})"
        )
        log(
            f"  target sorting key ({self.target.sorting_key}) "
            f"partition ({self.target.partition_key})"
        )
        log(
            f"  {rows} rows in {len(parts)} parts, "
            f"{sum(len(batches(n, self.a.batch_rows)) for n in parts.values())} batches"
        )

    def report_state(self) -> str:
        live, twin, pre = (tr.live_layout(self.ch, n) for n in (self.t, self.twin, self.pre))
        return tr.guidance(
            self.t,
            f"python -P -m scripts.ops.table_rebuild --table {self.t}",
            None if live is None else live == self.target,
            None if twin is None else twin == self.target,
            None if pre is None else pre == self.target,
        )

    # -- the run ------------------------------------------------------------
    def run(self) -> int:
        if tr.live_layout(self.ch, self.t) == self.target:
            self.plan()
            log("already on the target layout; nothing to do")
            return OK
        why = self.preflight()
        if not why:
            self.plan()
            why = self.refuse()
        if why:
            log("REFUSED:", why)
            return REFUSED
        if self.a.dry_run:
            log("dry run: twin DDL follows; nothing changed")
            print(tr.twin_statement(self.stmt, self.t, self.twin))
            return OK
        if not self.a.yes:
            log("plan only: pass --yes to copy and swap")
            return UNCONFIRMED
        return self.copy_and_swap()

    def create_twin(self) -> str:
        """Create the twin; why not, or ''."""
        if self.exists(self.twin):  # refuse() let it through: --restart
            self.ch.command(f"DROP TABLE loom.{self.twin} SYNC")
        try:
            self.ch.command(tr.twin_statement(self.stmt, self.t, self.twin))
        except Exception as e:  # noqa: BLE001 - a lost CREATE race is a refusal, anything else too
            return f"could not create loom.{self.twin} (another run?): {str(e)[:160]}"
        tr.same_columns(self.ch, self.t, self.twin)
        row = self.table_row(self.twin)
        self.twin_uuid = row[1] if row else ""
        return ""

    def altered(self) -> bool:
        """An ALTER that is not a mutation (an index, a setting, a TTL, a column's
        default or codec) moved the live table's metadata time since the twin
        was checked against it. The
        time has one second's precision: two ALTERs in the snapshot's second
        compare equal."""
        row = self.table_row(self.t)
        return row is None or row[2] != self.live_meta

    def still_ours(self) -> bool:
        row = self.table_row(self.twin)
        return row is not None and row[1] == self.twin_uuid

    def copy_and_swap(self) -> int:
        row = self.table_row(self.t)  # before the twin's column check, so any later ALTER shows
        self.live_meta = row[2] if row else None
        why = self.create_twin()
        if why:
            log("REFUSED:", why)
            return REFUSED
        started = self.q("SELECT now()")[0][0]
        patches = self.patch_parts()
        t0 = time.time()
        n = self.copy_new(self.t, self.twin)
        log(f"copied {n} rows in {time.time() - t0:.1f} s")
        self.converge()
        if self.touched(started, patches):
            log(
                "REFUSED: a mutation or lightweight update touched the table during the copy; "
                "rerun with --restart (restate a schema ALTER in the migration's target first)"
            )
            return TOUCHED
        if not self.verified():
            log("GATE FAILED: the twin does not match; no swap. The twin is left for inspection")
            return GATE_FAILED
        if self.touched(started, patches):
            log(
                "REFUSED: a mutation or lightweight update touched the table during the check; "
                "rerun with --restart (restate a schema ALTER in the migration's target first)"
            )
            return TOUCHED
        if self.aborted():
            log("abort file present; no swap")
            return ABORTED
        if self.altered():
            log(
                f"REFUSED: loom.{self.t} was altered during the copy; no swap. A rebuild does "
                "not carry an ALTER over: restate it in the migration's target, then --restart"
            )
            return REFUSED
        if not self.still_ours():
            log(f"REFUSED: loom.{self.twin} is no longer the twin this run created; no swap")
            return REFUSED
        self.swap()
        return self.check_swapped()

    def swap(self) -> None:
        self.copy_new(self.t, self.twin)
        tr.swap_in(self.ch, self.t)
        for _ in range(2):
            time.sleep(self.a.settle_s)
            log("post-swap delta rows:", self.copy_new(self.pre, self.t))

    def check_swapped(self) -> int:
        live = tr.live_layout(self.ch, self.t)
        log("live", self.count_final(self.t), "rows under FINAL; layout", live)
        if live != self.target or not self.exists(self.pre) or self.exists(self.twin):
            log("POSTCONDITION FAILED: the names do not hold what the swap leaves")
            return POSTCONDITION
        tr.same_columns(self.ch, self.t, self.pre)
        return OK

    def rollback(self) -> int:
        if not self.exists(self.pre):
            log(f"REFUSED: no loom.{self.pre} to roll back to")
            return REFUSED
        log(f"rollback: loom.{self.pre} back in as loom.{self.t}, the rows written since copied")
        if not self.a.yes:
            log("plan only: pass --yes to roll back")
            return UNCONFIRMED
        since = self.max_version(self.pre)
        self.ch.command(f"EXCHANGE TABLES loom.{self.t} AND loom.{self.pre}")
        sql = tr.copy_sql(
            self.pre, self.t, tr.insertable_columns(self.ch, self.t), "version > {v:UInt64}"
        )
        for i in range(2):  # the second pass: writes in flight at the exchange
            if i:
                time.sleep(self.a.settle_s)
            self.ch.command(sql, parameters={"v": since}, settings=tr.CONSISTENT)
        try:
            self.ch.command(f"SYSTEM START MERGES loom.{self.t}")
        except Exception as e:  # noqa: BLE001 - merges resume on the next restart anyway
            log("could not start merges:", str(e)[:120])
        if tr.live_layout(self.ch, self.pre) != self.target:
            log(f"POSTCONDITION FAILED: loom.{self.pre} does not hold the target layout")
            return POSTCONDITION
        log(
            f"ROLLED BACK: loom.{self.t} is the old layout again; "
            f"loom.{self.pre} holds the new one, drop it when done"
        )
        return OK

    def drop_pre(self) -> int:
        if self.target not in (tr.live_layout(self.ch, self.t), tr.live_layout(self.ch, self.pre)):
            log(f"REFUSED: neither loom.{self.t} nor loom.{self.pre} has the target layout")
            return REFUSED
        if not self.exists(self.pre):
            log(f"loom.{self.pre} is already gone")
            return OK
        log(f"drop: loom.{self.pre} ({tr.row_count(self.ch, self.pre)} rows); cannot be undone")
        if not self.a.yes:
            log("plan only: pass --yes to drop it")
            return UNCONFIRMED
        self.ch.command(f"DROP TABLE loom.{self.pre} SYNC")
        if self.exists(self.pre):
            log(f"POSTCONDITION FAILED: loom.{self.pre} is still there")
            return POSTCONDITION
        log(f"dropped loom.{self.pre}")
        return OK


def parse(argv: list[str] | None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument(
        "--table", required=True, help="table name without the database, e.g. code_test_facets"
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true", help="print the plan and the twin DDL; change nothing"
    )
    mode.add_argument("--rollback", action="store_true", help="exchange <t>_pre_rebuild back in")
    mode.add_argument("--drop-pre-rebuild", action="store_true", help="drop the rollback copy")
    ap.add_argument("--yes", action="store_true", help="carry out the change the plan prints")
    ap.add_argument("--restart", action="store_true", help="drop a twin left by an earlier run")
    ap.add_argument("--batch-rows", type=int, default=500_000)
    ap.add_argument("--max-insert-threads", type=int, default=2)
    ap.add_argument(
        "--pause-s", type=float, default=0.0, help="sleep between batches to cap the load"
    )
    ap.add_argument("--sample-keys", type=int, default=20_000, help="about how many keys to verify")
    ap.add_argument("--settle-s", type=float, default=15.0, help="wait before each post-swap delta")
    ap.add_argument(
        "--abort-file", default=str(Path(os.environ.get("LOOM_REBUILD_DIR", ".")) / "ABORT_REBUILD")
    )
    ap.add_argument("--migrations-dir", default=str(MIGRATIONS_DIR))
    return ap.parse_args(argv)


def action_of(args: argparse.Namespace) -> str:
    if args.rollback:
        return "rollback"
    if args.drop_pre_rebuild:
        return "drop-pre-rebuild"
    return "dry-run" if args.dry_run else "rebuild"


def execute(job: Rebuild, action: str) -> int:
    job.print_target()
    if action == "rollback":
        return job.rollback()
    if action == "drop-pre-rebuild":
        return job.drop_pre()
    return job.run()


def main(argv: list[str] | None = None, ch: Any = None) -> int:
    args = parse(argv)
    action = action_of(args)
    if ch is None:
        from server.core.schema.migrate import client

        ch = client()
    try:
        job = Rebuild(ch, args)
    except LookupError as e:  # no migration declares a rebuild of this table
        why = str(e)

        def refuse() -> int:
            tr.print_target(ch, args.table)
            log("REFUSED:", why)
            return REFUSED

        return tr.run_reported(
            args.table,
            action,
            refuse,
            lambda: "check --table: only a table a migration declares can be rebuilt",
            lambda: 0,
        )
    return tr.run_reported(
        args.table, action, lambda: execute(job, action), job.report_state, lambda: job.rows_copied
    )


if __name__ == "__main__":
    sys.exit(main())
