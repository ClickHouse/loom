"""A chunk is embedded with its context.

Contextual retrieval prepends 50–100 tokens of chunk-specific context,
written by a model from the whole document, to each chunk before it is
embedded and indexed. On a temporal-dialogue bench it lifted evidence
recall@10 from 0.574 to 0.633 and exact match by 7.5 points.

This module is the write-path form. `memory.bulk_set(contextualize=true)`
treats the entries of one call as one document (a session, a file, a
page) and, for each entry, asks the model for a one- or two-sentence
context: who, when, what the passage is about. The context is prepended to
the text that is embedded and to `content_excerpt` (which the lexical legs
index and the reader sees). `value` stays byte-identical: the context is a
retrieval aid, not a fact.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from server.observability.genai import llm_stage

log = logging.getLogger(__name__)

STAGE = "bulk_contextualize"

SYSTEM = (
    "You situate a passage within the document it was taken from so that a search can "
    "find the passage. Answer only with the context: one or two sentences naming who is "
    "involved, when (if the document says), and what the passage is about. No preamble, "
    "no quotation of the passage, no judgement."
)

USER = (
    "<document>\n{document}\n</document>\n"
    "<chunk>\n{chunk}\n</chunk>\n"
    "Give the short, succinct context that situates this chunk within the overall "
    "document for the purposes of improving search retrieval of the chunk."
)


def build_prompt(document: str, chunk: str, *, max_document_chars: int = 24000) -> tuple[str, str]:
    """(system, user) for one chunk. The document is clipped from the end so
    the chunk's neighbourhood (usually near it) survives a long session."""
    doc = document
    if len(doc) > max_document_chars:
        # keep the window around the chunk when it can be located
        pos = doc.find(chunk[:200]) if chunk else -1
        if pos >= 0:
            half = max_document_chars // 2
            start = max(0, pos - half)
            doc = doc[start : start + max_document_chars]
        else:
            doc = doc[:max_document_chars]
    return SYSTEM, USER.format(document=doc, chunk=chunk)


def clean_context(text: str, *, max_chars: int = 400) -> str:
    """One line, capped; empty when the model returned nothing usable."""
    t = " ".join((text or "").split())
    for prefix in ("Context:", "context:", "Succinct context:"):
        if t.startswith(prefix):
            t = t[len(prefix) :].strip()
    return t[:max_chars].strip()


async def contextualize_chunks(
    llm: Any,
    document: str,
    chunks: list[str],
    *,
    concurrency: int = 8,
    max_context_chars: int = 400,
    max_tokens: int = 160,
    operator_policy: str,
) -> list[str]:
    """One context per chunk, in order; "" where the model failed (fail-open:
    that chunk is embedded as it is). One `llm_stage` for the whole batch. The
    context is written from the caller's text and stored with it, so each call
    ends with the operator's extraction policy."""
    from server.memory.domain.extraction_policy import with_policy

    if not chunks:
        return []
    sem = asyncio.Semaphore(max(1, concurrency))

    async def one(chunk: str) -> str:
        system, user = build_prompt(document, chunk)
        system = with_policy(system, operator_policy)
        async with sem:
            try:
                res = await llm.complete(system=system, user=user, max_tokens=max_tokens)
            except Exception as e:  # noqa: BLE001 — a missing context is not a failed write
                log.debug("contextualize: model call failed (chunk embedded plain): %s", e)
                return ""
        return clean_context(getattr(res, "text", "") or "", max_chars=max_context_chars)

    with llm_stage(STAGE):
        return list(await asyncio.gather(*[one(c) for c in chunks]))


def prefixed(context: str, text: str) -> str:
    return f"{context}\n{text}" if context else text
