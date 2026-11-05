"""knowledge.synthesize: map-reduce over every active chunk of the named
documents, and a calculation over the named datasets."""

from __future__ import annotations

import asyncio
import typing
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
from server.knowledge.domain import datasets as ds
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
        return _abstain("budget_capped")
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
    # One calculation an answer: the plan for the task, cited `[knowledge.query#1]`.
    model = str(getattr(llm, "model", "") or synth_model or "")
    calculation: SynthesizeCalculation | None = None
    if input.datasets:
        done = await _calculate(input, llm, model)
        if isinstance(done, KnowledgeSynthesizeOutput):
            return done
        calculation = done
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

    notes = [_evidence(calculation, input.window_chars)] if calculation is not None else []
    notes += [n for n in await asyncio.gather(*[_map(w) for w in wins]) if n]
    answer, rounds = await _reduce(input, llm, notes, note_tokens)
    # Grounded before the cut, so removed citations take no room; a kept citation the
    # cut halves is no longer one, so `citations` is read again from what is returned.
    by_key = {(c.source_uri, c.idx): c for w in wins for c in w.chunks}
    read = set(by_key) | ({(ap.CALCULATION_SOURCE, 1)} if calculation is not None else set())
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
        calculations=[calculation] if calculation is not None else [],
        model=model,
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


def _abstain(reason: str, clarification: str = "", model: str = "") -> KnowledgeSynthesizeOutput:
    """An abstention; `model` names the LLM only when one was called."""
    return KnowledgeSynthesizeOutput(
        answer="",
        windows=0,
        chunks_read=0,
        documents=0,
        reduce_rounds=0,
        abstain=True,
        abstain_reason=reason,
        clarification=clarification,
        model=model,
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
            )
        )
        found = next((x for x in got.datasets if x.dataset_id == d.dataset_id), None)
        if found is None:
            raise NotFoundError(f"no live dataset {d.dataset_id!r} of {d.source_uri!r}")
        if found.schema_state == "needs_clarification":
            asks = ds.asks([c.model_dump() for c in found.clarifications])
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
    input: KnowledgeSynthesizeInput, llm: Any, model: str
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
            return _abstain("ambiguous", proposal.ask[:1000], model)
        if proposal.plan is None:
            refused = proposal.unreadable
            continue
        unknown = _unknown_keys(KnowledgeQueryInput, proposal.plan)
        if unknown:
            refused = f"unknown fields {', '.join(unknown[:10])}; use only the fields listed"
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
    return _abstain("invalid_plan", refused[:1000], model)


def _unknown_keys(model: type[pydantic.BaseModel], data: Any, path: str = "") -> list[str]:
    """The keys of `data` its model, or a model nested in it, does not declare. The
    models ignore an unknown key, so a misnamed field (`filters` for `where`, `period`
    for `bucket`) would vanish and the plan run without it: a count of every row
    cited as the count asked for."""
    if not isinstance(data, dict):
        return []
    found = []
    for key, value in data.items():
        field = model.model_fields.get(key)
        if field is None:
            found.append(f"{path}{key}")
            continue
        nested = _model_in(field.annotation)
        if nested is None:
            continue
        items = value if isinstance(value, list) else [value]
        for i, item in enumerate(items):
            where = f"{path}{key}.{i}." if isinstance(value, list) else f"{path}{key}."
            found += _unknown_keys(nested, item, where)
    return found


def _model_in(annotation: Any) -> type[pydantic.BaseModel] | None:
    """The model a field holds, alone, in a list or as an optional."""
    if isinstance(annotation, type) and issubclass(annotation, pydantic.BaseModel):
        return annotation
    for arg in typing.get_args(annotation):
        found = _model_in(arg)
        if found is not None:
            return found
    return None


def _why(e: Exception) -> str:
    """A refused plan's reason, short enough to hand back to the model."""
    if isinstance(e, pydantic.ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in e.errors()[:5]
        )
    return str(e)


def _evidence(c: SynthesizeCalculation, window_chars: int) -> str:
    return ap.evidence(
        1,
        c.plan,
        [col.model_dump() for col in c.columns],
        c.rows,
        truncated=c.truncated,
        incomplete=c.incomplete,
        max_chars=window_chars // 2,  # half a window: room for the documents' notes
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
