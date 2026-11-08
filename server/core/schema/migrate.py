"""The migration runner: applies `schemas/migrations/*.sql` against the
`loom._migrations` ledger. docs/development/migrations.md has the rules.

Usage:
    loom migrate                     # apply pending migrations
    loom migrate --status            # applied/pending; no changes
    loom migrate --dry-run           # what would apply, with SHA-256 and ledger drift
    loom migrate --dry-run --strict  # as above; exit 1 on ledger-only rows or drift
    loom migrate --target N          # apply through migration N only
    python -m server.core.schema.migrate   # the same, from Make and CI

Invariants:
- A file is recorded (filename + SHA-256) only after every statement ran and
  every mutation it started finished; a later edit of an applied file is
  checksum drift.
- Every ALTER waits on the replicas (`alter_sync`, a mutating one also
  `mutations_sync`) where this user may send the setting: level 3 (active
  replicas, so a replica restarting mid-deploy does not fail the file) where
  the server accepts it, else 2. A mutation that fails, or outlives
  `LOOM_MIGRATE_MUTATION_WAIT_S`, fails the file unrecorded.
- A baseline (`-- loom: baseline covers N`) is recorded without running for a
  ledger that holds file N, and `stranded` (refused) for one that holds only
  some of the files it replaces.
- The non-dry apply runs under the cross-container lock (`migrate_lease`).

Connection: the `LOOM_CH_*` settings, read through `server.settings` so `.env`
applies; port 8443 or `LOOM_CH_SECURE=true` opens TLS.
"""

from __future__ import annotations

import argparse
import logging
import re
import socket
import sys
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import TYPE_CHECKING, cast

from server.core.ch_errors import error_code as _error_code
from server.core.ch_names import NamedClient
from server.core.hashing import sha256_text as _checksum
from server.core.leases import LOCK_TABLE
from server.core.paths import MIGRATIONS_DIR
from server.core.schema import table_rebuild
from server.core.schema.journal import (
    baseline_map,
    classify_ledger,
    sorted_migrations,
    split_statements,
    through_target,
)
from server.core.schema.migrate_lease import (
    NO_KEEPER_CODES,
    KeeperLock,
    KeeperRequired,
    KeeperUnavailable,
    LeaseConfig,
    bootstrap_keeper_lock,
)
from server.core.schema.vector_codec_ddl import CODEC_SETTINGS_SQL, NO_CODEC, CodecSettings

if TYPE_CHECKING:
    from clickhouse_connect.driver.client import Client


def client(
    *,
    receive_timeout_s: int | None = None,
    connect_timeout_s: int = 10,
    retries: bool = True,
    session: bool = True,
) -> Client:
    """A client on the `LOOM_CH_*` settings. `receive_timeout_s` replaces the
    default read timeout for a caller whose single statement runs longer (the
    vector rebuild's copy) or must fail fast (the supervisor lease).
    `retries=False` sends each statement once: a lease call's time is bounded
    by its two timeouts, not by the driver's retries of them. `session=False`
    sends no session id: a call that timed out here but still runs on the
    server would otherwise lock the session, and the next call fails (373)."""
    import clickhouse_connect
    from clickhouse_connect import common as _cc_common

    from server.settings import settings

    # clickhouse-connect errors on settings not in its built-in list; let the
    # experimental flags in _stmt_experimental_settings through to the server.
    _cc_common.set_setting("invalid_setting_action", "send")

    from server.core.ch_target import connection_kwargs

    driver = clickhouse_connect.get_client(
        **connection_kwargs(),
        # A statement waits for its mutation on every replica; the read
        # timeout must outlast that wait, which the runner bounds itself.
        send_receive_timeout=receive_timeout_s
        or max(300, int(settings.migrate_mutation_wait_s) + 60),
        connect_timeout=connect_timeout_s,
        query_retries=2 if retries else 0,
        autogenerate_session_id=session,
        # UTC whatever the server's <timezone>, as server.core.ch pins it.
        settings={"session_timezone": "UTC"},
        tz_source="server",
    )
    from clickhouse_connect.driver.httpclient import HttpClient

    driver.server_tz = UTC
    if not retries and isinstance(driver, HttpClient):
        driver.http_retries = 0
    # A migration names the `loom` alias; the wrapper names the configured database.
    return cast("Client", NamedClient(driver))


def _reads_loom_table(stmt: str) -> bool:
    """True for a data migration that reads loom tables it may have just
    written — INSERT … SELECT … FROM loom.x, ALTER … UPDATE … WHERE (SELECT …).
    Such a read is sent with
    `select_sequential_consistency = 1` so a boot-time migration on a
    replicated service sees its predecessors' writes."""
    up = " ".join(stmt.upper().split())
    if up.startswith("CREATE MATERIALIZED VIEW"):
        return False
    return "SELECT" in up and " FROM LOOM." in up + " "


def _consistency_settings(stmt: str) -> dict[str, int]:
    return {"select_sequential_consistency": 1} if _reads_loom_table(stmt) else {}


def _stmt_experimental_settings(stmt: str) -> dict[str, int] | None:
    """Per-statement experimental CH flags a migration statement needs.

    Sent ONLY for statements using the feature — attaching a flag to an
    unrelated statement makes some clusters reject that statement too.
    """
    up = stmt.upper()
    settings: dict[str, int] = {}
    if "STATISTICS" in up:
        settings["allow_experimental_statistics"] = 1
    # NLP flag is needed for statements that call STEM/LEMMATIZE directly AND
    # for any ALTER on memory_embeddings: content_excerpt_stem is a
    # MATERIALIZED stem() column, and CH re-validates every materialized
    # expression when the table is altered, so the ALTER fails with code 344
    # even though its own text never names the function.
    if "STEM(" in up or "LEMMATIZE(" in up or ("ALTER TABLE" in up and "MEMORY_EMBEDDINGS" in up):
        settings["allow_experimental_nlp_functions"] = 1
    if "VECTOR_SIMILARITY" in up or "'HNSW'" in up:
        settings["allow_experimental_vector_similarity_index"] = 1
    return settings or None


# Settings that make a statement wait for its background work on every
# replica. Sent only where system.settings lists them as changeable.
_SYNC_SETTINGS = (
    "alter_sync",
    "mutations_sync",
    "lightweight_deletes_sync",
    "optimize_throw_if_noop",
)
_MUTATING_ALTER = re.compile(
    r"\b(?:UPDATE|DELETE|MATERIALIZE|CLEAR|APPLY|DROP\s+COLUMN|MODIFY\s+(?:COLUMN|TTL|STATISTICS))\b"
)


def _sync_settings(stmt: str, accepted: frozenset[str], level: int = 2) -> dict[str, int]:
    """The wait settings for one statement, by its kind: an ALTER waits on
    the replicas, a mutating one for its mutation too; OPTIMIZE waits and a
    no-op merge is not an error."""
    up = " ".join(stmt.upper().split())
    want: dict[str, int] = {}
    if up.startswith("ALTER "):
        want["alter_sync"] = level
        if _MUTATING_ALTER.search(up):
            want["mutations_sync"] = level
    elif up.startswith("DELETE FROM "):
        want["lightweight_deletes_sync"] = level
    elif up.startswith("OPTIMIZE "):
        want["alter_sync"] = level
        want["optimize_throw_if_noop"] = 0
    return {k: v for k, v in want.items() if k in accepted}


@dataclass(frozen=True)
class SyncPolicy:
    """Which wait settings this server takes, and at what level: 3 waits for
    active replicas only, 2 for all of them (the fallback where 3 is unknown)."""

    accepted: frozenset[str] = frozenset()
    level: int = 2

    def settings_for(self, stmt: str) -> dict[str, int]:
        return _sync_settings(stmt, self.accepted, self.level)


NO_SYNC = SyncPolicy()


def _sync_level(ch: Client) -> int:
    """3 where the server accepts `alter_sync = 3`, else 2."""
    try:
        ch.query("SELECT 1", settings={"alter_sync": 3})
        return 3
    except Exception:  # noqa: BLE001 — an older server rejects the value; 2 is the fallback
        return 2


def sync_policy(ch: Client) -> SyncPolicy:
    """The run's wait settings, probed once."""
    accepted = _sync_settings_accepted(ch)
    return SyncPolicy(accepted, _sync_level(ch) if accepted else 2)


def _sync_settings_accepted(ch: Client) -> frozenset[str]:
    """Which wait settings this user may send. Probed once per run; a
    setting the server does not know or holds readonly is left out."""
    names = ", ".join(f"'{n}'" for n in _SYNC_SETTINGS)
    try:
        rows = ch.query(
            f"SELECT name FROM system.settings WHERE name IN ({names}) AND readonly = 0"
        ).result_rows
    except Exception as e:  # noqa: BLE001 — without them the mutation check still gates
        print(f"  · wait settings: system.settings unreadable ({type(e).__name__}); none sent")
        return frozenset()
    accepted = frozenset(str(r[0]) for r in rows)
    missing = [n for n in _SYNC_SETTINGS if n not in accepted]
    if missing:
        print(f"  · wait settings not sent (unknown or readonly here): {', '.join(missing)}")
    return accepted


_MUTATED_TABLE = re.compile(r"^\s*(?:ALTER\s+TABLE|DELETE\s+FROM)\s+loom\.(\w+)", re.I)

_PENDING_MUTATIONS_SQL = (
    "SELECT table, mutation_id, command, latest_fail_reason FROM system.mutations "
    "WHERE database = currentDatabase() AND table IN {tables:Array(String)} AND NOT is_done "
    "ORDER BY create_time LIMIT 100"
)


class MutationError(RuntimeError):
    """A mutation a migration started failed or outlived the wait."""


def _mutated_tables(sql: str) -> list[str]:
    return sorted({m.group(1) for s in split_statements(sql) if (m := _MUTATED_TABLE.match(s))})


def _pending_mutations(ch: Client, tables: list[str]) -> list[tuple[str, str, str, str]]:
    rows = ch.query(_PENDING_MUTATIONS_SQL, parameters={"tables": tables}).result_rows
    return [(str(r[0]), str(r[1]), str(r[2]), str(r[3] or "")) for r in rows]


def _await_mutations(
    ch: Client,
    tables: list[str],
    before: set[tuple[str, str]],
    *,
    wait_s: float,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> None:
    """Block until the mutations this file started are done. Mutations that
    were already pending before the file ran (`before`) are not this file's."""
    started, poll, announced = clock(), 0.5, False
    while True:
        rows = [r for r in _pending_mutations(ch, tables) if (r[0], r[1]) not in before]
        failed = next((r for r in rows if r[3]), None)
        if failed is not None:
            raise MutationError(
                f"mutation {failed[1]} on loom.{failed[0]} ({failed[2][:120]}) failed: "
                f"{failed[3][:400]}"
            )
        if not rows:
            return
        if clock() - started >= wait_s:
            ids = ", ".join(f"loom.{t} {m}" for t, m, _, _ in rows[:5])
            raise MutationError(
                f"{len(rows)} mutation(s) still running after {wait_s:.0f} s ({ids}); "
                "not recorded — rerun once system.mutations shows them done"
            )
        if not announced:
            announced = True
            print(f"    … waiting for {len(rows)} mutation(s) on {', '.join(tables)}")
        sleep(poll)
        poll = min(poll * 2, 5.0)


# The ledger read fails with one of these on a fresh cluster (no `loom` database
# or no `_migrations` table yet). Any other failure is not "nothing applied".
_FRESH_CLUSTER_CODES = frozenset({60, 81})  # UNKNOWN_TABLE, UNKNOWN_DATABASE


def applied_migrations(ch: Client) -> dict[str, str]:
    """Return {filename: checksum} of already-applied migrations.

    Empty on a fresh cluster (the ledger does not exist yet). Any other error
    propagates: reading a transient failure as an empty ledger would re-run
    every migration against a live database.
    """
    try:
        rows = ch.query("SELECT filename, checksum FROM loom._migrations FINAL").result_rows
    except Exception as e:
        if _error_code(e) in _FRESH_CLUSTER_CODES:
            return {}
        raise
    out: dict[str, str] = {}
    for r in rows:
        cs = r[1]
        if isinstance(cs, bytes):
            cs = cs.decode("ascii", errors="replace").rstrip("\x00")
        elif isinstance(cs, str):
            cs = cs.rstrip("\x00")
        out[r[0]] = cs
    return out


def _recorded_by(ch: Client, filename: str) -> str | None:
    """Who the ledger already credits for `filename`, or None. Read with
    sequential consistency so a sibling's record on another replica counts."""
    rows = ch.query(
        "SELECT applied_by FROM loom._migrations FINAL WHERE filename = {f:String}",
        parameters={"f": filename},
        settings={"select_sequential_consistency": 1},
    ).result_rows
    return str(rows[0][0]) if rows else None


def _record(ch: Client, filename: str, checksum: str) -> None:
    # The belt under the lease: re-read the ledger for this filename first. Two appliers
    # that both ran the file (the lease table absent) write one row, not two.
    try:
        by = _recorded_by(ch, filename)
    except Exception:  # noqa: BLE001 — the ledger read is a belt, not a gate
        by = None
    if by is not None:
        print(f"    ~ ledger already holds {filename} (applied_by {by}); not recorded again")
        return
    # Explicit column_names so the server's DEFAULT now() fills applied_at;
    # clickhouse-connect's dict-row path requires the column set to match
    # the schema exactly, which it doesn't (no applied_at in our payload).
    ch.insert(
        "loom._migrations",
        [[filename, checksum, socket.gethostname()[:128]]],
        column_names=["filename", "checksum", "applied_by"],
    )


@dataclass(frozen=True)
class LedgerReport:
    """The ledger (`loom._migrations`) against the files in the repo.

    `pending`: repo files the ledger lacks (applied next boot / `loom migrate`).
    `baselined`: pending baselines recorded without running (the ledger holds
    the files they replace). `superseded`: ledger rows a baseline replaces.
    `ledger_only`: ledger filenames nothing in the repo accounts for — a file
    renumbered or renamed AFTER it was applied somewhere; the checksum check
    cannot see them because there is no file to hash. `drift`: applied files
    whose text no longer hashes to the recorded checksum. `stranded`:
    baselines this ledger holds only part of (see `journal.LedgerClasses`).
    `renamed`: (old, new) for a ledger row whose recorded checksum is a repo
    file's under another name — the same text renumbered after it ran (a
    branch migration renumbered past main's). Not ledger-only: a pending
    `new` is recorded without running, since its statements already ran.
    Only a renumber counts (same text, same name after the number), so a text
    repeated under another name runs again.
    """

    pending: tuple[str, ...]
    ledger_only: tuple[str, ...]
    drift: tuple[str, ...]
    baselined: tuple[str, ...] = ()
    superseded: tuple[str, ...] = ()
    stranded: tuple[str, ...] = ()
    renamed: tuple[tuple[str, str], ...] = ()

    @property
    def clean(self) -> bool:
        return not self.ledger_only and not self.drift and not self.stranded


def _unnumbered(name: str) -> str:
    return re.sub(r"^\d+_", "", name)


def ledger_report(applied: dict[str, str], files: list[Path]) -> LedgerReport:
    by_name = {f.name: f for f in files}
    classes = classify_ledger([f.name for f in files], applied, baseline_map(files))
    drift = tuple(
        name
        for name, f in by_name.items()
        if name in applied and applied[name] and _checksum(f.read_text()) != applied[name]
    )
    # A renumber keeps the text and the name after the number: a text repeated on
    # purpose under another name is not taken for the copy that ran.
    by_key: dict[tuple[str, str], str] = {}
    for f in files:
        by_key.setdefault((_checksum(f.read_text()), _unnumbered(f.name)), f.name)
    renamed = tuple(
        (old, by_key[(applied[old], _unnumbered(old))])
        for old in classes.ledger_only
        if applied.get(old) and (applied[old], _unnumbered(old)) in by_key
    )
    gone = {old for old, _new in renamed}
    return LedgerReport(
        pending=classes.pending + classes.baselined,
        ledger_only=tuple(n for n in classes.ledger_only if n not in gone),
        renamed=renamed,
        drift=drift,
        baselined=classes.baselined,
        superseded=classes.superseded,
        stranded=classes.stranded,
    )


def pending_names(mig_dir: str | Path | None = None) -> list[str]:
    """The repo's migration files the service's ledger lacks — what a process that skips the
    boot step is serving without. One read of `loom._migrations`, no DDL. Fail-open: a
    missing directory, an unreachable service or an unreadable ledger yields [] and a
    WARNING, so the caller's boot never depends on this check."""
    try:
        d = Path(mig_dir) if mig_dir is not None else MIGRATIONS_DIR
        files = sorted_migrations(d.glob("*.sql"))
        if not files:
            return []
        return list(ledger_report(applied_migrations(client()), files).pending)
    except Exception as e:  # noqa: BLE001 — a diagnostic, never a boot failure
        logging.getLogger(__name__).warning("pending-migration check skipped: %s", e)
        return []


def print_ledger_report(report: LedgerReport, *, applied_count: int, file_count: int) -> None:
    print(
        f"  ledger: {applied_count} applied · repo: {file_count} file(s) · "
        f"pending: {len(report.pending)} · ledger-only: {len(report.ledger_only)} · "
        f"checksum drift: {len(report.drift)}"
        + (f" · superseded by a baseline: {len(report.superseded)}" if report.superseded else "")
    )
    for name in report.baselined:
        print(f"  · {name}: a baseline this ledger already covers — recorded without running")
    for old, new in report.renamed:
        print(f"  · {old} is {new} under an earlier name (same SHA-256)")
    for name in report.stranded:
        print(
            f"  ✗ {name}: this ledger holds only part of what the baseline replaces — apply "
            "the missing files with a build from before the baseline, then migrate again"
        )
    for name in report.ledger_only:
        print(f"  ⚠ ledger-only: {name} — applied under a filename the repo no longer has")
    for name in report.drift:
        print(f"  ⚠ checksum drift: {name} — file edited after it was applied")


_TOLERATE_RE = re.compile(
    r"^--[ \t]*loom:[ \t]*tolerate-error[ \t]+(\d+)[ \t]*(?:--.*)?$", re.MULTILINE
)


def _tolerated_errors(sql: str) -> set[int]:
    """Error codes a migration file declares survivable, one per directive line
    `-- loom: tolerate-error <code>`. A statement the server rejects with one
    of them is reported and skipped, and the file still records as applied.
    For DDL only some servers accept."""
    return {int(m.group(1)) for m in _TOLERATE_RE.finditer(sql)}


def _codec_settings(ch: Client) -> CodecSettings:
    """How this server takes the codec settings (`CodecSettings`), read before
    any is sent. A fresh table is created coded where this user can have
    `enable_quantized_codec` on, plain where not. Probed once per run."""
    try:
        rows = ch.query(CODEC_SETTINGS_SQL).result_rows
    except Exception:  # noqa: BLE001 — an older server: plain layout
        return NO_CODEC
    return CodecSettings.from_rows(rows)


def _install_vector_width(ch: Client) -> int | None:
    """The width a new coded table takes, or None to create it plain: the width
    the install's coded tables hold (a model chosen in the dashboard leaves
    LOOM_EMBEDDING_DIM behind); None when its vector tables are plain, so one
    install never mixes the layouts (`scripts/ops/vector_codec_rebuild.py`
    moves it); on a fresh install, LOOM_EMBEDDING_DIM."""
    from server.core.schema.vector_codec_ddl import CODED_TABLES, codec_width
    from server.core.vectors.codes_state import written_by_served_model
    from server.settings import settings

    rows = ch.query(
        "SELECT table, name, compression_codec FROM system.columns "
        "WHERE database = currentDatabase() AND table IN {tables:Array(String)}",
        parameters={"tables": sorted(CODED_TABLES)},
    ).result_rows
    widths: Counter[int] = Counter()
    plain = False
    for table, name, codec in rows:
        if name not in CODED_TABLES[str(table)][0] or not written_by_served_model(str(table)):
            continue
        width = codec_width(str(codec))
        if width is None:
            plain = True
        else:
            widths[width] += 1
    if len(widths) > 1:
        print(f"  ! coded vector tables hold several widths {sorted(widths)}: a rebuild stopped")
    if widths:
        return widths.most_common(1)[0][0]
    return None if plain else int(settings.embedding_dim)


def _new_table_layout(ch: Client, codec: CodecSettings) -> tuple[int | None, str]:
    """The width new vector tables are coded at (None: plain), and a line saying why."""
    from server.core.schema.vector_codec_ddl import CHAIN_SETTING, CODEC_SETTING, codable

    if not codec.listed(CODEC_SETTING):
        return (
            None,
            "vector tables created plain: no Quantized codec here (it needs 26.8 or later)",
        )
    if not codec.codec:
        return None, (
            f"vector tables created plain: {CODEC_SETTING} is held at 0 here; "
            "scripts/ops/vector_codec_rebuild.py codes them once it is on"
        )
    width = _install_vector_width(ch)
    if width is None:
        return None, "vector tables created plain, as the install's existing ones are"
    if not codable(width):
        return None, (
            f"vector tables created plain: width {width} is not a multiple of 8, "
            "which the Quantized codec needs"
        )
    if not codec.chain:
        return width, (
            "Quantized codec available: fresh vector tables are created coded, with no "
            f"general codec after it ({CHAIN_SETTING} is held at 0 here)"
        )
    return width, "Quantized codec available: fresh vector tables are created coded"


DIMENSIONS_SETTING = "allow_dimensions_outside_sorting_key"
_AGGREGATING_CREATE = re.compile(
    r"^\s*CREATE\s+TABLE\b[^;]*?\bENGINE\s*=\s*(?:Shared|Replicated)?"
    r"(?:Aggregating|Summing|Coalescing)MergeTree\b",
    re.IGNORECASE,
)
_TABLE_SETTINGS = re.compile(r"^SETTINGS\s+", re.MULTILINE)


def dimensions_rewrite(stmt: str) -> str:
    """`stmt` with `allow_dimensions_outside_sorting_key = 1` among its table
    settings when it creates an aggregating table. Such a table keeps plain
    columns outside its sort key (`df_added_at` next to the aggregate states);
    servers that know this MergeTree setting default it to 0 and refuse the
    CREATE (code 36). Any other statement, an ALTER included, is returned
    unchanged. Pure."""
    if not _AGGREGATING_CREATE.match(stmt) or DIMENSIONS_SETTING in stmt:
        return stmt
    found = list(_TABLE_SETTINGS.finditer(stmt))
    if found:
        at = found[-1].end()
        return f"{stmt[:at]}{DIMENSIONS_SETTING} = 1, {stmt[at:]}"
    return f"{stmt.rstrip()}\nSETTINGS {DIMENSIONS_SETTING} = 1"


def _dimensions_setting_known(ch: Client) -> bool:
    """Whether this server has the MergeTree setting `dimensions_rewrite`
    names. Servers without it (26.5, and any that never check) get the
    statements unchanged."""
    try:
        rows = ch.query(
            f"SELECT count() FROM system.merge_tree_settings WHERE name = '{DIMENSIONS_SETTING}'"
        ).result_rows
    except Exception:  # noqa: BLE001 - an unreadable system table leaves statements unchanged
        return False
    return bool(rows and rows[0][0])


# ClickHouse takes DEFINER only directly before SQL SECURITY, so the match is anchored
# there: the same words in a string literal or a comment are left alone.
_DEFINER_RE = re.compile(
    r"\bDEFINER\s*=\s*(?!CURRENT_USER\b)(?:`[^`]+`|\w+)(?=\s+SQL\s+SECURITY\b)", re.IGNORECASE
)


def definer_rewrite(stmt: str) -> str:
    """A view's `DEFINER = <user>` names whoever dumped the schema (`loom` on the pinned
    server the dump comes from); a server without that user refuses the view
    (UNKNOWN_USER). The view is created under the user running the migration instead,
    the definer every existing view on such a server already has. A double-quoted name is
    not matched and passes through, so the statement fails loudly (UNKNOWN_USER)."""
    return _DEFINER_RE.sub("DEFINER = CURRENT_USER", stmt)


def _apply_file(
    ch: Client,
    path: Path,
    *,
    dry: bool,
    nlp_ok: bool = True,
    coded: bool = False,
    codec_dim: int = 0,
    codec: CodecSettings = NO_CODEC,
    sync: SyncPolicy = NO_SYNC,
    dims: bool = False,
) -> None:
    sql = path.read_text()
    stmts = split_statements(sql)
    tolerated = _tolerated_errors(sql)
    rebuilds = table_rebuild.targets_in(sql, stmts)
    if rebuilds:
        table_rebuild.preflight(ch, sql)  # fails before any change when a table holds rows
    if dry:
        print(f"    {len(stmts)} statement(s) — dry-run, not executed")
        return
    for i, stmt in enumerate(stmts, 1):
        head = " ".join(stmt.split()[:5])
        print(f"    [{i}/{len(stmts)}] {head} …")
        if stmt in rebuilds:
            table_rebuild.apply(ch, rebuilds[stmt], stmt)
            continue
        codec_settings: dict[str, int] = {}
        if coded:
            from server.core.schema.vector_codec_ddl import rewrite_statement

            rewritten = rewrite_statement(stmt, codec_dim)
            if rewritten is None:
                print("    ~ skipped: restates a coded vector column")
                continue
            if rewritten != stmt:
                print("    ~ vector columns created with the Quantized codec")
            stmt, codec_settings = codec.statement(rewritten)
        stmt = definer_rewrite(stmt)
        if dims:
            stmt = dimensions_rewrite(stmt)
        settings = _stmt_experimental_settings(stmt) or {}
        settings.update(_consistency_settings(stmt))
        settings.update(sync.settings_for(stmt))
        settings.update(codec_settings)
        if not nlp_ok:
            # The server forbids enabling NLP functions (ClickHouse Cloud),
            # and there it accepts stem() without the flag.
            settings.pop("allow_experimental_nlp_functions", None)
        try:
            ch.command(stmt, settings=settings or None)
        except Exception as e:
            code = _error_code(e)
            if code is None or code not in tolerated:
                raise
            print(f"    ~ skipped: server rejected it with code {code}, which this file tolerates")


_NLP_SETTING = "allow_experimental_nlp_functions"


def _nlp_available(ch: Client) -> bool:
    """Whether the server will run experimental NLP functions. False on
    ClickHouse Cloud, where allow_experimental_nlp_functions is readonly.

    `system.settings` answers first, so a readonly flag costs no refused
    statement in the query log. Where the flag is writable a lemmatize probe
    decides (OSS may lack the dictionary). Every outcome is one line, never a
    traceback."""
    try:
        rows = ch.query(
            f"SELECT value, readonly FROM system.settings WHERE name = '{_NLP_SETTING}'"
        ).result_rows
    except Exception as e:  # noqa: BLE001 — an older server without the setting
        print(f"  · NLP functions: system.settings has no {_NLP_SETTING} ({type(e).__name__})")
        return False
    if rows and int(rows[0][1] or 0) == 1 and str(rows[0][0]).strip() in ("0", "false", ""):
        print(f"  · NLP functions: {_NLP_SETTING} is readonly at 0 on this server (Cloud); skipped")
        return False
    try:
        ch.command(
            "SELECT lemmatize('en', 'tests')",
            settings={_NLP_SETTING: 1},
        )
        return True
    except Exception as e:  # noqa: BLE001 — a refused probe means no NLP columns, never a failed migrate
        print(f"  · NLP functions: lemmatize probe refused ({type(e).__name__}); skipped")
        return False


LEASE_MIGRATION = "000_migrations.sql"


def _lease_config() -> LeaseConfig:
    """The cross-container lease's knobs from Settings."""
    from server.settings import settings as _s

    return LeaseConfig(
        ttl_s=int(_s.boot_migrate_lease_ttl_s),
        wait_s=float(_s.boot_migrate_lease_wait_s),
    )


Lease = KeeperLock


def _heartbeat_client(cfg: LeaseConfig) -> Client:
    """The migration lock heartbeat's own client: no session and no retries, each
    call bounded well inside the time left before the lock counts as lost."""
    short = max(1, int(cfg.lose_after / 4))
    return client(receive_timeout_s=short, connect_timeout_s=short, retries=False, session=False)


def _acquire_lease(ch: Client) -> Lease:
    """Take the cross-container KeeperMap lock before applying. The lock's
    `outcome` is `held` or `timeout`; KeeperRequired on a server without a Keeper."""
    cfg = _lease_config()
    from server.settings import settings as _s

    commit = str(_s.build_commit)
    lock = KeeperLock(ch, cfg=cfg, build_commit=commit)
    try:
        bootstrap_keeper_lock(ch, wait_s=cfg.wait_s, poll_s=cfg.poll_s)
    except KeeperUnavailable as e:
        print(f"  · migration lock: {e}", file=sys.stderr)  # the boot retries
        lock.outcome, lock.waited_s = "timeout", cfg.wait_s
        return lock
    if lock.acquire() == "unleased":
        if lock.error_code in NO_KEEPER_CODES:
            raise KeeperRequired(lock.error_detail)
        # A refusal from the Keeper (a privilege, a bad argument): the boot retries.
        print(f"  · migration lock: {lock.error_detail}", file=sys.stderr)
        lock.outcome, lock.waited_s = "timeout", cfg.wait_s
        return lock
    print(f"  · migration lock: KeeperMap {LOCK_TABLE}")
    lock.start_heartbeat(lambda: _heartbeat_client(cfg))
    print(
        f"  · migration lock held as {lock.holder}"
        + (f" after waiting {lock.waited_s:.1f} s" if lock.waited_s >= 1 else "")
    )
    return lock


def _run_file(
    ch: Client,
    f: Path,
    *,
    nlp_ok: bool,
    coded: bool,
    sync: SyncPolicy,
    dims: bool = False,
    codec_dim: int = 0,
    codec: CodecSettings = NO_CODEC,
) -> None:
    """Apply one file, then wait for the mutations it started."""
    from server.settings import settings as _s

    tables = _mutated_tables(f.read_text())
    before = {(r[0], r[1]) for r in _pending_mutations(ch, tables)} if tables else set()
    _apply_file(
        ch,
        f,
        dry=False,
        nlp_ok=nlp_ok,
        coded=coded,
        codec_dim=codec_dim,
        codec=codec,
        sync=sync,
        dims=dims,
    )
    if tables:
        _await_mutations(ch, tables, before, wait_s=float(_s.migrate_mutation_wait_s))


def _apply_pending(
    ch: Client,
    to_apply: list[Path],
    *,
    dry: bool,
    lease: Lease | None,
    baselined: frozenset[str] = frozenset(),
    renamed: frozenset[str] = frozenset(),
) -> int:
    """Apply `to_apply` in order, recording each; 1 on the first failure. A
    file in `baselined` or `renamed` (its text already ran under an older
    name) is recorded without running."""
    from server.core.schema.vector_codec_ddl import creates_coded_table

    # Probe NLP support once: it decides whether the NLP experimental flag is
    # sent at all (ClickHouse Cloud forbids it).
    nlp_ok = _nlp_available(ch)
    codec = _codec_settings(ch)
    width, layout = _new_table_layout(ch, codec)
    coded, codec_dim = width is not None, width or 0
    dims = _dimensions_setting_known(ch)
    sync = NO_SYNC if dry else sync_policy(ch)
    if sync.accepted:
        scope = "active replicas" if sync.level == 3 else "all replicas"
        print(f"  · waits: level {sync.level} ({scope})")
    if any(creates_coded_table(f.read_text()) for f in to_apply):
        print(f"  · {layout}")
    if dims:
        print(f"  · aggregating tables are created with {DIMENSIONS_SETTING} = 1")
    for f in to_apply:
        if f.name in baselined:
            print(f"  ≡ {f.name}: baseline of a schema this database already has — recorded")
            if not dry:
                _record(ch, f.name, _checksum(f.read_text()))
            continue
        if f.name in renamed:
            print(f"  ≡ {f.name}: already ran under an older name (same SHA-256) — recorded")
            if not dry:
                _record(ch, f.name, _checksum(f.read_text()))
            continue
        if dry:
            print(f"  → {f.name}  sha256 {_checksum(f.read_text())[:12]}…  pending")
        else:
            print(f"  → {f.name}")
        try:
            if dry:
                _apply_file(
                    ch, f, dry=True, nlp_ok=nlp_ok, coded=coded, codec_dim=codec_dim, codec=codec
                )
            else:
                _run_file(
                    ch,
                    f,
                    nlp_ok=nlp_ok,
                    coded=coded,
                    sync=sync,
                    dims=dims,
                    codec_dim=codec_dim,
                    codec=codec,
                )
                _record(ch, f.name, _checksum(f.read_text()))
                print(f"  ✓ {f.name}")
        except Exception as e:  # noqa: BLE001 — report any migration failure and stop
            print(f"  ✗ {f.name}: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        if lease is not None:
            lease.renew_if_due()
            if lease.outcome == "held" and not lease.held:
                print("  ✗ migration lock lost; stopping so the boot retries", file=sys.stderr)
                return 1
    return 0


def _apply_under_lease(ch: Client, files: list[Path], to_apply: list[Path]) -> int:
    """The non-dry apply path: take the lease, re-read the ledger under it —
    a sibling container may have applied the same files while this process
    waited — then apply what is still pending and release."""
    for f in to_apply:
        if f.name == LEASE_MIGRATION:
            # On a fresh database the ledger does not exist yet. Its CREATE IF
            # NOT EXISTS depends on nothing: run it first, unrecorded; it is
            # applied again in order below (a no-op) and recorded then.
            print(f"  · {f.name} is pending: creating the migrator's tables first")
            _apply_file(ch, f, dry=False)
    lease = _acquire_lease(ch)
    if lease.outcome == "timeout":
        print(
            f"  ✗ migration lease still held by another process after "
            f"{lease.waited_s:.0f} s; giving up so the boot retries",
            file=sys.stderr,
        )
        lease.release()
        return 1
    try:
        applied = applied_migrations(ch)
        if lease.held:
            still = [f for f in to_apply if f.name not in applied]
            if not still:
                print(
                    f"  ✓ no pending migrations — {len(to_apply)} applied by another "
                    f"process while this one waited {lease.waited_s:.1f} s"
                )
                # A pre-migration schema guard or health read can cache the
                # fresh-cluster UNKNOWN_TABLE answer.  The sibling just made
                # that answer stale, so do not serve it for the next 60 s.
                from server.core.schema.status import reset_cache

                reset_cache()
                return 0
            to_apply = still
        report = ledger_report(applied, files)
        rc = _apply_pending(
            ch,
            to_apply,
            dry=False,
            lease=lease,
            baselined=frozenset(report.baselined),
            renamed=frozenset(new for _old, new in report.renamed),
        )
        if rc == 0:
            # Migration statements use the same CH client whose schema guard
            # may have cached an unreadable/missing ledger before
            # 000_migrations.sql created it.  Clear both the health cache and
            # the guard's derived pending-table set before startup proceeds.
            from server.core.schema.status import reset_cache

            reset_cache()
            print(f"  done — {len(to_apply)} applied")
        return rc
    finally:
        lease.release()


def _check_server(args: argparse.Namespace) -> None:
    """The preflight for what this run does; what a passing check still warns of goes to stderr."""
    from server.core.schema import preflight

    need: preflight.Need = "read" if args.status or args.dry_run else "server"
    server = preflight.require_ready(need=need, journal=Path(args.migrations_dir))
    # A read-only run changes nothing the warnings are about.
    if need == "server" and (warning := preflight.warnings(server)):
        print(f"  ! {warning}", file=sys.stderr)


def main(argv: list[str] | None = None, *, check_server: bool = True) -> int:
    """The `loom migrate` command. `check_server=False` for the server's boot,
    which ran the ClickHouse preflight before its first connect."""
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument(
        "--status",
        action="store_true",
        help="Show applied/pending; do not change anything",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would apply; do not change anything",
    )
    p.add_argument(
        "--strict",
        action="store_true",
        help="With --dry-run/--status: exit 1 when the ledger holds a filename the repo "
        "lacks or an applied file's checksum drifted (deploy preflight / CI)",
    )
    p.add_argument(
        "--target",
        type=int,
        help="Apply migrations through NNN only (e.g. --target 14)",
    )
    p.add_argument(
        "--migrations-dir",
        default=str(MIGRATIONS_DIR),
        help="Directory containing NNN_*.sql files (default: this checkout's journal)",
    )
    args = p.parse_args(argv)

    mig_dir = Path(args.migrations_dir)
    if not mig_dir.is_dir():
        print(f"  ✗ migrations directory not found: {mig_dir}", file=sys.stderr)
        return 2

    files = sorted_migrations(mig_dir.glob("*.sql"))
    if not files:
        print("  ✗ no migration files found", file=sys.stderr)
        return 2

    from server.core.schema import preflight

    try:
        if check_server:
            _check_server(args)
        ch = client()
    except preflight.PreflightError as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 1
    except Exception as e:  # noqa: BLE001 — any connect failure exits with a message
        print(f"  ✗ could not connect to ClickHouse: {e}", file=sys.stderr)
        return 1

    applied = applied_migrations(ch)
    report = ledger_report(applied, files)

    if args.status:
        print(f"  applied: {len(applied)} / total: {len(files)}")
        for f in files:
            mark = "✓" if f.name in applied else " "
            note = ""
            if f.name in report.drift:
                note = "  (CHECKSUM DRIFT — file was edited after apply)"
            print(f"    [{mark}] {f.name}{note}")
        print_ledger_report(report, applied_count=len(applied), file_count=len(files))
        return 0 if (report.clean or not args.strict) else 1

    if args.dry_run or report.stranded:
        print_ledger_report(report, applied_count=len(applied), file_count=len(files))
    if report.stranded:
        # Fail closed: running the baseline would skip the replaced files this
        # database lacks, and recording it would claim their schema.
        return 1

    # The ledger is classified against every file; the target limits only what runs.
    runnable = files if args.target is None else through_target(files, args.target)
    to_apply = [f for f in runnable if f.name not in applied]
    if not to_apply:
        print(f"  ✓ no pending migrations ({len(applied)} already applied)")
        return 0 if (report.clean or not args.strict or not args.dry_run) else 1

    print(f"  {len(to_apply)} migration(s) to apply" + ("  [dry-run]" if args.dry_run else ""))
    if args.dry_run:
        rc = _apply_pending(
            ch,
            to_apply,
            dry=True,
            lease=None,
            baselined=frozenset(report.baselined),
            renamed=frozenset(new for _old, new in report.renamed),
        )
        if rc != 0:
            return rc
        print(f"  dry-run — {len(to_apply)} pending, nothing applied")
        return 0 if (report.clean or not args.strict) else 1
    # The apply path is serialised across containers by the KeeperMap lock; the file lock in
    # server/main.py stays the intra-container fast path.
    try:
        return _apply_under_lease(ch, files, to_apply)
    except KeeperRequired as e:
        print(f"  ✗ {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
