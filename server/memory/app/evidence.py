"""Evidence-first provenance: store the raw source material behind a belief
and link the two. Append-only so write paths need no second transaction."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from server.core.ch import get_client
from server.core.ch_writes import batched_insert_settings
from server.core.datetimes import naive_utc_now
from server.core.hashing import sha256_text as _sha256_text


@dataclass(frozen=True)
class EvidenceInput:
    org: str
    namespace: str
    agent: str
    user_id: str = "-"
    session_id: str = "-"
    run_id: str = "-"
    source_type: str = "message"
    role: str = ""
    actor: str = ""
    content: str = ""
    source_resource_id: str = ""
    observed_at: datetime | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class EvidenceRow:
    evidence_id: str
    content_sha256: str


async def write_evidence_rows(
    evidence: list[EvidenceInput],
    *,
    ch: Any | None = None,
) -> list[EvidenceRow]:
    """Persist raw evidence rows and return their stable IDs.

    IDs are generated per row instead of using the content hash as the primary
    key because the same text can appear in different sessions and still be
    distinct evidence for erasure/audit purposes.
    """

    if not evidence:
        return []
    ch = ch or get_client()
    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    out: list[EvidenceRow] = []
    for item in evidence:
        evidence_id = str(uuid.uuid4())
        content = str(item.content or "")
        content_sha = _sha256_text(content)
        metadata_json = json.dumps(item.metadata or {}, sort_keys=True, default=str)
        rows.append(
            {
                "evidence_id": evidence_id,
                "org": item.org,
                "namespace": item.namespace,
                "agent": item.agent,
                "user_id": item.user_id,
                "session_id": item.session_id,
                "run_id": item.run_id,
                "source_type": item.source_type,
                "role": item.role,
                "actor": item.actor,
                "content": content,
                "content_sha256": content_sha,
                "source_resource_id": item.source_resource_id,
                "observed_at": (item.observed_at or now),
                "created_at": now,
                "metadata": metadata_json,
                "tombstoned_at": None,
            }
        )
        out.append(EvidenceRow(evidence_id=evidence_id, content_sha256=content_sha))
    await ch.insert(
        "loom.memory_evidence",
        rows,
        settings=batched_insert_settings(),
    )
    return out


async def link_belief_sources(
    *,
    org: str,
    namespace: str,
    memory_links: list[tuple[str, str]],
    evidence_ids: list[str],
    evidence_spans_by_memory_key: dict[str, list[tuple[str, int, int]]] | None = None,
    support_type: str = "source",
    ch: Any | None = None,
) -> None:
    """Link written memories to evidence rows.

    ``memory_links`` contains ``(memory_key, memory_id)`` pairs. With
    ``evidence_spans_by_memory_key``, each memory links only to its scoped
    ``(evidence_id, char_start, char_end)`` refs; without it, each memory links
    to every evidence row with an unknown span.
    """

    if not memory_links or not evidence_ids:
        return
    ch = ch or get_client()
    now = naive_utc_now()
    rows: list[dict[str, Any]] = []
    for memory_key, memory_id in memory_links:
        refs = (evidence_spans_by_memory_key or {}).get(memory_key) or [
            (evidence_id, 0, 0) for evidence_id in evidence_ids
        ]
        for evidence_id, char_start, char_end in refs:
            rows.append(
                {
                    "org": org,
                    "namespace": namespace,
                    "memory_key": memory_key,
                    "memory_id": memory_id,
                    "evidence_id": evidence_id,
                    "support_type": support_type,
                    "char_start": max(0, int(char_start or 0)),
                    "char_end": max(0, int(char_end or 0)),
                    "created_at": now,
                    "tombstoned_at": None,
                }
            )
    if not rows:
        return
    await ch.insert(
        "loom.memory_belief_sources",
        rows,
        settings=batched_insert_settings(),
    )
