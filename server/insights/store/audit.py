"""Typed reads over the loom.audit_log table.

The row_hash is per-row (SHA-256 of the canonical row), not a chain. This repo
does not compute it — that lives in `server.observability.audit`; appends go
through the audit writer. Reads here: trace by request_id, recent events by
(org, namespace), and the audit-verifier worker's window scan.

Checkpoints (loom.audit_checkpoints) are the one thing this repo writes: the
verifier seals closed windows through `insert_checkpoint` and recounts them
through `window_stats`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict

from server.core.hashing import hash_text
from server.core.repo_base import _CHClient, _default_client
from server.insights.domain.audit_checkpoints import Checkpoint


class AuditRow(BaseModel):
    model_config = ConfigDict(frozen=True)

    ts: datetime
    event_type: str
    org: str
    namespace: str
    actor: str
    subject: str
    request_id: str
    row_hash: str = ""


def _hydrate(row: dict[str, Any]) -> AuditRow:
    return AuditRow(
        ts=row["ts"],
        event_type=row["event_type"],
        org=row["org"],
        namespace=row["namespace"],
        actor=row["actor"],
        subject=row["subject"],
        request_id=row["request_id"],
        # FixedString(64): bytes from the native client, NULs when never set
        row_hash=hash_text(row.get("row_hash")),
    )


async def list_by_request_id(
    request_id: str,
    *,
    ch: _CHClient | None = None,
    limit: int = 100,
) -> list[AuditRow]:
    """Trace every event tagged with this request_id (uses the request_id skip index)."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT ts, event_type, org, namespace, actor,
               subject, request_id, row_hash
        FROM loom.audit_log
        WHERE request_id = {rid:String}
        ORDER BY ts ASC
        LIMIT {limit:UInt32}
        """,
        params={"rid": request_id, "limit": int(limit)},
    )
    return [_hydrate(r) for r in rows]


async def list_page(
    org: str,
    *,
    scope_where: str,
    scope_params: dict[str, Any],
    event_type: str | None = None,
    since: datetime | None = None,
    before: tuple[datetime, str] | None = None,
    limit: int,
    ch: _CHClient | None = None,
) -> list[AuditRow]:
    """One page of an org's events, newest first: at most `limit` rows
    inside the caller-visible `scope_where` (built by the handler's
    visibility decision), strictly older than the keyset `before` =
    (ts, request_id) of the previous page's last row."""
    ch = ch or _default_client()
    where = scope_where
    params: dict[str, Any] = {**scope_params, "org": org, "lim": int(limit)}
    if event_type is not None:
        where += " AND event_type = {et:String}"
        params["et"] = event_type
    if since is not None:
        # clickhouse-connect binds naive datetimes as client-local; pin UTC.
        where += " AND ts >= {since:DateTime64(3)}"
        params["since"] = since if since.tzinfo is not None else since.replace(tzinfo=UTC)
    if before is not None:
        cur_ts, cur_rid = before
        # Keyset via tuple comparison. ts is an explicit UTC literal rather than
        # a bound param to dodge clickhouse-connect's naive-datetime TZ binding.
        if cur_ts.tzinfo is not None:
            cur_ts = cur_ts.astimezone(UTC).replace(tzinfo=None)
        ts_literal = cur_ts.strftime("%Y-%m-%d %H:%M:%S.") + f"{cur_ts.microsecond // 1000:03d}"
        where += (
            f" AND (ts, request_id) < (toDateTime64('{ts_literal}', 3, 'UTC'), {{c_rid:String}})"
        )
        params["c_rid"] = cur_rid
    rows = await ch.query_all(
        f"""
        SELECT ts, event_type, org, namespace, actor, subject,
               request_id, row_hash
        FROM loom.audit_log
        WHERE org = {{org:String}}
              {where}
        ORDER BY ts DESC, request_id DESC
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )
    return [_hydrate(r) for r in rows]


async def verify_window(
    look_back_seconds: int,
    *,
    after: tuple[Any, str, str] | None = None,
    limit: int = 10_000,
    ch: _CHClient | None = None,
) -> list[dict[str, Any]]:
    """One page of raw rows for the audit_verifier's per-row hash check,
    oldest first: at most `limit` rows of the look-back window strictly
    after the keyset `after` = (ts, request_id, row_hash) of the previous
    page's last row. `toYYYYMM(ts) AS partition` lets the worker report a
    break's partition without a second read."""
    ch = ch or _default_client()
    params: dict[str, Any] = {"lb": int(look_back_seconds), "lim": int(limit)}
    keyset = ""
    if after is not None:
        keyset = (
            "AND (ts, request_id, row_hash) > "
            "({a_ts:DateTime64(3)}, {a_rid:String}, {a_hash:FixedString(64)})"
        )
        params.update({"a_ts": after[0], "a_rid": after[1], "a_hash": after[2]})
    return await ch.query_all(
        f"""
        SELECT toYYYYMM(ts) AS partition, ts, event_type, org, namespace,
               actor, subject, request_id, row_hash
        FROM loom.audit_log
        WHERE ts >= toDateTime64(now() - INTERVAL {{lb:UInt32}} SECOND, 3, 'UTC')
          {keyset}
        ORDER BY ts, request_id, row_hash
        LIMIT {{lim:UInt32}}
        """,
        params=params,
    )


TAMPER_EVENT = "audit.tamper_detected"


async def reported_tamper_keys(since_seconds: int, *, ch: _CHClient | None = None) -> set[str]:
    """The break keys the verifier's own `audit.tamper_detected` rows of the
    last `since_seconds` already reported (their subject's `break_keys`), so
    a break is reported once, across passes and restarts. The rows sit on
    the ('-', '-', event_type) prefix of the sort key."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT subject FROM loom.audit_log
        WHERE org = '-' AND namespace = '-' AND event_type = {et:String}
          AND ts >= toDateTime64(now() - INTERVAL {s:UInt32} SECOND, 3, 'UTC')
        """,
        params={"et": TAMPER_EVENT, "s": int(since_seconds)},
    )
    keys: set[str] = set()
    for r in rows:
        try:
            subject = json.loads(str(r.get("subject") or "{}"))
        except ValueError:
            continue
        if isinstance(subject, dict):
            keys.update(str(k) for k in subject.get("break_keys") or [])
    return keys


# ----------------------------------------------------------------------
# Checkpoints over closed windows
# ----------------------------------------------------------------------


async def earliest_audit_ts(*, ch: _CHClient | None = None) -> datetime | None:
    """When the log begins; None for an empty log."""
    ch = ch or _default_client()
    row = await ch.query_one("SELECT min(ts) AS first_ts, count() AS n FROM loom.audit_log")
    if not row or not int(row.get("n") or 0):
        return None
    first = row.get("first_ts")
    return first if isinstance(first, datetime) else None


async def window_stats(
    window_start: datetime, window_end: datetime, *, ch: _CHClient | None = None
) -> tuple[int, str]:
    """(row_count, rows_digest) of the log rows in [window_start, window_end).
    The digest is the ClickHouse mirror of insights.domain.audit_checkpoints.rows_digest."""
    from server.insights.domain.audit_checkpoints import EMPTY_DIGEST, rows_digest_sql

    ch = ch or _default_client()
    row = await ch.query_one(
        f"""
        SELECT count() AS n, {rows_digest_sql("row_hash")} AS digest
        FROM loom.audit_log
        WHERE ts >= toDateTime64({{ws:DateTime}}, 3, 'UTC')
          AND ts <  toDateTime64({{we:DateTime}}, 3, 'UTC')
        """,
        params={"ws": window_start, "we": window_end},
    )
    n = int((row or {}).get("n") or 0)
    digest = str((row or {}).get("digest") or "") if n else EMPTY_DIGEST
    return n, digest or EMPTY_DIGEST


async def latest_checkpoints(limit: int, *, ch: _CHClient | None = None) -> list[Checkpoint]:
    """The most recent `limit` checkpoints, oldest first."""
    ch = ch or _default_client()
    rows = await ch.query_all(
        """
        SELECT window_start, window_end, row_count,
               toString(rows_digest) AS rows_digest,
               toString(prev_checkpoint_hash) AS prev_checkpoint_hash,
               toString(checkpoint_hash) AS checkpoint_hash
        FROM loom.audit_checkpoints
        ORDER BY window_start DESC
        LIMIT {lim:UInt32}
        """,
        params={"lim": int(limit)},
    )
    return [
        Checkpoint(
            window_start=r["window_start"],
            window_end=r["window_end"],
            row_count=int(r["row_count"]),
            rows_digest=str(r["rows_digest"]).rstrip("\x00"),
            prev_checkpoint_hash=str(r["prev_checkpoint_hash"]).rstrip("\x00"),
            checkpoint_hash=str(r["checkpoint_hash"]).rstrip("\x00"),
        )
        for r in reversed(rows)
    ]


async def insert_checkpoint(row: dict[str, Any], *, ch: _CHClient | None = None) -> None:
    ch = ch or _default_client()
    await ch.insert("loom.audit_checkpoints", [row])
