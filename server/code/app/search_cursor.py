"""The next page is a point read of what was already ranked.

A repeated `code.search` is most often a caller asking for more than the
first page showed; recomputing it re-runs the embedding call, the vector leg
and the lexical scan to re-derive a ranking the server already computed and
threw away past `top_k`.

This module keeps the fused, ranked POOL of a computed search (up to `pool_cap` unit ids
with paths and scores, never bodies) in `loom.code_search_memo` — the fleet-tier memo
table — under the key `cursor:<kind>:<run id>`, scoped by (org, namespace) in the table
key. The caller gets an opaque `next_cursor`; the next page decodes it, point-reads the
pool row, fetches the bodies of the next page's units by full sort key (every column
except the vector) and budgets them. Zero provider calls, no leg scans, nothing
in-process (a process cache would miss whenever another worker serves the page).

  cursor   base64url JSON {v, k, r, t, o, p, c, s}: version, kind
           (s|g), pool row id, issue time, offset into the pool, the hash
           of the request fields that shape the RANKING, the indexed
           commit at issue, and a keyed checksum. A tampered cursor is a
           400; an expired one (read-side TTL, same as the memo) or one
           whose fire-and-forget pool row has not landed falls back to a
           disclosed re-search.
  exclude  `exclude_unit_ids` is the caller's seen-set. On a computed
           search it is applied to the fused list after fusion and before
           diversification; on a page it skips those ids while walking the
           pool from the offset, so pool positions are stable.

Grep pages are the stored match lines themselves: the extraction loop
already had them and the row is small, so a grep page is one point read.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from server.code.domain.units import CodeUnit, unit_from_chunk
from server.core.background import TaskSet
from server.core.datetimes import naive_utc_now
from server.embedding.normalize import normalize_whitespace

logger = logging.getLogger("loom.code_search_cursor")

CURSOR_VERSION = 1
# Ranking-shaping request fields hashed into the cursor: a page must be
# served from a pool ranked under the same query, prefix, reranker, legs
# and priors. Page size (top_k) and body fill (token_budget, bodies,
# annotate) are deliberately NOT in the hash — they shape the page, not
# the ranking, and the next page uses the request's own values.
_RANKING_FIELDS = (
    "query",
    "path_prefix",
    "expand",
    "legs",
    "user_id",
)


class CursorError(ValueError):
    """The cursor is not one this server issued for this request shape."""


# --------------------------------------------------------------------------
# cursor encoding
# --------------------------------------------------------------------------


def _secret() -> bytes:
    """The HMAC key for cursors: the root secret, which every process of the
    deployment holds. Without one a cursor is neither issued nor accepted: a
    fixed fallback key would let anyone forge one."""
    from server.settings import settings

    if not settings.jwt_secret:
        raise CursorError("cursors need LOOM_JWT_SECRET; this server has none")
    return str(settings.jwt_secret).encode()


def _sign(fields: dict[str, Any]) -> str:
    canon = json.dumps(
        {k: fields[k] for k in ("v", "k", "r", "t", "o", "p", "c")},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return hmac.new(_secret(), canon, hashlib.sha256).hexdigest()[:16]


def params_hash(inp: Any, *, rerank: str, embedding_model: str) -> str:
    parts: dict[str, Any] = {f: getattr(inp, f, None) for f in _RANKING_FIELDS}
    parts["query"] = normalize_whitespace(str(parts.get("query") or ""))
    parts["rerank"] = rerank
    parts["embedding_model"] = embedding_model
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def encode(
    *,
    kind: str,
    run_id: str,
    offset: int,
    params: str,
    indexed_commit: str,
    ts: int | None = None,
) -> str:
    fields: dict[str, Any] = {
        "v": CURSOR_VERSION,
        "k": kind,
        "r": run_id,
        "t": int(ts or time.time()),
        "o": int(offset),
        "p": params,
        "c": (indexed_commit or "")[:12],
    }
    fields["s"] = _sign(fields)
    raw = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def decode(cursor: str, *, kind: str, params: str) -> dict[str, Any]:
    """Fields of a cursor this server issued for this request shape, or
    CursorError. Expiry is NOT checked here — see `expired`."""
    try:
        pad = "=" * (-len(cursor) % 4)
        fields = json.loads(base64.urlsafe_b64decode(cursor + pad).decode())
    except Exception as e:
        raise CursorError("cursor is not decodable") from e
    if not isinstance(fields, dict) or fields.get("v") != CURSOR_VERSION:
        raise CursorError("cursor version is not supported")
    try:
        if not hmac.compare_digest(str(fields.get("s", "")), _sign(fields)):
            raise CursorError("cursor checksum does not match")
    except KeyError as e:
        raise CursorError("cursor is missing fields") from e
    if fields.get("k") != kind:
        raise CursorError("cursor was issued for a different operation")
    if fields.get("p") != params:
        raise CursorError(
            "cursor was issued for a different query, path_prefix or ranking settings"
        )
    if int(fields.get("o", -1)) < 0:
        raise CursorError("cursor offset is invalid")
    return fields


def ttl_s() -> int:
    from server.settings import settings

    return int(settings.code_search_memo_ttl_s)


def expired(fields: dict[str, Any], *, now: float | None = None) -> bool:
    return (now if now is not None else time.time()) - int(fields.get("t", 0)) > ttl_s()


def enabled() -> bool:
    from server.settings import settings

    return bool(settings.code_search_cursor_enabled) and bool(settings.jwt_secret)


def pool_cap() -> int:
    from server.settings import settings

    return max(1, min(200, int(settings.code_search_cursor_pool)))


# --------------------------------------------------------------------------
# pool rows (loom.code_search_memo, key = cursor:<kind>:<run id>)
# --------------------------------------------------------------------------


def pool_key(kind: str, run_id: str) -> str:
    return f"cursor:{kind}:{run_id}"


def pool_entry(u: CodeUnit) -> dict[str, Any]:
    """What a page needs to rebuild a unit in pool order: its full-key
    parts (path, chunk id), its rank score and the header fields, never
    the body. ~150 bytes per unit."""
    d: dict[str, Any] = {
        "id": u.unit_id,
        "path": u.path,
        "score": u.score,
        "s": u.start_line,
        "e": u.end_line,
        "sym": u.symbol,
        "kind": u.kind,
    }
    via = getattr(u, "via", "")
    if via:
        d["via"] = via
    return d


def put_pool(
    org: str, ns: str, key: str, payload: dict[str, Any], *, indexed_commit: str, route: str
) -> None:
    """Fire-and-forget insert of a pool row; never awaited by the read. A
    lost row costs one disclosed re-search on the next page."""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    row = {
        "org": org,
        "namespace": ns,
        "key": key,
        "indexed_commit": (indexed_commit or "")[:40],
        "route": str(route or "")[:64],
        "payload": json.dumps(payload, separators=(",", ":"), default=str),
        "created_at": naive_utc_now(),
    }

    async def _bg() -> None:
        try:
            from server.code.store.index import CodeRepo

            await CodeRepo().insert_search_memo_row(row)
        except Exception:  # fire-and-forget pool write: the next page recomputes
            logger.debug("code search pool write failed", exc_info=True)

    # A write for this request: the task keeps the request's query tag
    # (`create_task` copies the context), so the row is attributed to it.
    _TASKS.spawn(_bg(), name="code-search-pool-put")


_TASKS = TaskSet()


async def get_pool(org: str, ns: str, key: str) -> dict[str, Any] | None:
    """The pool row under the identity prefix inside the read-side TTL:
    {payload: dict, indexed_commit: str, created_at} or None."""
    try:
        from server.code.store.index import CodeRepo

        row = await CodeRepo().search_memo_row(org, ns, key, ttl_s=ttl_s())
    except Exception:  # a failed pool read is a miss
        logger.debug("code search pool read failed", exc_info=True)
        return None
    if not row or not row.get("payload"):
        return None
    try:
        payload = json.loads(str(row["payload"]))
    except Exception:  # noqa: BLE001 — an unreadable pool payload is a miss
        return None
    stored = row.get("indexed_commit") or b""
    commit = (stored.decode("ascii", "ignore") if isinstance(stored, bytes) else str(stored)).strip(
        "\x00"
    )
    return {"payload": payload, "indexed_commit": commit, "created_at": row.get("created_at")}


# --------------------------------------------------------------------------
# paging over a pool
# --------------------------------------------------------------------------


def take_page(
    pool: list[dict[str, Any]], *, offset: int, size: int, exclude: set[str]
) -> tuple[list[dict[str, Any]], int, bool]:
    """(entries, next_offset, has_more): the next `size` pool entries from
    `offset` whose id is not excluded. next_offset is the pool index after
    the last one taken (positions stay stable across pages, whatever the
    caller excludes); has_more says whether any entry remains past it."""
    out: list[dict[str, Any]] = []
    i = max(0, offset)
    while i < len(pool) and len(out) < size:
        entry = pool[i]
        i += 1
        if str(entry.get("id") or "") in exclude:
            continue
        out.append(entry)
    return out, i, i < len(pool)


def rebuild_units(entries: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[CodeUnit]:
    """Pool entries → units in pool order, bodies from the fetched chunk
    rows. An entry whose chunk is gone (rewritten, tombstoned since the
    pool was ranked) is dropped: a body the index no longer holds is not
    served from memory."""
    by_id = {str(r.get("chunk_id") or ""): r for r in rows}
    units: list[CodeUnit] = []
    for e in entries:
        row = by_id.get(str(e.get("id") or ""))
        if row is None:
            continue
        u = unit_from_chunk(row, score=float(e.get("score") or 0.0))
        via = e.get("via")
        if via and "via" in CodeUnit.model_fields:
            u = u.model_copy(update={"via": via})
        units.append(u)
    return units


def disclose(
    as_of: dict[str, Any], *, page_commit: str, offset: int, fallback: str
) -> dict[str, Any]:
    """The current freshness block plus what a page discloses: the commit
    the pool was ranked at, whether the index moved past it, the offset
    served, and the fallback reason when the pool could not be read."""
    current = dict(as_of or {})
    current["page_offset"] = int(offset)
    if page_commit:
        current["page_commit"] = page_commit
        current["page_stale"] = bool(
            current.get("commit") and page_commit != str(current.get("commit"))
        )
    if fallback:
        current["page_fallback"] = fallback
    return current


def search_pool_payload(
    *,
    query: str,
    route: str,
    model: str,
    params: str,
    served: int,
    pool: list[CodeUnit],
    calibration: dict[str, Any],
) -> dict[str, Any]:
    return {
        "v": CURSOR_VERSION,
        "kind": "s",
        "query": query[:2000],
        "route": route,
        "model": model,
        "params": params,
        "served": int(served),
        "pool": [pool_entry(u) for u in pool[: pool_cap()]],
        "calib": calibration,
    }
