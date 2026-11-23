"""Session digests built only from authoritative, committed facts."""

from __future__ import annotations

from typing import Any

from server.core.ch import get_client
from server.core.datetimes import naive_utc_now, parse_iso_dt
from server.embedding import get_embedder
from server.embedding._provider import MalformedEmbedding
from server.embedding.documents import document_vector
from server.identity.domain.read_scope import WHOLE_NAMESPACE
from server.memory.domain.sensitivity import value_reads_high
from server.memory.domain.summary_provenance import render_summary, source_text
from server.observability.genai import staged
from server.settings import settings

_SESSION_SUMMARY_MAX_KEY_FACTS = 6


@staged("session_summary")
async def _write_session_summary(input: Any, state: Any) -> None:
    """Persist an extractive digest with enough lineage to reconstruct every byte."""
    import server.memory.store as memory_repo
    from server.memory.app.digest_chain import link_digest

    ch = get_client()
    identity = tuple(
        str(getattr(input, axis, "-") or "-") for axis in ("agent", "user_id", "session_id")
    )
    keys: list[str] = []
    facts: list[str] = []
    cap = int(settings.memory_session_digest_max_input_chars)
    if cap == 0:
        cap = 3000  # Zero selects the documented built-in character budget.
    cap = max(500, cap)
    for written in state.written or []:
        memory_id = str(written.get("memory_id") or "")
        version = int(written.get("version") or 0)
        if not memory_id or not version:
            continue
        source = await memory_repo.get_by_id(
            memory_id,
            org=input.org,
            namespace=input.namespace,
            scope=WHOLE_NAMESPACE,
            ch=ch,
            strong=True,
            fresh=True,
            include_usage_totals=False,
        )
        if source is None or int(source.version) != version:
            continue
        source_identity = (source.agent or "-", source.user_id or "-", source.session_id or "-")
        if source_identity != identity or source.memory_key in keys:
            continue
        text = source_text(source, now=naive_utc_now())
        if not text or len(render_summary([*facts, text])) > cap:
            continue
        keys.append(source.memory_key)
        facts.append(text)
        if len(facts) >= _SESSION_SUMMARY_MAX_KEY_FACTS:
            break
    if not facts:
        return
    summary = render_summary(facts)
    if value_reads_high(summary):
        return
    embedder = get_embedder()
    try:
        embedding = await document_vector(embedder, summary)
    except MalformedEmbedding:
        return
    row = memory_repo.build_session_summary_row(
        org=input.org,
        namespace=input.namespace,
        agent=identity[0],
        user_id=identity[1],
        session_id=identity[2],
        run_id=str(getattr(input, "run_id", "") or "-"),
        summary_text=summary,
        summary_embedding=embedding,
        embedding_model=embedder.model_name,
        embedding_dim=len(embedding),
        key_facts=facts,
        derived_from=keys,
        valid_at=parse_iso_dt(getattr(state, "observation_date", None)),
        created_at=naive_utc_now(),
    )
    from server.memory.store.summary_sources import insert_verified_summaries

    if not await insert_verified_summaries(ch, [row]):
        return
    await link_digest(
        ch,
        org=input.org,
        namespace=input.namespace,
        agent=identity[0],
        user_id=identity[1],
        session_id=identity[2],
        at=row.get("valid_at") or row["created_at"],
    )
