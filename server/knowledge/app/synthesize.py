"""knowledge.synthesize: map-reduce over every active chunk of the named
documents, and a calculation over the named datasets."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import pydantic

from server.core.errors import NotFoundError, ValidationError
from server.knowledge.api.models_knowledge import (
    KnowledgeDatasetGetInput,
    KnowledgeSynthesizeInput,
    KnowledgeSynthesizeOutput,
    SynthesizeCalculation,
    _CitedChunk,
)
from server.knowledge.api.models_query import KnowledgeQueryInput
from server.knowledge.app.datasets import handle_knowledge_dataset_get
from server.knowledge.app.document_reads import document_chunks
from server.knowledge.app.query import handle_knowledge_query
from server.knowledge.domain import answer_plan as ap
from server.observability.genai import llm_stage
from server.settings.budgets import llm_max_tokens

# Plans proposed at most: one, and one more told why the first was refused.
_PLAN_TRIES = 2  # not a setting: a second chance, not a tunable cost

if TYPE_CHECKING:
    from server.memory.domain.corpus_synthesis import CorpusChunk


async def handle_knowledge_synthesize(
    input: KnowledgeSynthesizeInput,
) -> KnowledgeSynthesizeOutput:
    """Map-reduce over every active chunk of the named documents: windows by section and
    size, one LLM note per window with chunk citations, notes folded into the answer
    (hierarchically when they do not fit one call). With datasets, a calculation over
    them (`_calculate`) is one more note the answer cites. Cost-cap gated like memory.ask;
    abstains, never guesses, when there is nothing to read, the namespace is capped, a
    dataset's schema needs an answer, the model would need to ask first or proposes no
    plan that holds, or the answer is empty as written or once citations of what it did
    not read are removed."""
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
    if not chunks and not input.datasets:
        return _abstain("no_chunks")
    synth_model = _s.memory_ask_synth_model.strip()
    if synth_model:
        from server.llm.client import get_llm_client_for_model

        llm = get_llm_client_for_model(synth_model)
    else:
        from server.llm.client import get_llm_client

        llm = get_llm_client()
    calculations: list[SynthesizeCalculation] = []
    if input.datasets:
        done = await _calculate(input, llm)
        if isinstance(done, KnowledgeSynthesizeOutput):
            done.model = str(getattr(llm, "model", "") or synth_model or "")
            return done
        calculations.append(done)
    wins = cs.windows(chunks, window_chars=input.window_chars)
    truncated = len(wins) > input.max_windows
    wins = wins[: input.max_windows]
    note_tokens = max(256, min(2000, input.window_chars // 12))

    async def _map(w: cs.Window) -> str:
        with llm_stage("knowledge_synthesize_map"):
            res = await llm.complete(
                cs.MAP_SYSTEM, cs.map_prompt(input.task, w), max_tokens=note_tokens
            )
        return (getattr(res, "text", "") or "").strip()

    notes = [_evidence(i + 1, c) for i, c in enumerate(calculations)]
    notes += [n for n in await asyncio.gather(*[_map(w) for w in wins]) if n]
    answer, rounds = await _reduce(input, llm, notes, note_tokens)
    # Grounded before the cut, so removed citations take no room; a kept citation the
    # cut halves is no longer one, so `citations` is read again from what is returned.
    by_key = {(c.source_uri, c.idx): c for w in wins for c in w.chunks}
    read = set(by_key) | {(ap.CALCULATION_SOURCE, i + 1) for i in range(len(calculations))}
    answer, cited, unsupported = cs.ground_citations(answer, read)
    if len(answer) > input.max_answer_chars:
        answer = answer[: input.max_answer_chars].rstrip() + "…"
        cited = cs.ground_citations(answer, read)[1]
    # Punctuation or list markers left by the removed citations are no answer either.
    if not any(ch.isalnum() for ch in answer):
        answer = ""
    located = _located(by_key, [k for k in cs.cited_chunks(cited, read) if k in by_key])
    return KnowledgeSynthesizeOutput(
        answer=answer,
        windows=len(wins),
        chunks_read=sum(len(w.chunks) for w in wins),
        documents=docs,
        reduce_rounds=rounds,
        citations=cited,
        cited_chunks=located,
        unsupported_citations=unsupported,
        truncated=truncated,
        abstain=not answer,
        abstain_reason="" if answer else "empty_answer",
        calculations=calculations,
        model=str(getattr(llm, "model", "") or synth_model or ""),
    )


async def _reduce(
    input: KnowledgeSynthesizeInput, llm: Any, notes: list[str], note_tokens: int
) -> tuple[str, int]:
    """The notes folded into the answer, in as many rounds as they need (at most
    nine; past them the best notes so far are the answer), and the rounds."""
    from server.memory.domain import corpus_synthesis as cs

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
    return answer, rounds


def _abstain(reason: str, clarification: str = "") -> KnowledgeSynthesizeOutput:
    return KnowledgeSynthesizeOutput(
        answer="",
        windows=0,
        chunks_read=0,
        documents=0,
        reduce_rounds=0,
        abstain=True,
        abstain_reason=reason,
        clarification=clarification,
    )


async def _schemas(
    input: KnowledgeSynthesizeInput,
) -> list[dict[str, Any]] | KnowledgeSynthesizeOutput:
    """Each named dataset's alias, name, row count and stored columns, or the
    abstention when one's schema needs a person's answer. A dataset that is not
    live is not found, as knowledge.query answers."""
    out = []
    for d in input.datasets:
        got = await handle_knowledge_dataset_get(
            KnowledgeDatasetGetInput(
                org=input.org,
                namespace=input.namespace,
                source_uri=d.source_uri,
                dataset_id=d.dataset_id,
            )  # fmt: skip
        )
        found = next((x for x in got.datasets if x.dataset_id == d.dataset_id), None)
        if found is None:
            raise NotFoundError(f"no live dataset {d.dataset_id!r} of {d.source_uri!r}")
        if found.schema_state == "needs_clarification":
            asks = "; ".join(f"{c.code} {c.column}".strip() for c in found.clarifications[:5])
            return _abstain(
                "needs_clarification",
                f"dataset {d.alias!r} needs its schema answered ({asks}):"
                " knowledge.dataset_approve",
            )
        out.append(
            {
                "alias": d.alias,
                "name": found.name,
                "row_count": found.row_count,
                "columns": [{"name": c.name, "type": c.type} for c in found.columns if c.stored],
            }
        )
    return out


async def _calculate(
    input: KnowledgeSynthesizeInput, llm: Any
) -> SynthesizeCalculation | KnowledgeSynthesizeOutput:
    """One plan the model proposes from the task and the datasets' schemas, run
    by knowledge.query, which checks it against them; a refused plan is proposed
    again once, with why. Or the abstention: the schema needs an answer, the
    model would need to ask first, or no plan it proposed held."""
    schemas = await _schemas(input)
    if isinstance(schemas, KnowledgeSynthesizeOutput):
        return schemas
    refused = ""
    for _ in range(_PLAN_TRIES):
        with llm_stage("knowledge_synthesize_plan"):
            res = await llm.complete(
                ap.PLAN_SYSTEM,
                ap.plan_prompt(input.task, schemas, refused),
                max_tokens=llm_max_tokens("knowledge_synthesize_plan"),
            )
        proposal = ap.read_proposal(getattr(res, "text", "") or "")
        if proposal.ask:
            return _abstain("ambiguous", proposal.ask[:1000])
        if proposal.plan is None:
            refused = proposal.unreadable
            continue
        try:
            query = KnowledgeQueryInput(
                org=input.org, namespace=input.namespace, datasets=input.datasets, **proposal.plan
            )
            out = await handle_knowledge_query(query)
        except (pydantic.ValidationError, ValidationError) as e:
            refused = _why(e)
            continue
        return SynthesizeCalculation(
            citation=f"{ap.CALCULATION_SOURCE}#1",
            plan=out.receipt.plan,
            columns=out.columns,
            rows=out.rows,
            truncated=out.truncated,
            complete=out.complete,
            incomplete=out.incomplete,
            receipt=out.receipt,
        )
    return _abstain("invalid_plan", refused[:1000])


def _why(e: Exception) -> str:
    """A refused plan's reason, short enough to hand back to the model."""
    if isinstance(e, pydantic.ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]
        )
    return str(e)


def _evidence(n: int, c: SynthesizeCalculation) -> str:
    return ap.evidence(
        n,
        [col.model_dump() for col in c.columns],
        c.rows,
        truncated=c.truncated,
        incomplete=c.incomplete,
    )


def _located(
    by_key: dict[tuple[str, int], CorpusChunk], chunks: list[tuple[str, int]]
) -> list[_CitedChunk]:
    """Each cited chunk with the revision it was written as and its locator, as read."""
    return [
        _CitedChunk(
            source_uri=uri,
            chunk_idx=idx,
            revision=by_key[(uri, idx)].revision,
            locator=by_key[(uri, idx)].locator,
        )
        for uri, idx in chunks
    ]


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
