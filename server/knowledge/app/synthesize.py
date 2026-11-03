"""knowledge.synthesize: map-reduce over every active chunk of the named documents."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from server.knowledge.api.models_knowledge import (
    KnowledgeSynthesizeInput,
    KnowledgeSynthesizeOutput,
)
from server.knowledge.app.document_reads import document_chunks
from server.observability.genai import llm_stage

if TYPE_CHECKING:
    from server.memory.domain.corpus_synthesis import CorpusChunk


async def handle_knowledge_synthesize(
    input: KnowledgeSynthesizeInput,
) -> KnowledgeSynthesizeOutput:
    """Map-reduce over every active chunk of the named documents: windows by section and
    size, one LLM note per window with chunk citations, notes folded into the answer
    (hierarchically when they do not fit one call). Cost-cap gated like memory.ask;
    abstains, never guesses, when the documents are empty, the namespace is capped, or the
    answer is empty once citations of chunks it did not read are removed."""
    from server.memory.domain import corpus_synthesis as cs
    from server.observability.budget import namespace_over_cap
    from server.settings import settings as _s

    if await namespace_over_cap(input.org, input.namespace):
        return KnowledgeSynthesizeOutput(
            answer="",
            windows=0,
            chunks_read=0,
            documents=0,
            reduce_rounds=0,
            abstain=True,
            abstain_reason="budget_capped",
        )
    chunks, docs = await _corpus_chunks(input)
    if not chunks:
        return KnowledgeSynthesizeOutput(
            answer="",
            windows=0,
            chunks_read=0,
            documents=0,
            reduce_rounds=0,
            abstain=True,
            abstain_reason="no_chunks",
        )
    wins = cs.windows(chunks, window_chars=input.window_chars)
    truncated = len(wins) > input.max_windows
    wins = wins[: input.max_windows]

    synth_model = _s.memory_ask_synth_model.strip()
    if synth_model:
        from server.llm.client import get_llm_client_for_model

        llm = get_llm_client_for_model(synth_model)
    else:
        from server.llm.client import get_llm_client

        llm = get_llm_client()
    note_tokens = max(256, min(2000, input.window_chars // 12))

    async def _map(w: cs.Window) -> str:
        with llm_stage("knowledge_synthesize_map"):
            res = await llm.complete(
                cs.MAP_SYSTEM, cs.map_prompt(input.task, w), max_tokens=note_tokens
            )
        return (getattr(res, "text", "") or "").strip()

    notes = list(await asyncio.gather(*[_map(w) for w in wins]))
    notes = [n for n in notes if n]
    rounds = 0
    answer_tokens = max(128, min(4000, input.max_answer_chars // 4 + 32))
    while True:
        batches = cs.fold(notes, limit_chars=input.window_chars)
        rounds += 1
        if len(batches) == 1:
            with llm_stage("knowledge_synthesize_reduce"):
                res = await llm.complete(
                    cs.REDUCE_SYSTEM,
                    cs.reduce_prompt(input.task, batches[0], final=True),
                    max_tokens=answer_tokens,
                )
            answer = (getattr(res, "text", "") or "").strip()
            break
        merged: list[str] = []
        for b in batches:
            with llm_stage("knowledge_synthesize_reduce"):
                res = await llm.complete(
                    cs.REDUCE_SYSTEM,
                    cs.reduce_prompt(input.task, b, final=False),
                    max_tokens=note_tokens,
                )
            merged.append((getattr(res, "text", "") or "").strip())
        notes = [n for n in merged if n] or [""]
        if rounds > 8:  # pathological: give the best we have
            answer = "\n\n".join(notes)
            break
    # Grounded before the cut, so removed citations take no room; a kept citation the
    # cut halves is no longer one, so `citations` is read again from what is returned.
    read = {(c.source_uri, c.idx) for w in wins for c in w.chunks}
    answer, cited, unsupported = cs.ground_citations(answer, read)
    if len(answer) > input.max_answer_chars:
        answer = answer[: input.max_answer_chars].rstrip() + "…"
        cited = cs.ground_citations(answer, read)[1]
    return KnowledgeSynthesizeOutput(
        answer=answer,
        windows=len(wins),
        chunks_read=sum(len(w.chunks) for w in wins),
        documents=docs,
        reduce_rounds=rounds,
        citations=cited,
        unsupported_citations=unsupported,
        truncated=truncated,
        # An answer that is empty, as written or once unread citations are removed, abstains.
        abstain=not answer,
        abstain_reason="" if answer else "empty_answer",
        model=str(getattr(llm, "model", "") or synth_model or ""),
    )


async def _corpus_chunks(input: KnowledgeSynthesizeInput) -> tuple[list[CorpusChunk], int]:
    """Every active chunk of the named documents, in document order, and how
    many of the documents had any."""
    chunks: list[CorpusChunk] = []
    docs = 0
    for uri in input.source_uris:
        doc_chunks = await document_chunks(input.org, input.namespace, uri)
        if doc_chunks:
            docs += 1
        chunks.extend(doc_chunks)
    return chunks, docs
