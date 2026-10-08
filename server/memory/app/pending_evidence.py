"""Pending evidence for `memory.ask`.

A deferred `memory.set_from_messages` stores the raw request
in `loom.memory_extraction_queue` and writes nothing else until the worker
replays it: no facts, no chunks, no summary. Until then a reader sees
`pending_extraction > 0` and an answer that does not know what was just
said: the delayed-flush loss, evidence left unresolved at query time.

This module reads the pending batches in the asked scope, ranks their turns
by lexical overlap with the question, and hands the best few back as hits
the synthesizer can read — labelled `source="pending"`, keyed
`pending:<queue_id>:<turn>`, carrying the batch's session. No model call,
bounded reads, fail-open to nothing.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any

from server.memory.api.models_search import _MemorySearchHit

log = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[a-z0-9][a-z0-9'\-]{2,}")
_STOP = frozenset(
    "the and for you your that this with what when where which who whom how did does "
    "was were are our have has had from into about after before their they them she her "
    "his him its been will would could should than then there here also just like some "
    "any all not but can may might one two".split()
)


@dataclass
class PendingTurn:
    queue_id: str
    session_id: str
    index: int
    role: str
    content: str
    score: float
    enqueued_at: str = ""


def _tokens(text: str) -> set[str]:
    return {t for t in _WORD_RE.findall((text or "").lower()) if t not in _STOP}


def rank_pending_turns(
    question: str,
    batches: list[dict[str, Any]],
    *,
    max_turns: int = 5,
    min_overlap: int = 2,
    max_chars: int = 600,
) -> list[PendingTurn]:
    """The pending turns most lexically related to the question. Score is
    the count of shared content words (then the shared fraction of the
    question's words); a turn under `min_overlap` shared words is not
    evidence. Pure; malformed payloads are skipped."""
    q_tokens = _tokens(question)
    if not q_tokens:
        return []
    out: list[PendingTurn] = []
    for b in batches:
        try:
            data = json.loads(b.get("payload") or "{}")
        except Exception:  # noqa: BLE001 — a malformed batch is not evidence
            continue
        msgs = data.get("messages") or []
        if not isinstance(msgs, list):
            continue
        qid = str(b.get("queue_id") or "")
        sid = str(b.get("session_id") or data.get("session_id") or "")
        for i, m in enumerate(msgs):
            if not isinstance(m, dict):
                continue
            content = str(m.get("content") or "")
            if not content.strip():
                continue
            shared = q_tokens & _tokens(content)
            if len(shared) < min_overlap:
                continue
            score = len(shared) + len(shared) / max(1, len(q_tokens))
            out.append(
                PendingTurn(
                    queue_id=qid,
                    session_id=sid,
                    index=i,
                    role=str(m.get("role") or ""),
                    content=content[:max_chars],
                    score=round(score, 4),
                    enqueued_at=str(b.get("enqueued_at") or ""),
                )
            )
    out.sort(key=lambda t: (-t.score, t.queue_id, t.index))
    return out[: max(0, int(max_turns))]


def pending_hits(turns: list[PendingTurn], *, namespace: str) -> list[Any]:
    """Wire hits the synthesizer reads like any other memory. Similarity is
    a fixed mid-band value: the rows are relevant by construction (they
    matched the question) and unranked against the cosine pool."""
    hits: list[Any] = []
    for t in turns:
        label = f"[{t.role}] " if t.role else ""
        hits.append(
            _MemorySearchHit(
                memory_key=f"pending:{t.queue_id}:{t.index}",
                memory_type="episodic",
                content_excerpt=f"{label}{t.content}",
                distance=0.5,
                similarity=0.5,
                rank_score=0.5,
                source="pending",
                namespace=namespace,
                session_id=t.session_id,
                created_at=t.enqueued_at[:19],
                explanation="raw turn of a batch still waiting for extraction",
            )
        )
    return hits


async def pending_batches(
    *,
    org: str,
    namespace: str,
    agent: str | None = None,
    user_id: str | None = None,
    limit: int = 20,
    ch: Any | None = None,
) -> list[dict[str, Any]]:
    """The newest batches not yet extracted in the asked scope. Scope rules
    are `extraction_queue.pending_count`'s: `agent="*"`/None and `user_id`
    None widen the way memory.search does."""
    from server.core.ch import get_client
    from server.memory.app.extraction_queue import STATUS_PENDING, STATUS_RUNNING

    ch = ch or get_client()
    scope = ""
    params: dict[str, Any] = {
        "org": org,
        "ns": namespace,
        "pending": STATUS_PENDING,
        "running": STATUS_RUNNING,
        "lim": max(1, int(limit)),
    }
    if agent and agent != "*":
        scope += " AND agent = {agent:String}"
        params["agent"] = agent
    if user_id and user_id != "-":
        scope += " AND user_id = {uid:String}"
        params["uid"] = user_id
    rows = await ch.query_all(
        f"""
        SELECT queue_id, session_id, payload, toString(enqueued_at) AS enqueued_at
        FROM loom.memory_extraction_queue FINAL
        WHERE org = {{org:String}} AND namespace = {{ns:String}}
          AND status IN ({{pending:String}}, {{running:String}}){scope}
        ORDER BY enqueued_at DESC
        LIMIT {{lim:UInt32}}
        """,
        params,
    )
    return [dict(r) for r in rows]


async def pending_evidence_for(
    question: str,
    *,
    org: str,
    namespace: str,
    agent: str | None,
    user_id: str | None,
    max_turns: int = 5,
    max_batches: int = 20,
    ch: Any | None = None,
) -> list[Any]:
    """Hits for the pending turns that speak to the question; [] when nothing
    is pending, nothing matches, or the read fails."""
    try:
        batches = await pending_batches(
            org=org,
            namespace=namespace,
            agent=agent,
            user_id=user_id,
            limit=max_batches,
            ch=ch,
        )
        if not batches:
            return []
        turns = rank_pending_turns(question, batches, max_turns=max_turns)
        return pending_hits(turns, namespace=namespace)
    except Exception as e:  # noqa: BLE001 — pending evidence never fails an answer
        log.debug("pending evidence read failed (fail-open): %s", e)
        return []


__all__ = [
    "PendingTurn",
    "pending_batches",
    "pending_evidence_for",
    "pending_hits",
    "rank_pending_turns",
]
