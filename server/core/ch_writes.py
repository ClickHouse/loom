"""What `CHClient.insert` applies to a write before it is sent: the
fire-and-forget dedup rule (`normalize_insert_settings`) and the deduplication
token that makes a retried batch land once.
"""

from __future__ import annotations

import hashlib
from typing import Any


def normalize_insert_settings(extra: dict[str, Any] | None) -> dict[str, Any] | None:
    """Fire-and-forget inserts (`async_insert = 1`) gain `async_insert_deduplicate
    = 0` unless the call site set it: the Cloud profile defaults it to 1, which
    would silently drop a byte-identical event row landing in the same flush
    window. Committed writes (`async_insert = 0`) pass through unchanged."""
    if not extra:
        return extra
    out = dict(extra)
    if out.get("async_insert") == 1:
        out.setdefault("async_insert_deduplicate", 0)
    return out


def _token_feed(h: Any, v: Any) -> None:
    # Vectors are thousands of floats per row: their length and a prefix
    # identify the batch as well as the whole would, at a fraction of the cost.
    if isinstance(v, (list, tuple)):
        h.update(f"[{len(v)}".encode())
        for x in v[:4]:
            h.update(repr(x).encode())
        h.update(b"]")
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
    off (byte-identical event rows in one flush window) does not apply."""
    out = dict(base or {})
    out["insert_deduplication_token"] = token
    if out.get("async_insert") == 1:
        out["async_insert_deduplicate"] = 1
    else:
        out["insert_deduplicate"] = 1
    return out


__all__ = [
    "dedup_settings",
    "insert_dedup_token",
    "normalize_insert_settings",
]
