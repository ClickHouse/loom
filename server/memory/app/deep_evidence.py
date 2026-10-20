"""Deep-evidence pass for memory.ask: open the raw turns behind the facts.

`memory.ask` synthesizes from fact excerpts (loom.memory, ≤2,000 chars each).
A fact is a compression of the turns it came from, and some questions need
the turns: the exact wording, the number the extractor rounded, the second
clause it dropped. The deep path recalls summaries, lets one LLM call pick the
relevant ones, follows the summary→event link to the raw text and answers again.
Loom stores the two tiers as `loom.memory` and `loom.memory_evidence`, linked by
`loom.memory_belief_sources`; this module is the read that follows the link.

Shape:

1. `select_candidates` — one structured LLM call over the numbered, compact
   candidate list returns the indices worth opening (at most
   `max_memories`). An empty or unparseable reply falls back to the top
   ranked candidates, so the pass always has something to open.
2. `fetch_raw_evidence` — the evidence rows linked to those memory keys,
   scoped rows first (`extracted_from`, `lexical_support`),
   window rows only if nothing scoped exists, bounded per memory and in
   total by characters so a whole-window memory cannot expand into
   its whole conversation.
3. `render_raw_evidence` — the block the synthesizer reads, each turn dated,
   role-tagged and labelled with the memory key it supports so citations
   keep working.

Every step is fail-open: a CH or LLM error yields an empty block and the
caller keeps its excerpt-level answer. Nothing here writes.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from server.identity.app.request_scope import request_scope
from server.identity.domain.read_scope import scope_and
from server.settings.budgets import llm_max_tokens

log = logging.getLogger(__name__)

# support_type values written by the extract path; the first
# two are scoped to the supporting message(s), "window" is every message in
# the extraction call, and rows written with a (0,0) span carry
# "extracted_from" with a zero-length span.
_SCOPED_SUPPORT = ("extracted_from", "lexical_support")

_SELECT_SYSTEM = (
    "You choose which stored memories are worth opening at full detail to "
    "answer a question. Each candidate is a numbered one-line summary of a "
    "memory. Return the numbers whose underlying source turns are most likely "
    "to contain the specific detail the question needs: the same entities, "
    "numbers, dates, decisions or events. Prefer fewer, precise picks. Return "
    'strict JSON: {"picks": [<int>, ...]} and nothing else. An empty list '
    "means none of the candidates is plausibly related."
)

_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


@dataclass
class EvidenceTurn:
    memory_key: str
    role: str
    observed_at: str
    content: str
    support_type: str = ""


@dataclass
class DeepEvidence:
    """What the pass produced, for the synthesizer and the wire."""

    selected_keys: list[str] = field(default_factory=list)
    turns: list[EvidenceTurn] = field(default_factory=list)
    block: str = ""
    reason: str = ""  # why the pass ran: verifier token or "requested"
    selector_fallback: bool = False  # the LLM picked nothing usable
    chars: int = 0


def candidate_lines(memories: list[Any], *, excerpt_chars: int = 200) -> str:
    """The numbered compact list the selector reads (≈ 60 tokens a line)."""

    lines: list[str] = []
    for i, m in enumerate(memories):
        date = str(getattr(m, "temporal_anchor", "") or getattr(m, "valid_at", "") or "")[:10]
        key = str(getattr(m, "memory_key", "") or "")
        excerpt = " ".join(str(getattr(m, "content_excerpt", "") or "").split())
        lines.append(f"[{i}] {date} {key}: {excerpt[:excerpt_chars]}")
    return "\n".join(lines)


def parse_picks(text: str, n_candidates: int, *, max_picks: int) -> list[int]:
    """Indices from the selector's reply, in-range, de-duplicated, capped."""

    if not text:
        return []
    m = _JSON_RE.search(text)
    if not m:
        return []
    try:
        payload = json.loads(m.group(0))
    except json.JSONDecodeError:
        return []
    raw = payload.get("picks") if isinstance(payload, dict) else None
    if not isinstance(raw, list):
        return []
    out: list[int] = []
    for item in raw:
        try:
            idx = int(item)
        except (TypeError, ValueError):
            continue
        if 0 <= idx < n_candidates and idx not in out:
            out.append(idx)
        if len(out) >= max_picks:
            break
    return out


async def select_candidates(
    llm: Any,
    question: str,
    memories: list[Any],
    *,
    max_memories: int,
    stage: str = "ask_deep_select",
) -> tuple[list[Any], bool]:
    """One LLM call picks the memories to open; falls back to the top ranked.

    Returns ``(picked_memories, fallback_used)``.
    """

    if not memories:
        return [], False
    top = memories[: max(1, min(max_memories, len(memories)))]
    if llm is None:
        return top, True
    try:
        from server.observability.genai import llm_stage

        with llm_stage(stage):
            res = await llm.complete(
                _SELECT_SYSTEM,
                f"Question: {question}\n\nCandidates:\n{candidate_lines(memories)}",
                max_tokens=llm_max_tokens("ask_deep_select"),
            )
        picks = parse_picks(
            str(getattr(res, "text", "") or ""), len(memories), max_picks=max_memories
        )
    except Exception as e:  # noqa: BLE001 — the pass is best-effort
        log.warning("deep-evidence selector failed (fail-open to top ranked): %s", e)
        picks = []
    if not picks:
        return top, True
    return [memories[i] for i in picks], False


# The link rows behind a set of memory keys. `memory_belief_sources` is a
# ReplacingMergeTree(created_at) keyed (org, namespace, memory_key,
# evidence_id); a link is written once and re-inserted only as a tombstone, so
# `LIMIT 1 BY` over the key after `ORDER BY created_at DESC` is the merge, on
# the granules the primary key already selected. The tombstone filter sits
# outside so a tombstoned latest version cannot let an older live one through.
# Keys repeat across users, so a narrowed read (`in_scope` holds its
# predicates) keeps only the links of the memory rows its scope reaches.
def _links_sql(in_scope: str) -> str:
    own_rows = (
        f"""
              AND memory_id IN (
                  SELECT memory_id FROM loom.memory
                  WHERE org = {{org:String}} AND namespace = {{ns:String}}
                    AND memory_key IN {{keys:Array(String)}}{in_scope}
              )"""
        if in_scope
        else ""
    )
    return f"""
        SELECT memory_key, support_type, char_start, char_end, toString(evidence_id) AS evidence_id
        FROM (
            SELECT memory_key, support_type, char_start, char_end, evidence_id, tombstoned_at
            FROM loom.memory_belief_sources
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND memory_key IN {{keys:Array(String)}}{own_rows}
            ORDER BY memory_key, evidence_id, created_at DESC
            LIMIT 1 BY memory_key, evidence_id
        )
        WHERE tombstoned_at IS NULL
"""


# The evidence rows for a set of ids: the same versioned shape (latest version
# per id, no FINAL), keyed as UUIDs. `evidence_id` is last in the table's
# primary key, so the key alone selects the namespace and the row filter
# decides which granules' `content` is read — 256-row granules make that a
# read of the rows asked for, not their 8,192 neighbours.
def _evidence_sql(in_scope: str) -> str:
    return f"""
        SELECT toString(evidence_id) AS evidence_id, role,
               toString(observed_at) AS observed_at, content
        FROM (
            SELECT evidence_id, role, observed_at, content, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND evidence_id IN {{ids:Array(UUID)}}{in_scope}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL AND content != ''
"""


_RAW_EVIDENCE_ROW_CAP = 400


def join_links_to_evidence(
    links: list[dict[str, Any]], evidence: list[dict[str, Any]], *, cap: int = _RAW_EVIDENCE_ROW_CAP
) -> list[dict[str, Any]]:
    """The rows a one-statement FINAL JOIN FINAL would return, built from the two
    keyed reads: one row per live link whose evidence row is live and
    non-empty, ordered by memory key, scoped support types first, a real span
    before a (0,0) one, then oldest first, capped at `cap` rows."""

    by_id = {str(e.get("evidence_id") or ""): e for e in evidence}
    out: list[dict[str, Any]] = []
    for link in links:
        ev = by_id.get(str(link.get("evidence_id") or ""))
        if ev is None:
            continue
        char_start = int(link.get("char_start") or 0)
        char_end = int(link.get("char_end") or 0)
        out.append(
            {
                "memory_key": str(link.get("memory_key") or ""),
                "support_type": str(link.get("support_type") or ""),
                "scoped_span": char_end > char_start + 1,
                "role": str(ev.get("role") or ""),
                "observed_at": str(ev.get("observed_at") or ""),
                "content": str(ev.get("content") or ""),
            }
        )
    out.sort(
        key=lambda r: (
            r["memory_key"],
            r["support_type"] not in _SCOPED_SUPPORT,
            not r["scoped_span"],
            r["observed_at"],
        )
    )
    return out[:cap]


async def fetch_raw_evidence(
    *,
    org: str,
    namespace: str,
    memory_keys: list[str],
    max_chars_per_memory: int,
    max_chars_total: int,
    ch: Any | None = None,
) -> list[EvidenceTurn]:
    """The source turns linked to `memory_keys`, scoped rows first, bounded.

    Two keyed reads, no FINAL: the link rows for the keys (a key-prefix read on
    `memory_belief_sources`, latest version per `(memory_key, evidence_id)` by `LIMIT 1 BY`,
    the tombstone filter outside), then the evidence rows for those ids (a read of
    `memory_evidence` by `evidence_id`, latest version per id, tombstone outside; the
    256-row granules keep the read to the rows asked for). The join is done
    here, and rows are ranked per memory with scoped support types first and a real span
    before a (0,0) one, then oldest first — the order a one-statement FINAL JOIN FINAL
    gives, so the caps below see the same rows. The per-memory and total character caps are
    applied in Python so the statements stay simple and the caps are exact.
    """

    if not memory_keys:
        return []
    from server.core.ch import get_client

    client = ch or get_client()
    base: dict[str, Any] = {"org": org, "ns": namespace}
    in_scope = scope_and(request_scope(), base)
    try:
        links = await client.query_all(
            _links_sql(in_scope), params={**base, "keys": list(memory_keys)}
        )
        ids = sorted({str(r.get("evidence_id") or "") for r in links} - {""})
        evidence = (
            await client.query_all(_evidence_sql(in_scope), params={**base, "ids": ids})
            if ids
            else []
        )
    except Exception as e:  # noqa: BLE001 — fail-open, keep the excerpt answer
        log.warning("deep-evidence fetch failed (fail-open): %s", e)
        return []
    rows = join_links_to_evidence(links, evidence)

    out: list[EvidenceTurn] = []
    used_per_key: dict[str, int] = {}
    seen: set[tuple[str, str]] = set()
    total = 0
    # A memory that has any scoped row never opens its window rows: the
    # window is the fallback and only worth reading when it is all
    # there is.
    has_scoped = {
        str(r.get("memory_key") or "")
        for r in rows
        if str(r.get("support_type") or "") in _SCOPED_SUPPORT and bool(r.get("scoped_span"))
    }
    for r in rows:
        key = str(r.get("memory_key") or "")
        support = str(r.get("support_type") or "")
        scoped = support in _SCOPED_SUPPORT and bool(r.get("scoped_span"))
        if key in has_scoped and not scoped:
            continue
        content = str(r.get("content") or "").strip()
        if not content:
            continue
        sig = (key, content[:120])
        if sig in seen:
            continue
        budget_left = max_chars_per_memory - used_per_key.get(key, 0)
        if budget_left <= 0:
            continue
        if total >= max_chars_total:
            break
        piece = content[: min(budget_left, max_chars_total - total)]
        used_per_key[key] = used_per_key.get(key, 0) + len(piece)
        total += len(piece)
        seen.add(sig)
        out.append(
            EvidenceTurn(
                memory_key=key,
                role=str(r.get("role") or ""),
                observed_at=str(r.get("observed_at") or "")[:19],
                content=piece,
                support_type=support,
            )
        )
    # Keep the block chronological across memories so "what changed" reads
    # in order; ties keep the per-memory grouping.
    out.sort(key=lambda t: (t.observed_at, t.memory_key))
    return out


SUMMARY_KEY_PREFIX = "summary:"
_TOKEN_RE = re.compile(r"[a-z0-9]{4,}")


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def session_ids_of(memory_keys: list[str]) -> list[str]:
    """Session ids behind `summary:<session_id>` hits (the session-summary
    leg, fetch_session_summaries), in order, de-duplicated."""

    out: list[str] = []
    for key in memory_keys:
        if key.startswith(SUMMARY_KEY_PREFIX):
            sid = key[len(SUMMARY_KEY_PREFIX) :]
            if sid and sid not in out:
                out.append(sid)
    return out


def window_turns_by_question(
    rows: list[dict[str, Any]],
    question: str,
    *,
    max_chars: int,
    context_turns: int = 1,
) -> list[dict[str, Any]]:
    """The turns of a session worth opening for `question`, bounded.

    A session summary is one hit for a whole session (tens of thousands of
    characters), so it cannot be opened whole. Score each turn by the
    distinct question tokens (4+ chars) it contains, keep the best-scoring
    turns plus `context_turns` neighbours on each side for coherence, in
    chronological order, until `max_chars` is spent. Zero-score turns are
    only kept as neighbours.
    """

    if not rows or max_chars <= 0:
        return []
    q = _tokens(question)
    scored = [(len(q & _tokens(str(r.get("content") or ""))), i) for i, r in enumerate(rows)]
    keep: set[int] = set()
    spent = 0
    for score, i in sorted(scored, key=lambda p: (-p[0], p[1])):
        if score <= 0:
            break
        block = range(max(0, i - context_turns), min(len(rows), i + context_turns + 1))
        cost = sum(len(str(rows[j].get("content") or "")) for j in block if j not in keep)
        if spent + cost > max_chars:
            # Take the scoring turn alone if it fits; otherwise stop.
            solo = len(str(rows[i].get("content") or ""))
            if i not in keep and spent + solo <= max_chars:
                keep.add(i)
                spent += solo
            continue
        for j in block:
            keep.add(j)
        spent += cost
    return [rows[i] for i in sorted(keep)]


async def fetch_session_turns(
    *,
    org: str,
    namespace: str,
    session_ids: list[str],
    question: str,
    max_chars_per_session: int,
    max_chars_total: int,
    ch: Any | None = None,
) -> list[EvidenceTurn]:
    """Open the sessions behind session-summary hits, windowed by the question.

    The summary leg ranks a whole session; this reads that session's turns
    from `loom.memory_evidence` (a key-prefix read on org, namespace, then
    session_id) and keeps the question-relevant window per session, capped
    per session and in total. Labelled with the `summary:<sid>` key so the
    synthesizer's citation points back at the hit.
    """

    if not session_ids:
        return []
    from server.core.ch import get_client

    client = ch or get_client()
    # Session ids repeat across users: a narrowed read opens only its own.
    params: dict[str, Any] = {"org": org, "ns": namespace, "sids": list(session_ids)}
    in_scope = scope_and(request_scope(), params)
    # Versioned, not FINAL: an evidence row's id is minted per
    # insert, so the only re-insert is a tombstone; the latest version by
    # `created_at` decides, as FINAL would, without the query-time merge
    # (same rows, a fraction of the read).
    sql = f"""
        SELECT session_id, role, toString(observed_at) AS observed_at, content
        FROM (
            SELECT session_id, role, observed_at, content, evidence_id, tombstoned_at
            FROM loom.memory_evidence
            WHERE org = {{org:String}}
              AND namespace = {{ns:String}}
              AND session_id IN {{sids:Array(String)}}
              AND content != ''{in_scope}
            ORDER BY evidence_id, created_at DESC
            LIMIT 1 BY evidence_id
        )
        WHERE tombstoned_at IS NULL
        ORDER BY session_id, observed_at ASC, evidence_id
        LIMIT 2000
    """
    try:
        rows = await client.query_all(sql, params=params)
    except Exception as e:  # noqa: BLE001 — fail-open
        log.warning("deep-evidence session fetch failed (fail-open): %s", e)
        return []
    by_sid: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        by_sid.setdefault(str(r.get("session_id") or ""), []).append(r)
    out: list[EvidenceTurn] = []
    total = 0
    for sid in session_ids:
        budget = min(max_chars_per_session, max_chars_total - total)
        if budget <= 0:
            break
        for r in window_turns_by_question(by_sid.get(sid, []), question, max_chars=budget):
            content = str(r.get("content") or "").strip()
            if not content:
                continue
            total += len(content)
            out.append(
                EvidenceTurn(
                    memory_key=f"{SUMMARY_KEY_PREFIX}{sid}",
                    role=str(r.get("role") or ""),
                    observed_at=str(r.get("observed_at") or "")[:19],
                    content=content,
                    support_type="session",
                )
            )
    return out


def render_raw_evidence(turns: list[EvidenceTurn]) -> str:
    """The block the synthesizer reads. Empty string when there is nothing."""

    if not turns:
        return ""
    parts = [
        f"[{t.observed_at[:10] or 'undated'}] [{t.memory_key}] ({t.role or 'turn'}) {t.content}"
        for t in turns
    ]
    return "\n\n".join(parts)


async def deep_evidence_pass(
    *,
    llm: Any,
    question: str,
    candidates: list[Any],
    org: str,
    namespace: str,
    reason: str,
    max_memories: int,
    max_chars_per_memory: int,
    max_chars_total: int,
    ch: Any | None = None,
) -> DeepEvidence:
    """Select, fetch, render. Never raises; an empty block means "nothing to add"."""

    picked, fallback = await select_candidates(llm, question, candidates, max_memories=max_memories)
    keys: list[str] = []
    for m in picked:
        k = str(getattr(m, "memory_key", "") or "")
        if k and k not in keys:
            keys.append(k)
    fact_keys = [k for k in keys if not k.startswith(SUMMARY_KEY_PREFIX)]
    turns = await fetch_raw_evidence(
        org=org,
        namespace=namespace,
        memory_keys=fact_keys,
        max_chars_per_memory=max_chars_per_memory,
        max_chars_total=max_chars_total,
        ch=ch,
    )
    # A picked session-summary hit opens its session, windowed by the
    # question, with whatever budget the fact evidence left.
    sids = session_ids_of(keys)
    if sids:
        left = max_chars_total - sum(len(t.content) for t in turns)
        if left > 0:
            turns = turns + await fetch_session_turns(
                org=org,
                namespace=namespace,
                session_ids=sids,
                question=question,
                max_chars_per_session=max_chars_per_memory,
                max_chars_total=left,
                ch=ch,
            )
            turns.sort(key=lambda t: (t.observed_at, t.memory_key))
    block = render_raw_evidence(turns)
    return DeepEvidence(
        selected_keys=keys,
        turns=turns,
        block=block,
        reason=reason,
        selector_fallback=fallback,
        chars=len(block),
    )


__all__ = [
    "SUMMARY_KEY_PREFIX",
    "DeepEvidence",
    "EvidenceTurn",
    "candidate_lines",
    "deep_evidence_pass",
    "fetch_raw_evidence",
    "fetch_session_turns",
    "parse_picks",
    "render_raw_evidence",
    "select_candidates",
    "session_ids_of",
    "window_turns_by_question",
]
