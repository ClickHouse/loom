"""Literal completion for count questions: the raw turns that name the
asked value join the pool.

Facet completion pulls every memory whose KEY carries the counted attribute; it cannot
pull a fact the extractor never wrote. Example: nine Chicago events stated, eight
`_location` facts extracted ("CraftFit Fest is happening in Chicago, IL" became
`user_attending_craftfit_fest` and nothing else), answer 8 against 9 with evidence
recall 1.0 — the missing statement was in the store as a raw turn all along. A raw turn
is a leaf, not a cache: when the question names a literal value — a place, a number —
the turns containing that literal are the complete set by construction. This module
extracts the literals from the question stem (capitalised spans, quoted strings), reads
the user turns containing each from `memory_evidence` with sequential consistency, and
adds them as hits labelled with their minute so the count runs over the whole set.
Fail-open to no extra hits.
"""

from __future__ import annotations

import contextvars
import logging
from typing import Any

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.memory.api.models_search import _MemorySearchHit
from server.memory.domain.question_literals import question_literals

log = logging.getLogger(__name__)

# How many user turns carried the question's literal on the last completion,
# duplicates included — a generated count counts every message that
# satisfies the predicate, so an asker who says "count every stated instance"
# wants this number, not the number of distinct entities. Read by
# ask_threshold.verify_statement_count; bound per request by memory.ask.
LAST_LITERAL_STATEMENTS: contextvars.ContextVar[int] = contextvars.ContextVar(
    "loom_last_literal_statements", default=0
)


async def fetch_turns_with_literal(
    *, org: str, namespace: str, literal: str, limit: int, ch: Any | None = None
) -> list[dict[str, Any]]:
    from contextlib import nullcontext

    from server.core.ch import get_client, sequential_reads
    from server.memory.store import read_your_writes_enabled

    client = ch or get_client()
    params: dict[str, Any] = {"org": org, "ns": namespace, "lit": literal, "lim": int(limit)}
    in_scope = scope_and(request_scope(), params)
    # Versioned, not FINAL.
    sql = f"""
        SELECT session_id, role, toString(observed_at) AS observed_at, content
        FROM (
            SELECT session_id, role, observed_at, content, evidence_id, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND role = 'user'
              AND positionCaseInsensitive(content, {{lit:String}}) > 0{in_scope}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY observed_at ASC, evidence_id
        LIMIT {{lim:UInt32}}
    """
    scope = sequential_reads() if read_your_writes_enabled() else nullcontext()
    with scope:
        rows = await client.query_all(sql, params=params)
    return list(rows or [])


def turn_hits(rows: list[dict[str, Any]], ranked: list[Any], *, max_hits: int) -> list[Any]:
    have = {getattr(h, "memory_key", "") for h in ranked}
    seen_text: set[str] = set()
    out: list[Any] = []
    for i, r in enumerate(rows):
        content = str(r.get("content") or "").strip()
        if not content or content.lower() in seen_text:
            continue
        seen_text.add(content.lower())
        observed = str(r.get("observed_at") or "")[:19].replace("T", " ")
        sid = str(r.get("session_id") or "")
        key = f"turn:{sid}:{observed[11:16] or i}"
        n = 2
        while key in have:
            key = f"turn:{sid}:{observed[11:16] or i}#{n}"
            n += 1
        have.add(key)
        try:
            out.append(
                _MemorySearchHit(
                    memory_key=key,
                    memory_type="episodic",
                    content_excerpt=f"[user @ {observed[:16]}] {content[:400]}",
                    distance=0.0,
                    similarity=0.0,
                    rank_score=0.0,
                    valid_at=observed,
                    source="evidence",
                    explanation="literal completion",
                )
            )
        except Exception:  # noqa: BLE001 — skip a row that won't validate
            continue
        if len(out) >= max_hits:
            break
    return out


async def literal_complete(
    search_input: Any, stem: str, ranked: list[Any], *, max_hits: int
) -> list[Any]:
    """Extra hits for the turns naming the question's literals; [] when the
    question names none or on any failure."""
    LAST_LITERAL_STATEMENTS.set(0)
    lits = question_literals(stem)
    if not lits:
        return []
    org = str(getattr(search_input, "org", "") or "")
    ns = str(getattr(search_input, "namespace", "") or "")
    rows: list[dict[str, Any]] = []
    for lit in lits:
        try:
            rows += await fetch_turns_with_literal(
                org=org, namespace=ns, literal=lit, limit=max_hits * 2
            )
        except Exception as e:  # noqa: BLE001 — additive, never fatal
            log.warning("literal completion failed for %r (fail-open): %s", lit, e)
    LAST_LITERAL_STATEMENTS.set(len(rows))
    hits = turn_hits(rows, ranked, max_hits=max_hits)
    if hits:
        log.info(
            "memory.ask: literal completion %s → %d turn(s) (%d rows)", lits, len(hits), len(rows)
        )
    return hits


__all__ = ["fetch_turns_with_literal", "literal_complete", "turn_hits"]
