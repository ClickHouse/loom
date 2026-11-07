"""Past queries as nodes.

`loom.query_nodes` keeps one row per distinct normalised query per
(org, namespace): the text, its embedding, the keys the last answer returned,
the sessions that asked and how often it was asked. `build_query_node_row` is
called from memory.search's telemetry hook, next to the search_trace_log row,
gated on the same capture consent (query text is user content) and on
LOOM_MEMORY_QUERY_NODES_ENABLED.
"""

from __future__ import annotations

import hashlib
import re
from datetime import UTC, datetime
from typing import Any

_WS = re.compile(r"\s+")
# from the run's first character only, so a long run is one pass
_TRAIL_PUNCT = re.compile(r"(?<![\s?.!,;:])[\s?.!,;:]+$")


def normalise_query(text: str) -> str:
    """The identity of a query node: lower-cased, whitespace-collapsed,
    trailing punctuation dropped. "How to X?" and "how to x" are one node;
    "how to x" and "how to y" are two."""
    t = _WS.sub(" ", str(text or "")).strip().lower()
    return _TRAIL_PUNCT.sub("", t)


def query_hash(text: str) -> int:
    """UInt64 identity of the normalised text. Python-side (blake2b/8), stable
    across processes; never compared to a ClickHouse hash function."""
    norm = normalise_query(text)
    return int.from_bytes(hashlib.blake2b(norm.encode("utf-8"), digest_size=8).digest(), "big")


def build_query_node_row(
    *,
    org: str,
    namespace: str,
    query: str,
    query_vec: list[float],
    embedding_model: str,
    returned_keys: list[str],
    abstain: bool,
    now: datetime | None = None,
    session_id: str = "-",
) -> dict[str, Any] | None:
    """Pure builder for one loom.query_nodes row. None when there is no
    text, no vector, or no answer."""
    norm = normalise_query(query)
    if not norm or not query_vec or not returned_keys:
        return None
    ts = (now or datetime.now(UTC)).replace(tzinfo=None, microsecond=0)
    return {
        "org": org,
        "namespace": namespace,
        "query_hash": query_hash(norm),
        "query": norm[:2000],
        "embedding_model": str(embedding_model or ""),
        "embedding_dim": len(query_vec),
        "embedding": [float(x) for x in query_vec],
        "returned_keys": [str(k) for k in returned_keys][:50],
        # who asked; '-' is nobody.
        "sessions": [str(session_id)] if session_id and session_id != "-" else [],
        "hits": 1,
        "abstains": 1 if abstain else 0,
        "first_seen": ts,
        "last_seen": ts,
    }


__all__ = [
    "build_query_node_row",
    "normalise_query",
    "query_hash",
]
