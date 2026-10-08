"""Source-evidence write + memory<->evidence linking for the extract path.

Imports no extract or search handler module, so the extract cluster stays
acyclic.
"""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client, sequential_reads
from server.core.datetimes import parse_iso_dt
from server.memory.app.chunk_projection import message_index_to_evidence_id
from server.memory.app.projection_errors import messages_source_resource_id

log = logging.getLogger(__name__)

# How a memory's evidence refs were found (`_scoped_evidence_refs`).
SCOPE_CITED = "cited"  # the extractor named the supporting message(s)
SCOPE_LEXICAL = "lexical"  # the server matched the fact to its best message
SCOPE_NONE = "none"  # nothing matched; the whole window is linked

# `loom.memory_belief_sources.support_type` per scope.
SUPPORT_CITED = "extracted_from"
SUPPORT_LEXICAL = "lexical_support"
SUPPORT_WINDOW = "window"

# The lexical fallback links at most this many messages per fact.
_LEXICAL_MAX_MESSAGES = 2


async def _write_source_evidence(
    input: Any,
    msgs: list[dict[str, str]],
    *,
    observation_date: str | None = None,
) -> list[str]:
    """Persist one evidence row per source message.

    `observed_at` is the day the turn was said: the message's own `timestamp` when it carries
    one, else the request's `observation_date` (the bench and any client replaying history
    send the session's date there), and only when neither is known the write time. Stamping
    replayed history with the ingest time would blind `memory.recall` by time and any dated
    reading of a raw turn.
    """

    from server.memory.app.evidence import EvidenceInput, write_evidence_rows

    if not msgs:
        return []
    source_resource_id = messages_source_resource_id(input)
    session_day = parse_iso_dt(observation_date) if observation_date else None
    rows = await write_evidence_rows(
        [
            EvidenceInput(
                org=input.org,
                namespace=input.namespace,
                agent=input.agent,
                user_id=input.user_id,
                session_id=input.session_id,
                run_id=getattr(input, "run_id", "-") or "-",
                source_type="message",
                role=str(msg.get("role") or "user"),
                actor=str(msg.get("role") or "user"),
                content=str(msg.get("content") or ""),
                source_resource_id=source_resource_id,
                # A timestamped message is evidence of that moment, not of the write.
                observed_at=parse_iso_dt(msg.get("timestamp")) or session_day,
                metadata={"operation": "memory.set_from_messages", "index": idx},
            )
            for idx, msg in enumerate(msgs)
            if str(msg.get("content") or "").strip()
        ],
    )
    return [row.evidence_id for row in rows]


async def _link_written_memories_to_evidence(
    input: Any,
    memory_keys: list[str],
    evidence_ids: list[str],
    *,
    evidence_ids_by_memory_key: dict[str, list[str]] | None = None,
    evidence_refs_by_memory_key: dict[str, list[tuple[str, int, int]]] | None = None,
    evidence_scope_by_memory_key: dict[str, str] | None = None,
) -> int:
    """Best-effort source links from written memories to evidence rows.

    ``evidence_scope_by_memory_key`` (from `_scoped_evidence_refs`) labels
    how each memory's refs were found; it selects the ``support_type`` the
    link rows are written under: ``extracted_from`` when the extractor cited
    the message, ``lexical_support`` when the server picked the best-matching
    message, ``window`` when nothing matched and the whole window is linked.
    """

    if not memory_keys or not evidence_ids:
        return 0
    ch = get_client()
    # The rows were inserted moments ago by this request. On shared-storage replicas a plain
    # read can land on a replica that has not seen them yet and return nothing, silently
    # linking nothing (22% of the facts of a whole-session conversation re-ingest ended up with
    # evidence rows and no links). Read with the write's consistency, and count what still
    # fails to resolve.
    with sequential_reads():
        rows = await ch.query_all(
            """
            SELECT memory_key, toString(memory_id) AS memory_id
            FROM loom.memory FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND agent = {agent:String}
              AND user_id = {uid:String}
              AND session_id = {sid:String}
              AND memory_key IN {keys:Array(String)}
              AND tombstoned_at IS NULL
            """,
            params={
                "org": input.org,
                "ns": input.namespace,
                "agent": input.agent,
                "uid": input.user_id,
                "sid": input.session_id,
                "keys": memory_keys,
            },
        )
    links = [
        (str(row["memory_key"]), str(row["memory_id"])) for row in rows if row.get("memory_id")
    ]
    missing = set(memory_keys) - {memory_key for memory_key, _memory_id in links}
    if missing:
        from server.memory.app.drops import record_drop

        log.warning(
            "evidence link: %d of %d written memories not readable after write (ns=%s); "
            "their links are not written",
            len(missing),
            len(memory_keys),
            input.namespace,
        )
        record_drop(
            input.org, input.namespace, "evidence.link", "unresolved_after_write", n=len(missing)
        )
    from server.memory.app.evidence import link_belief_sources

    if evidence_refs_by_memory_key:
        # Scoped refs (cited by the extractor, or the lexical fallback) link one memory to the
        # one to three messages that support it. Only a memory with no refs at all links the
        # whole window, under its own support_type, so the share of window-scoped links is
        # readable from the table. The LLM often omits source indices, so without the lexical
        # fallback most links would be whole-window links with a (0,0) span.
        scope = evidence_scope_by_memory_key or {}
        by_support: dict[str, list[tuple[str, str]]] = {}
        for memory_key, memory_id in links:
            refs = evidence_refs_by_memory_key.get(memory_key)
            if refs:
                support = (
                    SUPPORT_LEXICAL if scope.get(memory_key) == SCOPE_LEXICAL else SUPPORT_CITED
                )
            else:
                support = SUPPORT_WINDOW
            by_support.setdefault(support, []).append((memory_key, memory_id))
        linked = 0
        for support, scoped_links in by_support.items():
            await link_belief_sources(
                org=input.org,
                namespace=input.namespace,
                memory_links=scoped_links,
                evidence_ids=evidence_ids,
                evidence_spans_by_memory_key=(
                    evidence_refs_by_memory_key if support != SUPPORT_WINDOW else None
                ),
                support_type=support,
                ch=ch,
            )
            linked += sum(
                len(evidence_refs_by_memory_key.get(memory_key) or evidence_ids)
                for memory_key, _memory_id in scoped_links
            )
        return linked

    linked = 0
    grouped_links: dict[tuple[str, ...], list[tuple[str, str]]] = {}
    for memory_key, memory_id in links:
        scoped_evidence_ids = (evidence_ids_by_memory_key or {}).get(memory_key) or evidence_ids
        if not scoped_evidence_ids:
            continue
        grouped_links.setdefault(tuple(scoped_evidence_ids), []).append((memory_key, memory_id))
    for group_evidence_ids, scoped_links in grouped_links.items():
        await link_belief_sources(
            org=input.org,
            namespace=input.namespace,
            memory_links=scoped_links,
            evidence_ids=list(group_evidence_ids),
            support_type="extracted_from",
            ch=ch,
        )
        linked += len(scoped_links) * len(scoped_evidence_ids)
    return linked


def _evidence_ids_by_memory_key(
    msgs: list[dict[str, str]],
    facts: list[dict[str, Any]],
    evidence_ids: list[str],
) -> dict[str, list[str]]:
    """Build per-memory evidence links from extractor source indices."""

    evidence_by_index = message_index_to_evidence_id(msgs, evidence_ids)
    if not evidence_by_index:
        return {}
    out: dict[str, list[str]] = {}
    for fact in facts:
        key = str(fact.get("key") or fact.get("memory_key") or "").strip()
        if not key:
            continue
        raw_indices = fact.get("source_message_indices") or []
        if not isinstance(raw_indices, list):
            continue
        scoped_ids: list[str] = []
        seen_ids: set[str] = set()
        for idx_raw in raw_indices:
            try:
                idx = int(idx_raw)
            except (TypeError, ValueError):
                continue
            evidence_id = evidence_by_index.get(idx)
            if evidence_id and evidence_id not in seen_ids:
                seen_ids.add(evidence_id)
                scoped_ids.append(evidence_id)
        if scoped_ids:
            out[key] = scoped_ids
    return out


def _best_support_span(content: str, fact: dict[str, Any]) -> tuple[int, int]:
    """Return a best-effort support span for one extracted fact.

    The extractor emits source message indices but not character offsets. Use
    exact value/entity/key matches when possible, then fall back to the shortest
    sentence containing relevant terms, and finally link the full message.
    """

    if not content:
        return (0, 0)
    candidates: list[str] = []
    value = str(fact.get("value") or "").strip()
    if value:
        candidates.append(value)
    key = str(fact.get("key") or fact.get("memory_key") or "").strip()
    if key:
        candidates.append(key.replace("_", " "))
        candidates.append(key)
    for ent in list(fact.get("entities") or []):
        ent_text = str(ent).strip()
        if ent_text:
            candidates.append(ent_text)

    content_lc = content.lower()
    for term in candidates:
        term = " ".join(str(term).split())
        if len(term) < 3 or len(term) > 240:
            continue
        idx = content_lc.find(term.lower())
        if idx >= 0:
            return (idx, idx + len(term))

    term_tokens: set[str] = set()
    for term in candidates:
        cur: list[str] = []
        for ch in str(term).lower():
            if ch.isalnum():
                cur.append(ch)
            else:
                if len(cur) >= 4:
                    term_tokens.add("".join(cur))
                cur = []
        if len(cur) >= 4:
            term_tokens.add("".join(cur))
    if term_tokens:
        # Split into sentence spans (each terminator ends one; trailing text
        # is its own), then keep the highest-scoring, shortest-on-tie span.
        spans: list[tuple[int, int]] = []
        start = 0
        for idx, ch in enumerate(content):
            if ch in ".!?\n":
                spans.append((start, idx + 1))
                start = idx + 1
        if start < len(content):
            spans.append((start, len(content)))

        best: tuple[int, int, int] | None = None
        for s, e in spans:
            score = sum(1 for token in term_tokens if token in content[s:e].lower())
            if score and (
                best is None or score > best[0] or (score == best[0] and e - s < best[2] - best[1])
            ):
                best = (score, s, e)
        if best is not None:
            return (max(0, best[1]), max(0, best[2]))

    return (0, len(content))


def _evidence_refs_by_memory_key(
    msgs: list[dict[str, str]],
    facts: list[dict[str, Any]],
    evidence_ids: list[str],
) -> dict[str, list[tuple[str, int, int]]]:
    """Build per-memory evidence refs with best-effort source spans."""

    evidence_by_index = message_index_to_evidence_id(msgs, evidence_ids)
    if not evidence_by_index:
        return {}
    out: dict[str, list[tuple[str, int, int]]] = {}
    for fact in facts:
        key = str(fact.get("key") or fact.get("memory_key") or "").strip()
        if not key:
            continue
        raw_indices = fact.get("source_message_indices") or []
        if not isinstance(raw_indices, list):
            continue
        scoped_refs: list[tuple[str, int, int]] = []
        seen: set[tuple[str, int, int]] = set()
        for idx_raw in raw_indices:
            try:
                idx = int(idx_raw)
            except (TypeError, ValueError):
                continue
            evidence_id = evidence_by_index.get(idx)
            if not evidence_id:
                continue
            content = str((msgs[idx] or {}).get("content") or "")
            start, end = _best_support_span(content, fact)
            ref = (evidence_id, start, end)
            if ref in seen:
                continue
            seen.add(ref)
            scoped_refs.append(ref)
        if scoped_refs:
            out[key] = scoped_refs
    return out


def _fact_terms(fact: dict[str, Any]) -> tuple[list[str], set[str]]:
    """Phrases and word tokens that describe one fact, for lexical support."""

    phrases: list[str] = []
    value = str(fact.get("value") or "").strip()
    if value:
        phrases.append(value)
    key = str(fact.get("key") or fact.get("memory_key") or "").strip()
    if key:
        phrases.append(key.replace("_", " "))
    for ent in list(fact.get("entities") or []):
        ent_text = str(ent).strip()
        if ent_text:
            phrases.append(ent_text)
    tokens: set[str] = set()
    for phrase in phrases:
        cur: list[str] = []
        for ch in phrase.lower():
            if ch.isalnum():
                cur.append(ch)
            else:
                if len(cur) >= 4:
                    tokens.add("".join(cur))
                cur = []
        if len(cur) >= 4:
            tokens.add("".join(cur))
    return phrases, tokens


def _lexical_support_scores(
    msgs: list[dict[str, str]],
    fact: dict[str, Any],
) -> list[tuple[int, int]]:
    """Score every non-empty message as support for `fact`.

    Returns ``[(score, index), ...]`` sorted best first, zero-score messages
    omitted. An exact entity or key phrase found in the message outranks any
    token overlap; otherwise the score is the number of distinct fact tokens
    (4+ chars) the message contains. Ties keep message order, so the earlier
    turn wins when two messages match equally.
    """

    phrases, tokens = _fact_terms(fact)
    exact = [
        " ".join(p.split()).lower()
        for p in phrases[1:]  # entities and the key; the value is rarely verbatim
        if 3 <= len(p) <= 120
    ]
    scored: list[tuple[int, int]] = []
    for idx, msg in enumerate(msgs):
        content = str(msg.get("content") or "")
        if not content.strip():
            continue
        content_lc = content.lower()
        score = sum(1 for tok in tokens if tok in content_lc)
        if any(term in content_lc for term in exact):
            score += 100
        if score > 0:
            scored.append((score, idx))
    scored.sort(key=lambda pair: (-pair[0], pair[1]))
    return scored


def _scoped_evidence_refs(
    msgs: list[dict[str, str]],
    facts: list[dict[str, Any]],
    evidence_ids: list[str],
) -> tuple[dict[str, list[tuple[str, int, int]]], dict[str, str]]:
    """Per-memory evidence refs plus how each was found.

    Cited indices from the extractor win (`_evidence_refs_by_memory_key`).
    A fact the extractor did not cite is matched to its best one or two
    messages lexically (`_lexical_support_scores`), with a best-effort span.
    A fact nothing matches gets no refs and the ``none`` scope; the caller
    links the whole window for it under its own support_type.

    Returns ``(refs_by_key, scope_by_key)`` where scope is one of
    SCOPE_CITED / SCOPE_LEXICAL / SCOPE_NONE. Keys without a usable
    ``key`` are skipped, as in the cited path.
    """

    refs = _evidence_refs_by_memory_key(msgs, facts, evidence_ids)
    scope: dict[str, str] = {key: SCOPE_CITED for key in refs}
    evidence_by_index = message_index_to_evidence_id(msgs, evidence_ids)
    if not evidence_by_index:
        return refs, scope
    for fact in facts:
        key = str(fact.get("key") or fact.get("memory_key") or "").strip()
        if not key or key in refs:
            continue
        picked: list[tuple[str, int, int]] = []
        scored = _lexical_support_scores(msgs, fact)
        top = scored[0][0] if scored else 0
        for score, idx in scored:
            # A second message only rides along when it matches as well as
            # the first; a weaker runner-up is noise.
            if picked and score < top:
                break
            evidence_id = evidence_by_index.get(idx)
            if not evidence_id:
                continue
            content = str((msgs[idx] or {}).get("content") or "")
            start, end = _best_support_span(content, fact)
            picked.append((evidence_id, start, end))
            if len(picked) >= _LEXICAL_MAX_MESSAGES:
                break
        if picked:
            refs[key] = picked
            scope[key] = SCOPE_LEXICAL
        else:
            scope[key] = SCOPE_NONE
    return refs, scope
