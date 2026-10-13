"""The query text the search embeds, in the namespace's own spelling of its
lowercased words (`server.memory.domain.query_casing`)."""

from __future__ import annotations

import asyncio
import logging

from server.core.ch import get_client
from server.memory.domain.query_casing import casing_candidates, pick_spellings
from server.memory.store.query_spellings import spelling_counts

log = logging.getLogger(__package__)

# The lookup rides the search's critical path ahead of the embed; a slow read
# embeds the query as typed.
_LOOKUP_BOUND_S = 0.5


async def corpus_spellings(org: str, namespace: str, query: str) -> dict[str, str]:
    """lowercase query word -> the spelling the namespace's rows use most, for
    the words whose spelling differs; {} when off, nothing differs or the read
    fails."""
    from server.settings import settings

    tokens = casing_candidates(query, max_words=settings.memory_search_corpus_casing_max_words)
    if not tokens or not org or not namespace:
        return {}
    try:
        counts = await asyncio.wait_for(
            spelling_counts(get_client(), org=org, namespace=namespace, tokens=tokens),
            timeout=_LOOKUP_BOUND_S,
        )
    except Exception as e:  # noqa: BLE001 — the spelling is a recall aid; the query embeds as typed
        log.debug("query spelling lookup failed (non-fatal): %s", e)
        return {}
    return pick_spellings(tokens, counts)
