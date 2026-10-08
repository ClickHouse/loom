"""Chunk provenance: which memory keys and evidence turns each transcript chunk backs.

loom.memory_chunk_sources holds one row per chunk x parent memory key x overlapping
evidence turn. Every memory extracted from a session is a parent of every chunk of
that session, so the table grows as the cross product. Every writer and reader of
the links goes through this module; the read shape is a module constant for the
analyzer test.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

SOURCES_TABLE = "loom.memory_chunk_sources"

# (chunk_index, content_sha256, line_start, line_end)
ChunkSpan = tuple[int, str, int, int]
# (line_start, line_end, evidence_id) — `message_line_evidence_refs`
EvidenceRef = tuple[int, int, str]


def _overlapping(refs: list[EvidenceRef], line_start: int, line_end: int) -> list[str]:
    return [
        evidence_id
        for evidence_line_start, evidence_line_end, evidence_id in refs
        if evidence_line_start <= line_end and evidence_line_end >= line_start
    ]


def provenance_rows(
    *,
    org: str,
    namespace: str,
    agent: str,
    session_id: str,
    chunks: list[ChunkSpan],
    parent_keys: list[str],
    evidence_refs: list[EvidenceRef],
    now: datetime,
) -> list[dict[str, Any]]:
    """The link rows for a session's chunks. The extraction writer and the
    projection-failure retry both build from here, so a retry re-creates exactly
    what the write path would have."""
    base = {"org": org, "namespace": namespace, "agent": agent or "", "session_id": session_id}
    keys: list[str] = list(parent_keys) or [""]
    rows: list[dict[str, Any]] = []
    for chunk_index, content_sha256, line_start, line_end in chunks:
        hits = _overlapping(evidence_refs, line_start, line_end)
        overlapping: list[str | None] = list(hits) or [None]
        for memory_key in keys:
            for turn in overlapping:
                rows.append(
                    {
                        **base,
                        "chunk_index": int(chunk_index),
                        "content_sha256": content_sha256,
                        "memory_key": memory_key,
                        "evidence_id": turn,
                        "line_start": int(line_start),
                        "line_end": int(line_end),
                        "created_at": now,
                        "tombstoned_at": None,
                    }
                )
    return rows


def import_link_rows(
    *,
    org: str,
    namespace: str,
    agent: str,
    chunks: list[tuple[str, int, str, int, int, list[str]]],
    now: datetime,
) -> list[dict[str, Any]]:
    """memory.import_evidence's links: `chunks` is (session_id, chunk_index,
    content_sha256, line_start, line_end, memory_keys) per imported chunk; one row
    per chunk per non-empty key."""
    rows: list[dict[str, Any]] = []
    for session_id, chunk_index, sha, line_start, line_end, memory_keys in chunks:
        for key in dict.fromkeys(k for k in memory_keys if k):
            rows.append(
                {
                    "org": org,
                    "namespace": namespace,
                    "agent": agent or "",
                    "session_id": session_id,
                    "chunk_index": int(chunk_index),
                    "content_sha256": sha,
                    "memory_key": key,
                    "evidence_id": None,
                    "line_start": int(line_start or 0),
                    "line_end": int(line_end or 0),
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
    return rows


# The export's link read: keys per chunk.
KEYS_SQL = f"""
SELECT session_id, chunk_index, groupUniqArray(memory_key) AS memory_keys
FROM {SOURCES_TABLE} FINAL
WHERE org = {{org:String}} AND namespace = {{ns:String}}
  AND session_id IN {{sessions:Array(String)}}
  AND tombstoned_at IS NULL AND memory_key != ''
GROUP BY session_id, chunk_index
"""


async def chunk_memory_keys(
    ch: Any,
    *,
    org: str,
    namespace: str,
    chunks: list[tuple[str, int]],
) -> dict[tuple[str, int], list[str]]:
    """(session_id, chunk_index) -> sorted memory keys, for the chunks given."""
    if not chunks:
        return {}
    sessions = sorted({sid for sid, _ in chunks})
    rows = await ch.query_all(KEYS_SQL, params={"org": org, "ns": namespace, "sessions": sessions})
    return {
        (str(r["session_id"]), int(r["chunk_index"])): sorted(
            str(k) for k in (r.get("memory_keys") or [])
        )
        for r in rows
    }
