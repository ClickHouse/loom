"""Canonical verbs for authored edges (memory.link → loom.operator_edges).

`relation` is part of the edge's ORDER BY, so "contradicts", "Contradicts"
and "conflicts with" would be three edges. Writes store one canonical
verb: the same normalization the triple plane uses (graph_quality.
canonical_relation: lowercase, spaces → underscores, punctuation dropped)
plus a small synonym map for the verbs agents actually assert about
memories. Unknown verbs pass through normalized — the set is a preference,
not a gate; `related_to` is the honest fallback when nothing sharper fits.

`contradicts` is special: asserting it opens a conflict between the two
memories (memory/app/conflicts.mark_peers_conflicted), so an agent-noticed
contradiction reaches conflicts.list next to the ones loom detects itself.
"""

from __future__ import annotations

from typing import NamedTuple

# Preferred verbs, in the order the MCP description lists them.
CANONICAL_RELATIONS: tuple[str, ...] = (
    "supports",
    "depends_on",
    "caused",
    "influenced",
    "contradicts",
    "refines",
    "implements",
    "validates",
    "supersedes",
    "blocks",
    "fixes",
    "derived_from",
    "precedes",
    "related_to",
)

_SYNONYMS: dict[str, str] = {
    # supports
    "support": "supports",
    "evidence_for": "supports",
    "confirms_that": "supports",
    "corroborates": "supports",
    "backs": "supports",
    # depends_on
    "depend_on": "depends_on",
    "requires": "depends_on",
    "needs": "depends_on",
    "prerequisite": "depends_on",
    "depends": "depends_on",
    # caused
    "causes": "caused",
    "cause": "caused",
    "led_to": "caused",
    "leads_to": "caused",
    "resulted_in": "caused",
    "results_in": "caused",
    "triggered": "caused",
    # influenced (weaker than caused; decision chains)
    "influence": "influenced",
    "influences": "influenced",
    "shaped": "influenced",
    "informed": "influenced",
    "influenced_by": "influenced",
    # contradicts
    "contradict": "contradicts",
    "conflicts_with": "contradicts",
    "conflicts": "contradicts",
    "disagrees_with": "contradicts",
    "refutes": "contradicts",
    "contradicted_by": "contradicts",
    # refines
    "refine": "refines",
    "extends": "refines",
    "elaborates": "refines",
    "specializes": "refines",
    "clarifies": "refines",
    "narrows": "refines",
    # implements
    "implement": "implements",
    "realizes": "implements",
    "fulfils": "implements",
    "fulfills": "implements",
    # validates
    "validate": "validates",
    "confirms": "validates",
    "verified_by": "validates",
    "verifies": "validates",
    "proves": "validates",
    # supersedes
    "supersede": "supersedes",
    "replaces": "supersedes",
    "obsoletes": "supersedes",
    "deprecates": "supersedes",
    "overrides": "supersedes",
    "succeeds": "supersedes",
    # blocks
    "block": "blocks",
    "blocked_by": "blocks",
    "prevents": "blocks",
    "gates": "blocks",
    # fixes
    "fix": "fixes",
    "fixed": "fixes",
    "resolves": "fixes",
    "resolved": "fixes",
    "solves": "fixes",
    "addresses": "fixes",
    # derived_from
    "derived": "derived_from",
    "derives_from": "derived_from",
    "based_on": "derived_from",
    "driven_by": "derived_from",
    "informed_by": "derived_from",
    "from": "derived_from",
    # precedes (temporal order; the digest chain). "A follows B"
    # and "A after B" say B precedes A, so they flip.
    "precede": "precedes",
    "preceded": "precedes",
    "before": "precedes",
    "followed_by": "precedes",
    "preceded_by": "precedes",
    "follows": "precedes",
    "after": "precedes",
    "then": "precedes",
    # related_to
    "related": "related_to",
    "relates_to": "related_to",
    "associated_with": "related_to",
    "see_also": "related_to",
    "linked_to": "related_to",
}

# Verbs whose subject→object direction is the reverse of what the caller
# wrote: "A blocked_by B" means B blocks A. Canonicalizing flips the edge so
# graph reads see one direction per verb.
_REVERSED: frozenset[str] = frozenset(
    {
        "blocked_by",
        "contradicted_by",
        "verified_by",
        "influenced_by",
        "preceded_by",
        "follows",
        "after",
    }
)


def canonical_link_relation(raw: str) -> tuple[str, bool]:
    """(canonical verb, flip) for an authored edge. `flip` is True when the
    caller's from/to should be swapped to express the canonical direction."""
    from server.memory.domain.graph_quality import canonical_relation

    verb = canonical_relation(raw or "").verb
    if not verb:
        return "", False
    flip = verb in _REVERSED
    return _SYNONYMS.get(verb, verb), flip


class CanonicalEdge(NamedTuple):
    from_key: str
    to_key: str
    relation: str
    defaulted: bool  # no verb was given, so the edge is `related_to`


def canonical_edge(from_key: str, to_key: str, relation: str) -> CanonicalEdge:
    """The edge as memory.link stores it: the canonical verb, `related_to` when there
    is none, and from/to swapped for a reversed verb (`blocked_by` is stored as `blocks`)."""
    verb, flip = canonical_link_relation(relation)
    if flip:
        from_key, to_key = to_key, from_key
    return CanonicalEdge(from_key, to_key, verb or "related_to", defaulted=not verb)
