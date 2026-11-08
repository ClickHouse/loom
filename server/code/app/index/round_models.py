"""A round's model calls, made together before its files are written.

A commit's files are ingested one after another (headers first), and a file making its
own model calls in turn pays one LLM round trip per new function's description, then one
embedding call for the file; the descriptions alone dominate a commit's time. Nothing
in those calls depends on another file of the round, only on the file's
own new chunks, its card and the prompt. So the round works them out first: it chunks
each parsed file with the ingest's own chunker, reads the file's live chunk index (kept
for the file's own read), and runs every description of the round under one bound, then
every embedding in batches.

The answers are keyed by the exact call a file would make: the description's (system,
user, max_tokens), the embedding's clipped text. A file's own call looks there first and
makes the call itself on a miss, so a prefetch can only save a round trip, never change
a row. A failed call is not recorded (the file retries it on its own path), a held one
included: the file's own call then answers the hold. Nothing here raises: a failing
prefetch leaves the per-file path exactly as it was.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.observability.genai import staged

logger = logging.getLogger("loom.code_index.round_models")


def model_concurrency() -> int:
    """`code_ingest_round_model_concurrency`, at least 1."""
    from server.settings import settings

    return max(1, int(settings.code_ingest_round_model_concurrency))


async def _bounded(limit: int, coros: list[Any]) -> list[Any]:
    gate = asyncio.Semaphore(limit)

    async def one(c: Any) -> Any:
        async with gate:
            try:
                return await c
            except Exception as exc:  # noqa: BLE001 — a miss, the file calls itself
                logger.debug("round models: call skipped (%s)", exc)
                return None

    return await asyncio.gather(*(one(c) for c in coros))


class _Plan:
    """One file's new chunks, as `ingest_file` will compute them."""

    def __init__(
        self, path: str, pf: Any, chunks: list[Any], prefixes: dict[int, str], to_insert: list[Any]
    ) -> None:
        self.path, self.pf, self.chunks, self.prefixes = path, pf, chunks, prefixes
        self.to_insert = to_insert
        self.descriptions: dict[int, str] = {}


async def prefetch_models(
    reads: Any,
    org: str,
    ns: str,
    sources: dict[str, bytes],
    *,
    llm: Any,
    embedder: Any,
    embedding_model: str,
    max_chunk_chars: int,
    embed: bool,
) -> None:
    """Fill `reads.describe_hints` / `reads.embed_hints` for the round's
    files and cache their live chunk indexes on `reads`. Never raises."""
    try:
        plans = await _plans(reads, org, ns, sources, embedding_model, max_chunk_chars)
        if llm is not None:
            await _describe(reads, org, ns, plans, llm, embedding_model)
        if embed and embedder is not None:
            await _embed(ns, plans, embedder, reads)
    except Exception as exc:  # noqa: BLE001 — the per-file path is unchanged
        logger.warning("round models: prefetch skipped (%s)", exc)


async def _plans(
    reads: Any,
    org: str,
    ns: str,
    sources: dict[str, bytes],
    model: str,
    max_chars: int,
) -> list[_Plan]:
    from server.code.app.index.ingest_chunks import _chunks_for
    from server.code.domain.extract.lang_reference import REFERENCE_LANG

    parsed: list[tuple[str, Any, list[Any], dict[int, str]]] = []
    for path, got in reads.parsed.items():
        pf = got[0] if got else None
        if pf is None or path not in sources or pf.lang == REFERENCE_LANG:
            continue
        try:
            # chunking is CPU work (a host file's shell may be parsed again): in a thread
            chunks, prefixes, _facets = await asyncio.to_thread(
                _chunks_for, pf, path, sources[path], max_chars, (org, ns), got[1]
            )
        except Exception:  # noqa: BLE001 — the file's own ingest records the error
            continue
        parsed.append((path, pf, chunks, prefixes))
    indexes = await _bounded(
        model_concurrency(),
        [reads.repo_read("live_chunk_index_for_path", org, ns, p, model) for p, *_ in parsed],
    )
    plans = []
    for (path, pf, chunks, prefixes), rows in zip(parsed, indexes, strict=True):
        if rows is None:
            continue
        reads.cache_chunk_index(path, model, rows)
        live = {str(r["chunk_id"]) for r in rows}
        plans.append(
            _Plan(path, pf, chunks, prefixes, [c for c in chunks if c.chunk_id(ns) not in live])
        )
    return plans


async def _describe(
    reads: Any,
    org: str,
    ns: str,
    plans: list[_Plan],
    llm: Any,
    model: str,
) -> None:
    from server.code.app.index.describe import (
        _DESCRIBE_KINDS,
        _card_for,
        describe_key,
        describe_prompt,
    )

    wanted = [p for p in plans if any(c.kind in _DESCRIBE_KINDS for c in p.to_insert)]
    if not wanted:
        return
    await reads.prefetch_cards(org, ns, model, [p.path for p in wanted])
    calls: list[tuple[_Plan, int, tuple[str, str, int]]] = []
    for plan in wanted:
        # the file's own card read, answered from the round's one statement
        card = await _card_for(reads, org, ns, model, plan.path, True)
        for i, c in enumerate(plan.to_insert):
            if c.kind not in _DESCRIBE_KINDS:
                continue
            calls.append((plan, i, describe_prompt(c, card)))

    got = await _bounded(model_concurrency(), [_describe_call(llm, p) for _, _, p in calls])
    for (plan, i, prompt), line in zip(calls, got, strict=True):
        if line is None:
            continue
        reads.describe_hints[describe_key(prompt)] = line
        if line:
            plan.descriptions[i] = line


@staged("code_describe")
async def _describe_call(llm: Any, prompt: tuple[str, str, int]) -> str:
    """The description call the file would make (`ingest.describe_prompt`)."""
    from server.code.app.index.describe import describe_line

    system, user, max_tokens = prompt
    res = await llm.complete(system, user, max_tokens=max_tokens)
    return describe_line(system, res.text)


async def _embed(ns: str, plans: list[_Plan], embedder: Any, reads: Any) -> None:
    from server.code.app.index.embed_text import clip_for_embedding
    from server.code.app.index.ingest import _EMBED_BATCH
    from server.code.app.index.ingest_chunks import chunk_context_texts

    counter = getattr(embedder, "_count_tokens", None)
    texts: list[str] = []
    seen: set[str] = set()
    for plan in plans:
        if not plan.to_insert:
            continue
        _d, ctx = chunk_context_texts(
            ns,
            plan.path,
            plan.pf,
            plan.chunks,
            plan.to_insert,
            plan.prefixes,
            plan.descriptions,
        )
        for t in ctx:
            clipped = clip_for_embedding(t, count_tokens=counter)
            if clipped not in seen:
                seen.add(clipped)
                texts.append(clipped)
    many = getattr(embedder, "embed_many", None)
    if not texts or many is None:
        return
    batches = [texts[i : i + _EMBED_BATCH] for i in range(0, len(texts), _EMBED_BATCH)]
    got = await _bounded(max(1, model_concurrency() // 4), [many(b) for b in batches])
    for batch, vecs in zip(batches, got, strict=True):
        if vecs is None or len(vecs) != len(batch):
            continue
        for t, v in zip(batch, vecs, strict=True):
            reads.embed_hints[t] = v
