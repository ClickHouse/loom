"""Single source of truth for memory-type classification flags.

Kept as a leaf module (no internal imports) so both handlers and the
search service can read these constants without risking a circular
import. Anywhere that needs to know "is this memory_type searchable /
embedded / queryable by similarity" must import from here.
"""

from __future__ import annotations

# Memory types that don't carry embeddings and don't surface in
# similarity-ranked search:
#
#   episodic: session-scoped, replayed by recency not similarity
#   profile:  always-loaded prompt context (agent profile) — fetched
#             by exact label, never ranked against natural-language
#             queries. Embedding them would waste compute on every
#             profile write and pollute memory.search rankings with the
#             persona/preferences text the agent is supposed to be
#             injecting into the system prompt instead.
#
# Custom taxonomies (observation/decision/... domain-specific types)
# are searchable by default. Add an entry here only when "fetched by
# exact key, never by similarity" is the explicit design intent.
NON_SEARCHABLE_TYPES: frozenset[str] = frozenset({"episodic", "profile"})


# Memory types that ARE embedded (a reflection about a run is worth finding
# by similarity) but stay out of UNFILTERED recall: they are process
# hypotheses — what worked, what failed, what to try — not facts about the
# world, and an answer synthesized from them would present a lesson as a
# claim. Reach them explicitly with memory_type="reflection".
HIDDEN_FROM_UNFILTERED_SEARCH: frozenset[str] = frozenset({"reflection"})


def excluded_from_unfiltered_search() -> list[str]:
    """Every type an unfiltered memory.search / memory.ask leaves out:
    the non-searchable set plus the hidden-by-default set."""
    return sorted(NON_SEARCHABLE_TYPES | HIDDEN_FROM_UNFILTERED_SEARCH)


def is_embedded(memory_type: str, sensitivity: str = "") -> bool:
    """True iff a row of this type AND sensitivity gets an embedding row.

    A 'high' sensitivity row never does: the absence of the
    embedding row is what keeps a credential out of every search leg —
    vector, lexical and graph seed all read loom.memory_embeddings — without
    a WHERE the ANN index would have to post-filter."""
    return is_searchable_type(memory_type) and (sensitivity or "") != "high"


def is_searchable_type(memory_type: str) -> bool:
    """True iff this memory_type gets an embedding row on write AND
    is allowed to appear in unfiltered memory.search results."""
    return memory_type not in NON_SEARCHABLE_TYPES
