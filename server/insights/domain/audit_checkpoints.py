"""Audit checkpoints: a chain over closed windows, not over rows.

`loom.audit_log` rows carry a per-row SHA-256, which catches a modified row
and nothing else: a deleted row leaves no hash behind to mismatch, and a
per-row chain would fork under stateless concurrent writers.
A checkpoint seals a CLOSED time window after the fact: how many rows it
holds and a digest over the sorted set of their row hashes, chained to the
previous checkpoint. One worker computes it once the window can no longer
receive writes, so the write path needs no coordination, and a later
recount that comes up short is a deletion made visible.

Pure helpers here (window math, digests, classification); SQL lives in
insights/store/audit.py and the worker leg in insights/workers/audit_verifier.py.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

# Digest of a window with no rows: SHA-256 of the empty string, so an empty
# window is still sealed and a row inserted into it later is a mismatch.
EMPTY_DIGEST = hashlib.sha256(b"").hexdigest()


@dataclass(frozen=True)
class Checkpoint:
    window_start: datetime
    window_end: datetime  # exclusive
    row_count: int
    rows_digest: str
    prev_checkpoint_hash: str
    checkpoint_hash: str


def floor_window(ts: datetime, window_seconds: int) -> datetime:
    """Start of the window containing `ts` (naive UTC)."""
    ts = ts.replace(tzinfo=None) if ts.tzinfo is None else ts.astimezone(UTC).replace(tzinfo=None)
    epoch = int(ts.replace(tzinfo=UTC).timestamp())
    return datetime.fromtimestamp(epoch - epoch % window_seconds, tz=UTC).replace(tzinfo=None)


def rows_digest(row_hashes: Iterable[str]) -> str:
    """Order-independent digest of a window's row hashes: SHA-256 over the
    sorted hashes concatenated. Mirrors the ClickHouse expression in
    audit_repo.window_stats, so a Python recount and a CH recount agree."""
    joined = "".join(sorted(h.rstrip("\x00") for h in row_hashes))
    return hashlib.sha256(joined.encode("ascii")).hexdigest()


def rows_digest_sql(column: str = "row_hash") -> str:
    """ClickHouse mirror of :func:`rows_digest`."""
    return f"lower(hex(SHA256(arrayStringConcat(arraySort(groupArray(toString({column})))))))"


def checkpoint_hash(
    window_start: datetime, window_end: datetime, row_count: int, digest: str, prev: str
) -> str:
    payload = "|".join(
        [window_start.isoformat(), window_end.isoformat(), str(int(row_count)), digest, prev]
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()


def seal(
    window_start: datetime,
    window_end: datetime,
    row_count: int,
    digest: str,
    prev: str,
) -> Checkpoint:
    return Checkpoint(
        window_start=window_start,
        window_end=window_end,
        row_count=int(row_count),
        rows_digest=digest or EMPTY_DIGEST,
        prev_checkpoint_hash=prev,
        checkpoint_hash=checkpoint_hash(
            window_start, window_end, int(row_count), digest or EMPTY_DIGEST, prev
        ),
    )


def windows_to_seal(
    last_end: datetime | None,
    first_row_ts: datetime | None,
    now: datetime,
    *,
    window_seconds: int,
    grace_seconds: int,
    max_windows: int,
) -> list[tuple[datetime, datetime]]:
    """Closed windows after the last checkpoint whose end is at least
    `grace_seconds` in the past (so late async inserts have landed), oldest
    first, capped. With no checkpoint yet, sealing starts at the window of
    the earliest audit row; with no rows at all, nothing to seal."""
    if last_end is None:
        if first_row_ts is None:
            return []
        start = floor_window(first_row_ts, window_seconds)
    else:
        start = last_end
    cutoff = now.replace(tzinfo=None) - timedelta(seconds=grace_seconds)
    out: list[tuple[datetime, datetime]] = []
    while len(out) < max_windows:
        end = start + timedelta(seconds=window_seconds)
        if end > cutoff:
            break
        out.append((start, end))
        start = end
    return out


def classify(sealed: Checkpoint, observed_count: int, observed_digest: str) -> str | None:
    """What a recount says about a sealed window. None = intact.
    'deletion'      fewer rows than sealed — rows are gone.
    'modification'  same count, different digest — a row's hash changed.
    'late_insert'   more rows than sealed — rows landed after the seal
                    (a backfill or a very late async insert), not tampering
                    by itself, but the seal no longer describes the window."""
    if observed_count < sealed.row_count:
        return "deletion"
    if observed_count > sealed.row_count:
        return "late_insert"
    if (observed_digest or EMPTY_DIGEST) != sealed.rows_digest:
        return "modification"
    return None


def chain_breaks(checkpoints: list[Checkpoint]) -> list[dict[str, Any]]:
    """Checkpoints in window order: each must recompute its own hash and
    point at its predecessor's. Returns one entry per break."""
    out: list[dict[str, Any]] = []
    prev_hash = None
    for cp in checkpoints:
        recomputed = checkpoint_hash(
            cp.window_start, cp.window_end, cp.row_count, cp.rows_digest, cp.prev_checkpoint_hash
        )
        ws = cp.window_start.isoformat()
        if recomputed != cp.checkpoint_hash:
            out.append({"window_start": ws, "reason": "checkpoint_hash mismatch"})
        if prev_hash is not None and cp.prev_checkpoint_hash != prev_hash:
            out.append({"window_start": ws, "reason": "chain link mismatch"})
        prev_hash = cp.checkpoint_hash
    return out
