"""Compose retention + tiering TTL clauses from global env vars and
per-namespace overrides from loom.namespaces.

Two layers stack: global env vars (LOOM_RETENTION_<TABLE>_DAYS,
LOOM_TIERING_<TABLE>_WARM_DAYS) are the default for any row whose
(org, namespace) has no override; per-namespace overrides are rows in
loom.namespaces with non-zero `retention_days` / `warm_days`.

Each managed table gets ONE `ALTER TABLE … MODIFY TTL`. With overrides
present, the TTL uses `multiIf` to dispatch on (org, namespace) — first
match wins, else the global default. No override and no global env var
means no TTL: a table that still carries one from an earlier pass gets
`ALTER TABLE … REMOVE TTL` (none of the managed tables has a schema TTL).

Per-namespace retention applies uniformly across every managed table;
per-table-per-namespace granularity is deliberately not exposed.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from server.core.ch_names import split_table

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetentionTarget:
    """One table's retention configuration.

    `ttl_column_expr` is the SQL expression the TTL is keyed on —
    usually a column name (`ts`, `created_at`, `ingested_at`), but
    for loom.memory it's `coalesce(expires_at, created_at)` so caller
    expires_at wins over the operator backstop. `retention_days` and
    `warm_days` read the table's two global settings.
    """

    table: str
    ttl_column_expr: str
    retention_days: Callable[[Any], int]
    warm_days: Callable[[Any], int]


# Tables this module manages; each reads its retention_<t>_days and
# tiering_<t>_warm_days setting (server/settings/catalog.py).
TARGETS: tuple[RetentionTarget, ...] = (
    RetentionTarget(
        "loom.audit_log",
        "ts",
        lambda s: s.retention_audit_log_days,
        lambda s: s.tiering_audit_log_warm_days,
    ),
    RetentionTarget(
        "loom.agent_events",
        "ts",
        lambda s: s.retention_agent_events_days,
        lambda s: s.tiering_agent_events_warm_days,
    ),
    RetentionTarget(
        "loom.memory",
        "coalesce(expires_at, created_at)",
        lambda s: s.retention_memory_days,
        lambda s: s.tiering_memory_warm_days,
    ),
    RetentionTarget(
        "loom.memory_embeddings",
        "created_at",
        lambda s: s.retention_memory_embeddings_days,
        lambda s: s.tiering_memory_embeddings_warm_days,
    ),
    RetentionTarget(
        "loom.conversations",
        "ts",
        lambda s: s.retention_conversations_days,
        lambda s: s.tiering_conversations_warm_days,
    ),
    RetentionTarget(
        "loom.chunks",
        "created_at",
        lambda s: s.retention_chunks_days,
        lambda s: s.tiering_chunks_warm_days,
    ),
    RetentionTarget(
        "loom.documents",
        "ingested_at",
        lambda s: s.retention_documents_days,
        lambda s: s.tiering_documents_warm_days,
    ),
)


# Tables a retention TTL must not touch under the Postgres authority: it would delete
# ClickHouse's copy of rows Postgres still serves, or the vectors of live memories.
_PG_HELD = ("loom.memory", "loom.memory_embeddings", "loom.documents")


def _warm_volume_name(settings_obj: Any) -> str:
    """Cluster operator can name the volume anything; default 'warm'."""
    name = settings_obj.tiering_warm_volume_name
    return name.strip() or "warm"


def _sql_quote(s: str) -> str:
    """Single-quote a string literal for embedding in a CH expression.

    org and namespace values come from operator-controlled wire ops,
    but escape defensively — a stray apostrophe must not corrupt the
    ALTER statement.
    """
    return "'" + s.replace("\\", "\\\\").replace("'", "\\'") + "'"


# A row whose namespace has no override and no global default never expires
# (ClickHouse's DateTime ceiling is 2106).
_NEVER = "toDateTime('2099-12-31 23:59:59')"


def _per_namespace(
    overrides: list[dict[str, Any]],
    field: str,
    *,
    global_days: int,
    per_row_expr: Callable[[int], str],
) -> str | None:
    """The TTL expression for one side (retention or tiering): the global
    default alone, or a `multiIf` that gives each namespace with a non-zero
    `field` its own day count (first match wins) and every other row the
    global default. None when neither is set."""
    with_days = [o for o in overrides if int(o.get(field) or 0) > 0]
    if not with_days and global_days <= 0:
        return None
    default = per_row_expr(global_days) if global_days > 0 else _NEVER
    if not with_days:
        return default
    cases: list[str] = []
    for ovr in with_days:
        cases.append(
            f"org = {_sql_quote(ovr['org'])} AND namespace = {_sql_quote(ovr['namespace'])}"
        )
        cases.append(per_row_expr(int(ovr[field])))
    return f"multiIf({', '.join(cases)}, {default})"


def _retention_clause(
    target: RetentionTarget,
    *,
    global_days: int,
    namespace_overrides: list[dict[str, Any]],
) -> str | None:
    """DELETE-side TTL expression; None if nothing should expire."""

    def per_row_expr(days: int) -> str:
        if target.table == "loom.memory":
            # caller expires_at wins; operator N-day backstops rows without one
            return f"coalesce(expires_at, created_at + INTERVAL {days} DAY)"
        return f"{target.ttl_column_expr} + INTERVAL {days} DAY"

    expr = _per_namespace(
        namespace_overrides, "retention_days", global_days=global_days, per_row_expr=per_row_expr
    )
    return None if expr is None else f"{expr} DELETE"


def _tiering_clause(
    target: RetentionTarget,
    *,
    global_days: int,
    namespace_overrides: list[dict[str, Any]],
    warm_volume: str,
) -> str | None:
    """TO VOLUME side, same shape as _retention_clause. Tiering keys on the
    column expression even for memory: moving a caller-expiring row to warm
    before its own expiry is wasteful, and operator tiering is for cost, not
    compliance."""
    expr = _per_namespace(
        namespace_overrides,
        "warm_days",
        global_days=global_days,
        per_row_expr=lambda days: f"{target.ttl_column_expr} + INTERVAL {days} DAY",
    )
    return None if expr is None else f"{expr} TO VOLUME '{warm_volume}'"


def _compose_ttl(
    target: RetentionTarget,
    *,
    retention_days: int,
    tiering_days: int,
    warm_volume: str,
    namespace_overrides: list[dict[str, Any]],
) -> str | None:
    """Compose the full TTL clause (retention + tiering)."""
    retention_part = _retention_clause(
        target,
        global_days=retention_days,
        namespace_overrides=namespace_overrides,
    )
    tiering_part = _tiering_clause(
        target,
        global_days=tiering_days,
        namespace_overrides=namespace_overrides,
        warm_volume=warm_volume,
    )

    parts = []
    if tiering_part:
        parts.append(tiering_part)
    if retention_part:
        parts.append(retention_part)
    if not parts:
        return None
    return "TTL " + ", ".join(parts)


async def _fetch_namespace_overrides(ch: Any) -> list[dict[str, Any]]:
    """One dict per (org, namespace) with a non-zero retention/warm."""
    rows: list[dict[str, Any]] = await ch.query_all(
        """
        SELECT org, namespace, retention_days, warm_days
        FROM loom.namespaces FINAL
        WHERE retention_days > 0 OR warm_days > 0
        ORDER BY org, namespace
        """,
    )
    return rows


def compute_alters(
    settings_obj: Any,
    *,
    namespace_overrides: list[dict[str, Any]] | None = None,
) -> list[tuple[RetentionTarget, str, dict[str, Any]]]:
    """Return (target, alter_sql, debug_params) tuples. Pure function —
    tests pass an explicit namespace_overrides list."""
    ovr_all = namespace_overrides or []
    warm_volume = _warm_volume_name(settings_obj)
    out: list[tuple[RetentionTarget, str, dict[str, Any]]] = []

    from server.core import pg

    for target in TARGETS:
        r_days = max(0, target.retention_days(settings_obj) or 0)
        t_days = max(0, target.warm_days(settings_obj) or 0)
        overrides = ovr_all
        if target.table in (*_PG_HELD, "loom.chunks") and pg.enabled():
            # Postgres holds memory and document heads: a TTL here would delete only
            # ClickHouse's copy (or only its vectors, leaving live memories search no
            # longer finds), and the text would stay in Postgres. A namespace's
            # retention_days leaves a document's chunks with it; LOOM_RETENTION_CHUNKS_DAYS,
            # set alone, expires chunks alone under either authority.
            overrides = [{**o, "retention_days": 0} for o in ovr_all]
            if target.table in _PG_HELD:
                r_days = 0

        ttl_clause = _compose_ttl(
            target,
            retention_days=r_days,
            tiering_days=t_days,
            warm_volume=warm_volume,
            namespace_overrides=overrides,
        )
        if ttl_clause is None:
            continue
        alter_sql = f"ALTER TABLE {target.table} MODIFY {ttl_clause}"
        out.append(
            (
                target,
                alter_sql,
                {
                    "retention_global": r_days,
                    "tiering_global": t_days,
                    "overrides": len(ovr_all),
                },
            )
        )

    return out


async def _formatted(ch: Any, sql: str) -> str:
    """ClickHouse's own canonical single-line form of a statement, so the
    TTL we want and the TTL the table has are compared after the same
    formatter (spacing, parentheses and literal quoting all normalise)."""
    row = await ch.query_one("SELECT formatQuerySingleLine({q:String}) AS f", {"q": sql})
    return str((row or {}).get("f") or "").strip()


async def _current_ttl(ch: Any, table: str) -> str:
    """The TTL clause `table` carries now (after `TTL `, before `SETTINGS`),
    or '' when it has none. Raises when system.tables cannot be read."""
    db, name = split_table(table)
    row = await ch.query_one(
        "SELECT engine_full FROM system.tables "
        "WHERE database = {db:String} AND name = {name:String}",
        {"db": db, "name": name},
    )
    engine_full = str((row or {}).get("engine_full") or "")
    if " TTL " not in engine_full:
        return ""
    current = engine_full.split(" TTL ", 1)[1]
    for stop in (" SETTINGS ", "\nSETTINGS "):
        if stop in current:
            current = current.split(stop, 1)[0]
    return current.strip()


async def current_ttl_matches(ch: Any, table: str, alter_sql: str) -> bool | None:
    """True when `table` already carries exactly the TTL that `alter_sql`
    would set, False when it differs or has none, None when the check
    itself failed (the caller applies it — fail-open).

    A MODIFY TTL is a full-table mutation on ClickHouse
    Cloud (materialize_ttl_after_modify); re-issuing it from every process at
    boot piles up TTL mutations per table and draws code 517 ("previous
    mutations not finished") once a rewrite outlives the next restart."""
    try:
        current = await _current_ttl(ch, table)
        if not current:
            return False
        want = await _formatted(ch, alter_sql)
        have = await _formatted(ch, f"ALTER TABLE {table} MODIFY TTL {current}")
        return bool(want) and want == have
    except Exception as e:  # noqa: BLE001 — the comparison never blocks the ALTER
        log.warning("retention/tiering: TTL comparison failed for %s (%s); applying", table, e)
        return None


MATERIALIZE_OFF = " SETTINGS materialize_ttl_after_modify = 0"


def alter_statement(alter_sql: str) -> str:
    """The statement actually sent for a planned ALTER.

    The planned SQL is kept bare so `current_ttl_matches` can compare it
    with the table's own clause; the executed statement adds
    `materialize_ttl_after_modify = 0`. A MODIFY TTL is then a metadata
    change: rows already past the new TTL leave at their part's next
    merge, or when an operator runs `ALTER TABLE … MATERIALIZE TTL` — not
    in a rewrite of every part per namespace registration."""
    return alter_sql + MATERIALIZE_OFF


# ClickHouse's answer to REMOVE TTL on a table that has none (code 36).
_NO_TTL_TO_REMOVE = "doesn't have any table TTL expression"


async def unfinished_mutations(ch: Any, table: str) -> int | None:
    """Unfinished mutations on `table` (`system.mutations.is_done = 0`), or
    None when the read fails (fail-open: the ALTER is issued).
    An ALTER assigned behind unfinished mutations is refused with
    `Code: 517` once the queue is deep; deferring to the next pass costs a
    retention policy nothing it promised."""
    db, name = split_table(table)
    try:
        row = await ch.query_one(
            "SELECT count() AS n FROM system.mutations "
            "WHERE database = {db:String} AND table = {name:String} AND is_done = 0",
            {"db": db, "name": name},
        )
    except Exception:  # noqa: BLE001 — an unreadable queue never blocks the ALTER
        log.info("retention/tiering: mutation-queue read failed for %s; proceeding", table)
        return None
    if not row:
        return 0
    val = row.get("n") if isinstance(row, dict) else row[0]
    return int(val or 0)


async def apply(
    settings_obj: Any,
    ch: Any | None = None,
    *,
    namespace_overrides: list[dict[str, Any]] | None = None,
    leader: bool,
) -> int:
    """Apply every retention/tiering override against CH, returning the
    count of ALTERs issued. namespace_overrides defaults to a fetch from
    loom.namespaces. Per-ALTER failures are logged + swallowed so one bad
    override can't crash boot.

    Only the process holding the supervisor lease alters
    tables (`leader`), and a table whose TTL already equals the composed clause is skipped —
    a MODIFY TTL rewrites the table on ClickHouse Cloud, so an unchanged
    override must cost nothing. A table with no composed clause that still
    carries a TTL gets REMOVE TTL, so a cleared window stops deleting rows."""
    if not leader:
        log.info(
            "retention/tiering: not the supervisor leader in pid %s; ALTERs skipped",
            os.getpid(),
        )
        return 0
    if ch is None:
        from server.core.ch import get_client

        ch = get_client()

    if namespace_overrides is None:
        namespace_overrides = await _fetch_namespace_overrides(ch)

    planned = {
        target.table: (alter_sql, params)
        for target, alter_sql, params in compute_alters(
            settings_obj, namespace_overrides=namespace_overrides
        )
    }

    issued = 0
    for target in TARGETS:
        if target.table in planned:
            alter_sql, params = planned[target.table]
            if await current_ttl_matches(ch, target.table, alter_sql):
                log.info("retention/tiering: %s TTL unchanged; no ALTER", target.table)
                continue
            statement = alter_statement(alter_sql)
            change = (
                f"global_retention={params['retention_global']}, "
                f"global_warm={params['tiering_global']}, "
                f"per-namespace overrides={params['overrides']}"
            )
        else:
            # A failed read defers the removal to the next pass: REMOVE TTL on
            # a table without one is an error.
            try:
                if not await _current_ttl(ch, target.table):
                    continue
            except Exception as e:  # noqa: BLE001 — an unreadable table is retried next pass
                log.warning(
                    "retention/tiering: TTL lookup failed for %s (%s); not removed",
                    target.table,
                    e,
                )
                continue
            statement = f"ALTER TABLE {target.table} REMOVE TTL"
            change = "no window set; removing its TTL"
        pending = await unfinished_mutations(ch, target.table)
        if pending:
            log.info(
                "retention/tiering: %s has %d unfinished mutation(s); ALTER deferred "
                "to the next reconcile pass",
                target.table,
                pending,
            )
            continue
        log.info("retention/tiering: %s -> %s", target.table, change)
        try:
            await ch.execute(statement)
            issued += 1
        except Exception as e:  # one table's ALTER never stops the others or boot
            if target.table not in planned and _NO_TTL_TO_REMOVE in str(e):
                # an overlapping pass removed it first
                log.info("retention/tiering: %s TTL already removed", target.table)
                continue
            log.exception(
                "retention/tiering: ALTER failed for %s; continuing",
                target.table,
            )
    return issued


__all__ = [
    "MATERIALIZE_OFF",
    "TARGETS",
    "RetentionTarget",
    "alter_statement",
    "apply",
    "compute_alters",
    "current_ttl_matches",
    "unfinished_mutations",
]
