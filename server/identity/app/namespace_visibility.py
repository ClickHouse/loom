"""TTL-cached namespace visibility for the search tier split.

`memory.search` splits the token's already-authorized namespaces into private
and public so it can try private first and skip public when private already
answered well. Deciding that split needs `loom.namespaces.visibility`, which
is a `FINAL` query; running it on **every** multi-namespace search is a
ClickHouse round trip on the hot path that often buys nothing (an org whose
namespaces are all public finds no private one and searches everything anyway).

Visibility is namespace configuration. It changes when an operator changes it,
which is approximately never relative to search rate, so it is cached.

**This is not an authorization input.** The caller has already resolved which
namespaces the token may read; this only orders them. A stale entry can
therefore cost relevance (a just-privatised namespace keeps being searched in
the public tier for up to `_TTL_S`) but cannot widen access. Access control
lives in the token and `ctx.namespaces`.

Fails open by design: on any error the caller gets no answer for that namespace
and falls back to the flat concurrent fan-out, which searches everything. That
is the safe direction — it can only search more than needed, never less.
"""

from __future__ import annotations

import logging
import time

log = logging.getLogger(__name__)

# Matches the capture-consent cache. Long enough that a busy namespace answers
# from memory, short enough that an operator flipping visibility sees search
# ordering follow within a minute.
_TTL_S = 60.0
# Bounded so a hostile or buggy caller cannot grow this without limit. On
# overflow the whole map is dropped rather than evicting one entry: a miss costs
# one query and repopulates.
_MAX_ENTRIES = 5000

# (org, namespace) -> (expires_at_monotonic, visibility)
_cache: dict[tuple[str, str], tuple[float, str]] = {}


def reset() -> None:
    """Drop every entry. For tests, and for an operator-facing cache flush."""
    _cache.clear()


async def visibility_map(org: str, namespaces: list[str]) -> dict[str, str]:
    """Resolve `namespace -> visibility` for `namespaces` under `org`.

    Answers cached namespaces from memory and queries only the misses. Returns a
    partial map on error rather than raising; namespaces absent from the result
    are unknown to the caller, not "private".
    """
    now = time.monotonic()
    out: dict[str, str] = {}
    missing: list[str] = []
    for ns in namespaces:
        hit = _cache.get((org, ns))
        if hit is not None and hit[0] > now:
            out[ns] = hit[1]
        else:
            missing.append(ns)

    if not missing:
        return out

    from server.core.ch import get_client
    from server.observability.otel import traced

    try:
        with traced(
            "ch.query.namespace_visibility",
            namespaces=len(missing),
            cached=len(out),
        ):
            rows = await get_client().query_all(
                """
                SELECT namespace, toString(visibility) AS visibility
                FROM loom.namespaces FINAL
                WHERE org = {org:String} AND namespace IN {ns:Array(String)}
                """,
                params={"org": org, "ns": missing},
            )
    except Exception as e:  # noqa: BLE001 — fail open: callers search every namespace
        log.warning("namespace visibility lookup failed (non-fatal): %s", e)
        return out

    if len(_cache) + len(rows) > _MAX_ENTRIES:
        _cache.clear()

    expires = time.monotonic() + _TTL_S
    for r in rows:
        ns = str(r["namespace"])
        vis = str(r.get("visibility") or "private")
        _cache[(org, ns)] = (expires, vis)
        out[ns] = vis
    return out
