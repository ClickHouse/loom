"""The deduplication settings `CHClient.insert` adds to every write
(`dedup_settings`, `insert_dedup_token`), and the settings a call site passes
for a batched insert (`batched_insert_settings`) or a committed one
(`COMMITTED_INSERT_SETTINGS`).
"""

from __future__ import annotations

import hashlib
from typing import Any

from server.settings import settings

# Insert settings that commit the rows before the insert returns.
COMMITTED_INSERT_SETTINGS: dict[str, Any] = {"async_insert": 0, "wait_for_async_insert": 1}


def batched_insert_settings(*, background: bool = False) -> dict[str, Any]:
    """Settings of a batched insert: an async insert answered once its shared part
    is written, waiting between the live bounds for others to join it. `background`
    is for an insert no request waits on, which takes the longer
    `ch_batched_insert_background_wait_max_ms`. With `ch_batched_inserts` off, a
    copy of the committed settings. Why it is safe:
    docs/architecture/clickhouse.md#batched-inserts."""
    if not settings.ch_batched_inserts:
        return dict(COMMITTED_INSERT_SETTINGS)
    if background:
        wait_max = int(settings.ch_batched_insert_background_wait_max_ms)
    else:
        wait_max = int(settings.ch_batched_insert_wait_max_ms)
    return {
        "async_insert": 1,
        "wait_for_async_insert": 1,
        # Sent so the min bound applies whatever the server profile says; with it
        # off, every flush waits the full max.
        "async_insert_use_adaptive_busy_timeout": 1,
        "async_insert_busy_timeout_min_ms": min(
            int(settings.ch_batched_insert_wait_min_ms), wait_max
        ),
        "async_insert_busy_timeout_max_ms": wait_max,
    }


# Bytes of a str or bytes value the token reads.
_PREFIX = 64


def _token_feed(h: Any, v: Any) -> None:
    # The batch's nonce already makes the token unique, so a long value is fed
    # as its length and a prefix: vectors (thousands of floats per row), and
    # text or bytes (an uploaded original can be megabytes).
    if isinstance(v, (list, tuple)):
        h.update(f"[{len(v)}".encode())
        for x in v[:4]:
            h.update(repr(x).encode())
        h.update(b"]")
    elif isinstance(v, (str, bytes, bytearray)):
        head = v[:_PREFIX]
        h.update(f"{type(v).__name__}{len(v)}:".encode())
        h.update(head.encode() if isinstance(head, str) else bytes(head))
    else:
        h.update(repr(v).encode())


def insert_dedup_token(
    table: str, columns: list[str], rows: list[dict[str, Any]], nonce: str
) -> str:
    """`insert_deduplication_token` for one batch: a sha1 of
    the table, the column list, every row's values and a nonce the caller
    draws once per batch. Every attempt at the same batch — loom's retry
    and the driver's own retry after a peer close — sends the same token,
    so ClickHouse keeps exactly one copy; two batches of identical content
    draw different nonces and both land."""
    h = hashlib.sha1()
    h.update(table.encode())
    h.update(",".join(columns).encode())
    h.update(str(len(rows)).encode())
    for r in rows:
        for c in columns:
            _token_feed(h, r.get(c))
        h.update(b"\n")
    h.update(nonce.encode())
    return h.hexdigest()


def dedup_settings(token: str, base: dict[str, Any] | None) -> dict[str, Any]:
    """`base` plus the token and the dedup switch of its insert mode: the
    synchronous path deduplicates through `insert_deduplicate`, the async
    buffer through `async_insert_deduplicate`. With a token the check is by
    token only, never by content, so the reason the async switch is otherwise
    off (byte-identical event rows in one flush window) does not apply. A token
    the caller set is kept: a replay names its batch so a second attempt drops."""
    out = dict(base or {})
    out.setdefault("insert_deduplication_token", token)
    if out.get("async_insert") == 1:
        out["async_insert_deduplicate"] = 1
    else:
        out["insert_deduplicate"] = 1
    return out


__all__ = [
    "batched_insert_settings",
    "dedup_settings",
    "insert_dedup_token",
]
