"""memory.evidence and memory.provenance: the sources a memory rests on, and
how each entered memory."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from server.memory.api.models_lifecycle import (
    MemoryEvidenceInput,
    MemoryEvidenceItem,
    MemoryEvidenceOutput,
    MemoryProvenanceInput,
    MemoryProvenanceLink,
    MemoryProvenanceOutput,
)


def _classify_source(source_trace: str) -> tuple[str, str, str]:
    """Map a raw source_trace to (kind, thread_id, human_label).

    kind ∈ {conversation, extracted, observed, consolidated, app, manual, other}.
    thread_id is set only for the `conversation:<id>` convention.
    """
    s = (source_trace or "").strip()
    if not s:
        return ("manual", "", "Added manually")
    if s.startswith("conversation:"):
        return ("conversation", s[len("conversation:") :], "From a conversation")
    if "://" in s:  # app-defined URI, e.g. myapp://finding/detected
        scheme, _, rest = s.partition("://")
        return ("app", "", f"{scheme} · {rest}" if rest else scheme)
    if s.startswith("extract:"):
        return ("extracted", "", "Extracted from messages")
    if s.startswith("observe"):
        return ("observed", "", "Captured from tool activity")
    if s.startswith("consolidate") or s == "memory.consolidate":
        return ("consolidated", "", "Consolidated from other memories")
    return ("other", "", s)


_EVIDENCE_SCOPE_BY_SUPPORT = {
    "extracted_from": "cited",
    "lexical_support": "lexical",
    "window": "window",
    # consolidation lineage: evidence_id is a SOURCE MEMORY id
    "derived_from": "derived",
}


def evidence_scope(support_type: str, char_start: int, char_end: int, n_links: int) -> str:
    """Scope a belief-source link implies.

    `extracted_from` is a citation only when it carries a span or the memory has few links;
    older rows linked the whole window under `extracted_from` with zero-length spans, and
    those must read as `window` or an integrity measure counts them as citations."""
    scope = _EVIDENCE_SCOPE_BY_SUPPORT.get(support_type or "", "other")
    if scope == "cited" and char_end <= char_start and n_links > 3:
        return "window"
    return scope


async def handle_memory_evidence(input: MemoryEvidenceInput) -> MemoryEvidenceOutput:
    """The turns a memory was extracted from, with spans and link scope.

    Two reads, no join under FINAL: the belief-source links for the key, then
    the evidence rows they point at. Identity-scoped like `memory.provenance`;
    a key outside the caller's namespace is `found=False`."""
    import server.memory.store as memory_repo
    from server.core.ch import get_client as _get_client

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    row = await memory_repo.get_by_key(identity, input.memory_key, fresh=True)
    ch = _get_client()
    memory_id = str(getattr(row, "memory_id", "") or "") if row is not None else ""
    value: Any = row.value if row is not None else None
    if row is None and (input.agent == "*" or input.session_id == "*" or input.user_id == "*"):
        # Wildcard identity, as memory.list accepts: a key is unique within a
        # namespace, so resolve the live row by key alone. Extracted memories
        # carry the extraction's session_id, which a caller auditing a
        # namespace does not know.
        hit = await ch.query_all(
            "SELECT toString(memory_id) AS mid, value FROM loom.memory FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String} "
            "AND tombstoned_at IS NULL ORDER BY status = 'active' DESC, version DESC LIMIT 1",
            {"o": input.org, "n": input.namespace, "k": input.memory_key},
        )
        if hit:
            memory_id = str(hit[0].get("mid") or "")
            value = hit[0].get("value")
    if row is None and not memory_id:
        return MemoryEvidenceOutput(found=False)
    # Versioned, not FINAL: one row per link, latest wins.
    links = await ch.query_all(
        "SELECT toString(evidence_id) AS eid, argMax(support_type, created_at) AS support_type, "
        "argMax(char_start, created_at) AS char_start, argMax(char_end, created_at) AS char_end, "
        "min(created_at) AS first_seen, argMax(tombstoned_at, created_at) AS tomb_latest "
        "FROM loom.memory_belief_sources "
        "WHERE org = {o:String} AND namespace = {n:String} AND memory_key = {k:String} "
        "GROUP BY evidence_id HAVING tomb_latest IS NULL "
        "ORDER BY first_seen, evidence_id LIMIT 1000",
        {"o": input.org, "n": input.namespace, "k": input.memory_key},
    )
    n_links = len(links)
    counts = {"cited": 0, "lexical": 0, "window": 0, "derived": 0}
    scoped: list[tuple[dict[str, Any], str]] = []
    for link in links:
        sc = evidence_scope(
            str(link.get("support_type") or ""),
            int(link.get("char_start") or 0),
            int(link.get("char_end") or 0),
            n_links,
        )
        if sc in counts:
            counts[sc] += 1
        scoped.append((link, sc))
    page = scoped[: int(input.limit)]
    ids = [str(link["eid"]) for link, sc in page if sc != "derived"]
    src_ids = [str(link["eid"]) for link, sc in page if sc == "derived"]
    ev_by_id: dict[str, dict[str, Any]] = {}
    if ids:
        # Versioned, not FINAL: latest row per id.
        ev_rows = await ch.query_all(
            "SELECT toString(evidence_id) AS eid, argMax(role, created_at) AS role, "
            "argMax(observed_at, created_at) AS observed_at, "
            "argMax(content, created_at) AS content, "
            "argMax(tombstoned_at, created_at) AS tomb_latest "
            "FROM loom.memory_evidence "
            "WHERE org = {o:String} AND namespace = {n:String} "
            "AND evidence_id IN {ids:Array(UUID)} "
            "GROUP BY evidence_id HAVING tomb_latest IS NULL",
            {"o": input.org, "n": input.namespace, "ids": ids},
        )
        ev_by_id = {str(r.get("eid")): r for r in ev_rows}
    if src_ids:
        # A derived row (consolidation) cites the memories it compressed; the "evidence" is
        # their text, which is what a drift audit of the derived row compares against.
        src_rows = await ch.query_all(
            "SELECT toString(memory_id) AS eid, memory_key, value, created_at AS observed_at "
            "FROM loom.memory FINAL "
            "WHERE org = {o:String} AND namespace = {n:String} "
            "AND memory_id IN {ids:Array(UUID)} AND tombstoned_at IS NULL",
            {"o": input.org, "n": input.namespace, "ids": src_ids},
        )
        for r in src_rows:
            val = r.get("value")
            text = val if isinstance(val, str) else json.dumps(val, ensure_ascii=False)
            ev_by_id[str(r.get("eid"))] = {
                "role": "memory",
                "observed_at": r.get("observed_at"),
                "content": f"[{r.get('memory_key', '')}] {text}",
            }
    items: list[Any] = []
    for link, sc in page:
        ev = ev_by_id.get(str(link["eid"]), {})
        content = str(ev.get("content") or "")
        cs, ce = int(link.get("char_start") or 0), int(link.get("char_end") or 0)
        span = content[cs:ce] if 0 <= cs < ce <= len(content) else ""
        items.append(
            MemoryEvidenceItem(
                evidence_id=str(link["eid"]),
                support_type=str(link.get("support_type") or ""),
                scope=sc,  # type: ignore[arg-type]
                role=str(ev.get("role") or ""),
                observed_at=ev.get("observed_at"),
                content=content[:600],
                char_start=cs,
                char_end=ce,
                span=span,
            )
        )
    items.sort(
        key=lambda it: (it.observed_at is None, it.observed_at or datetime.min, it.evidence_id)
    )
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return MemoryEvidenceOutput(
        found=True,
        memory_key=input.memory_key,
        memory_id=memory_id,
        value=text[:2000],
        items=items,
        truncated=n_links > len(page),
        cited=counts["cited"],
        lexical=counts["lexical"],
        window=counts["window"],
        derived=counts["derived"],
    )


async def handle_memory_provenance(
    input: MemoryProvenanceInput,
) -> MemoryProvenanceOutput:
    """Resolve where a memory came from, for user-facing provenance.

    Classifies the memory's source_trace and — for conversation-sourced memories
    — checks whether the source transcript is still retained (recording may be
    off per-namespace, or the conversation deleted), so the
    UI offers a live link only when it will actually resolve rather than
    dead-ending.
    """
    import server.memory.store as memory_repo
    from server.core.ch import get_client as _get_client

    identity = memory_repo.Identity(
        org=input.org,
        namespace=input.namespace,
        agent=input.agent,
        user_id=input.user_id,
        session_id=input.session_id,
    )
    row = await memory_repo.get_by_key(identity, input.memory_key, fresh=True)
    if row is None:
        return MemoryProvenanceOutput(found=False)

    source_trace = getattr(row, "source_trace", "") or ""
    kind, thread_id, label = _classify_source(source_trace)

    thread_exists = False
    thread_title = ""
    if kind == "conversation" and thread_id:
        ch = _get_client()
        params = {"o": input.org, "n": input.namespace, "t": thread_id}
        # A deleted or erased conversation is not retained: its tombstoned messages and
        # thread row neither make the link live nor hand back the title.
        conv = await ch.query_one(
            "SELECT count() AS n FROM loom.conversations "
            "WHERE org = {o:String} AND namespace = {n:String} "
            "AND thread_id = {t:String} AND tombstoned_at IS NULL",
            params,
        )
        thread_exists = bool(conv and int(conv.get("n") or 0) > 0)
        if thread_exists:
            tt = await ch.query_one(
                "SELECT title FROM loom.conversation_threads FINAL "
                "WHERE org = {o:String} AND namespace = {n:String} "
                "AND thread_id = {t:String} AND tombstoned_at IS NULL LIMIT 1",
                params,
            )
            thread_title = (tt or {}).get("title", "") or ""

    # Authored derivation edges (memory.link → loom.operator_edges), both ways.
    ch2 = _get_client()
    edge_rows = await ch2.query_all(
        "SELECT from_key, to_key, relation, label, explanation, evidence, "
        "confidence, author, run_id FROM loom.operator_edges FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} "
        "AND (from_key = {k:String} OR to_key = {k:String}) "
        "AND tombstoned_at IS NULL "
        "ORDER BY created_at DESC LIMIT 50",
        {"o": input.org, "n": input.namespace, "k": input.memory_key},
    )
    links = [
        MemoryProvenanceLink(
            direction="out" if r.get("from_key") == input.memory_key else "in",
            memory_key=(
                r.get("to_key") if r.get("from_key") == input.memory_key else r.get("from_key")
            )
            or "",
            relation=r.get("relation", "") or "",
            label=r.get("label", "") or "",
            explanation=r.get("explanation", "") or "",
            evidence=[str(e) for e in (r.get("evidence") or [])],
            # Float32 on disk: round so 0.9 reads back as 0.9, not 0.8999999761.
            confidence=(
                round(float(r["confidence"]), 4) if r.get("confidence") is not None else 1.0
            ),
            author=r.get("author", "") or "",
            run_id=r.get("run_id", "") or "",
        )
        for r in edge_rows
    ]

    # source_resource_id lives on the embedding row (bloom-indexed), not on
    # loom.memory — read the latest non-empty one for the key.
    emb = await ch2.query_one(
        "SELECT source_resource_id FROM loom.memory_embeddings FINAL "
        "WHERE org = {o:String} AND namespace = {n:String} "
        "AND memory_key = {k:String} AND source_resource_id != '' "
        "AND tombstoned_at IS NULL ORDER BY version DESC LIMIT 1",
        {"o": input.org, "n": input.namespace, "k": input.memory_key},
    )
    source_resource_id = (emb or {}).get("source_resource_id", "") or ""

    return MemoryProvenanceOutput(
        found=True,
        source_resource_id=source_resource_id,
        links=links,
        source_trace=source_trace,
        source_kind=kind,
        source_label=label,
        thread_id=thread_id,
        thread_exists=thread_exists,
        thread_title=thread_title,
        agent=getattr(row, "agent", "") or "",
        session_id=getattr(row, "session_id", "") or "",
        run_id=getattr(row, "run_id", "") or "",
        written_at=str(getattr(row, "created_at", "") or ""),
        actor=getattr(row, "actor", "") or "",
        source_class=getattr(row, "source_class", "") or "",
    )
