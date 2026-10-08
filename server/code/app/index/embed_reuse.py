"""Exact cross-namespace embedding reuse for the code plane.

A release branch shares most of its files byte-for-byte with the default
branch, and `loom.code_chunks` already stores, per chunk, the `content_hash`
and the exact `context_text` that was embedded. So before paying the provider
for a chunk, look for a live row in the configured source namespace with the
same path, the same content hash and the *identical* context text, and copy
its vector. Identity is the embedded text itself, never a guess: a chunk whose
LLM description differed embeds afresh.

Bounded: one query per file, keyed on the primary-key prefix
(org, namespace, embedding_model) plus `path`, so it costs a few ms.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("loom.code_index.embed_reuse")


async def reusable_vectors(
    repo: Any,
    org: str,
    source_ns: str,
    *,
    embedding_model: str,
    path: str,
    content_hashes: list[str],
    context_texts: list[str],
) -> dict[int, list[float]]:
    """`{index -> embedding}` for every context text that already has a live
    vector in `source_ns`. Empty on any error (the caller then embeds)."""
    if not source_ns or not content_hashes:
        return {}
    try:
        rows = await repo.embeddings_for_texts(
            org,
            source_ns,
            embedding_model=embedding_model,
            path=path,
            content_hashes=content_hashes,
        )
    except Exception as e:  # noqa: BLE001 — reuse is an optimisation, never a failure
        logger.debug("embed reuse lookup failed for %s: %s", path, e)
        return {}
    by_text: dict[str, list[float]] = {}
    for r in rows:
        vec = r.get("embedding")
        if isinstance(vec, list) and vec:
            by_text.setdefault(str(r.get("context_text") or ""), [float(x) for x in vec])
    if not by_text:
        return {}
    return {i: by_text[t] for i, t in enumerate(context_texts) if t in by_text}
