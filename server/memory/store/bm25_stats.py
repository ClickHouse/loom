"""BM25 statistics for embedding rows that land after their version's window.

`memory_doc_stats_refresh` and `memory_term_freq_refresh` fold memory_embeddings
rows by version, each pass taking `version > watermark AND version <= now - 10 s`
(the BM25 live tail reads the rows above the watermark). A write's embedding row
carries its memory row's version, drawn when the write began, so a write that
inserts more than the lag after drawing it (a long embed, a slow judge) can land
under a watermark that already moved past it, and no pass would count it. Such a
write indexes its own rows here with the views' expressions. Both tables take a
repeat: doc stats collapse per row (ReplacingMergeTree on the row key) and a
term's document frequency is a uniqExact over row identities.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from server.core.repo_base import _CHClient, _default_client

_log = logging.getLogger(__name__)

# The refresh views' lag, less a margin for the time a version spends in flight.
REFRESH_LAG_NS = 10 * 1_000_000_000
LATE_AFTER_NS = REFRESH_LAG_NS // 2

_IDENTITY = "(org, namespace, agent, user_id, session_id, memory_key)"
_KEYS = "{keys:Array(Tuple(String, String, String, String, String, String))}"

_DOC_STATS = f"""
INSERT INTO loom.memory_doc_stats
    (org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key,
     excerpt_length, version, stats_at, ts_hwm)
SELECT org, namespace, agent, user_id, session_id, scope, embedding_model, memory_key,
       toUInt32(length(content_excerpt)), version, now(), version
FROM loom.memory_embeddings FINAL
WHERE {_IDENTITY} IN {_KEYS} AND embedding_dim > 0 AND tombstoned_at IS NULL
"""

_TERM_FREQ = f"""
INSERT INTO loom.memory_term_freq (org, namespace, term, df_state, df_added_at, ts_hwm)
SELECT org, namespace, term,
       uniqExactState((toString(agent), user_id, session_id, toString(scope),
                       toString(embedding_model), memory_key)),
       now(), max(version)
FROM loom.memory_embeddings FINAL
ARRAY JOIN arrayDistinct(arrayFilter(t -> (length(t) >= 3),
           arrayMap(t -> lowerUTF8(t), splitByNonAlpha(content_excerpt)))) AS term
WHERE {_IDENTITY} IN {_KEYS} AND embedding_dim > 0 AND tombstoned_at IS NULL
GROUP BY org, namespace, term
"""


def late_rows(rows: list[dict[str, Any]], *, now_ns: int | None = None) -> list[dict[str, Any]]:
    """The live vector rows of a write whose version is old enough that a refresh pass
    may already have moved past it."""
    cutoff = (now_ns if now_ns is not None else time.time_ns()) - LATE_AFTER_NS
    return [
        r
        for r in rows
        if int(r.get("version") or 0) <= cutoff
        and int(r.get("embedding_dim") or 0) > 0
        and r.get("tombstoned_at") is None
    ]


async def index_late_rows(rows: list[dict[str, Any]], *, ch: _CHClient | None = None) -> int:
    """Fold a write's late embedding rows (`late_rows`) into the BM25 statistics. Never
    raises: the write landed, and a miss only skews BM25 weights until a rebuild.
    Returns the rows indexed."""
    late = late_rows(rows)
    if not late:
        return 0
    keys = sorted(
        {
            (
                str(r["org"]),
                str(r["namespace"]),
                str(r["agent"]),
                str(r["user_id"]),
                str(r["session_id"]),
                str(r["memory_key"]),
            )
            for r in late
        }
    )
    ch = ch or _default_client()
    try:
        await ch.execute(_DOC_STATS, params={"keys": keys})
        await ch.execute(_TERM_FREQ, params={"keys": keys})
    except Exception as e:  # noqa: BLE001 — the write landed; BM25 weights catch up on a rebuild
        _log.warning("bm25 stats: late rows not indexed for %d keys: %s", len(keys), e)
        return 0
    return len(late)
