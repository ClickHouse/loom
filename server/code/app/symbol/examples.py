"""code.examples: call sites of a symbol with the body around each."""

from __future__ import annotations

import asyncio
import time

from server.code.api.models_navigation import CodeExamplesInput, CodeExamplesOutput
from server.code.app.common import (
    new_trace_run_id,
    require_code_intel,
    resolve_code_identity,
    schedule_code_trace,
    tier_of,
)
from server.code.app.spans import code_span, set_code_span_attributes, span_attrs
from server.code.app.symbol.resolve import _resolve_symbols, _seed_ids_of
from server.code.store.index import CodeRepo
from server.core.errors import NotFoundError


async def handle_code_examples(input: CodeExamplesInput) -> CodeExamplesOutput:
    from server.code.app import enrich as code_enrich

    require_code_intel()
    started = time.monotonic()
    org, ns = resolve_code_identity(input.org, input.namespace)
    repo = CodeRepo()
    with code_span("examples.resolve") as sp:
        seeds = await _resolve_symbols(repo, org, ns, symbol_id=input.symbol_id, name=input.name)
        span_attrs(sp, results=len(seeds), tier=tier_of(seeds))
    if not seeds:
        raise NotFoundError(f"symbol not found: {input.name or input.symbol_id}")
    seed_ids = _seed_ids_of(seeds)
    with code_span("examples.sites", seeds=len(seed_ids)) as sp:
        examples = await code_enrich.usage_examples(repo, org, ns, seed_ids, limit=input.top_k)
        # attach the call-site unit body for each example
        bodies = await asyncio.gather(
            *(_site_body(repo, org, ns, ex["path"], ex["line"]) for ex in examples)
        )
        out = [{**ex, "body": body} for ex, body in zip(examples, bodies, strict=True)]
        span_attrs(sp, results=len(out))
    trace_run_id = new_trace_run_id()
    schedule_code_trace(
        org=org,
        ns=ns,
        op="code.examples",
        query=input.name or input.symbol_id,
        route="examples_usage",
        returned_ids=seed_ids,
        returned_paths=[str(e.get("path") or "") for e in out],
        returned_scores=[],
        top_k=input.top_k,
        tokens_returned=sum(len(e["body"]) for e in out) // 4,
        abstain=not out,
        retrieval_risk="low" if out else "abstain",
        embedding_model="",
        started_monotonic=started,
        trace_run_id=trace_run_id,
        consumer=input.consumer,
        agent=input.agent,
        user_id=input.user_id,
    )
    set_code_span_attributes(
        route="examples_usage",
        results=len(out),
        seeds=len(seed_ids),
        abstain=not out,
        retrieval_risk="low" if out else "abstain",
        consumer=input.consumer,
    )
    return CodeExamplesOutput(examples=out, trace_run_id=trace_run_id)


async def _site_body(repo: CodeRepo, org: str, ns: str, path: str, line: int) -> str:
    return (await repo.innermost_chunk_at(org, ns, path, line) or "")[:4000]
