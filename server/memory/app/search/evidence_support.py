"""Belief-source support count for a memory.search hit pool."""

from __future__ import annotations

import logging
from typing import Any

from server.core.ch import get_client
from server.memory.api.models_search import MemorySearchInput
from server.memory.store.reads import count_belief_support

log = logging.getLogger(__package__)


async def _evidence_support_count(input: MemorySearchInput, hits: list[Any]) -> int:
    if not hits:
        return 0
    keys = [(hit.namespace or input.namespace, hit.memory_key) for hit in hits]
    from server.observability.otel import traced

    try:
        with traced("ch.query.memory_belief_sources", op="evidence_support", keys=len(hits)):
            return await count_belief_support(get_client(), input.org, keys)
    except Exception as e:  # noqa: BLE001 — missing belief support must not fail search
        log.warning("evidence support lookup failed (non-fatal): %s", e)
        return 0
