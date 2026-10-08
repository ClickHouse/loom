"""The epistemic envelope of a graph answer.

`counts_floor: true` says that every count from the call graph is a floor.
It does not say WHY this particular answer is one, or whether this one is
in fact exact. Both are knowable from what the handler already holds:

  exact        the compiler tier indexed every seed file, no call site named
               like a seed was declined by the fast-tier resolver, and no
               override seed was left without its base declaration. The
               edges ARE the callers (modulo dynamic dispatch through
               function pointers, which no static index sees).
  lower_bound  at least one of those failed; `causes` names which, as
               machine-readable tokens an agent can branch on:
                 unresolved_calls        declined sites, no definition indexed
                 ambiguous_calls         declined sites, several definitions
                 dispatch_seeds_missing  a method seed with no resolvable base
                                         in a namespace whose compiler tier
                                         carries no override relationships
                 treesitter_only_files   a seed file only the parser indexed
                 external_calls          a seed file the index holds no
                                         symbols for at all (outside the
                                         indexed prefixes, unsupported
                                         language, or an index error) —
                                         its callers are external to the graph
                 scip_stale_files        a compiler-tier edge served from a file
                                         the fast tier rewrote after the compiler
                                         tier's last run: its line and caller come
                                         from content that tier never saw
                 truncated               the walk stopped short of the graph (a node
                                         cap, a hop read cut at its row limit, a
                                         page): the answer lists part of it

Pure: no I/O, no settings import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Epistemic = Literal["exact", "lower_bound"]

CAUSE_UNRESOLVED = "unresolved_calls"
CAUSE_AMBIGUOUS = "ambiguous_calls"
CAUSE_DISPATCH = "dispatch_seeds_missing"
CAUSE_TREESITTER = "treesitter_only_files"
CAUSE_EXTERNAL = "external_calls"
# A constructor seed with an empty walk in a namespace whose index predates constructor
# calls through factories (`std::make_shared<T>`): the class may be constructed on every
# other line and the graph would not know. Cleared once the tier that answers for the
# seed carries them.
CAUSE_CTOR = "ctor_edges_absent"
# A served scip edge whose file changed after the compiler tier last ran: the row
# survives from an older tree (its call may have moved or gone), so it is no proof.
CAUSE_STALE = "scip_stale_files"
# The walk stopped before the graph did: whatever else holds, the rows are part of it.
CAUSE_TRUNCATED = "truncated"
CAUSES: tuple[str, ...] = (
    CAUSE_UNRESOLVED,
    CAUSE_AMBIGUOUS,
    CAUSE_DISPATCH,
    CAUSE_TREESITTER,
    CAUSE_EXTERNAL,
    CAUSE_CTOR,
    CAUSE_STALE,
    CAUSE_TRUNCATED,
)


@dataclass
class Envelope:
    epistemic: str
    causes: list[str] = field(default_factory=list)


def classify(
    *,
    unresolved: int,
    ambiguous: int,
    seed_paths: list[str],
    scip_paths: set[str],
    unindexed_paths: set[str] | None = None,
    dispatch_missing: bool = False,
    ctor_edges_absent: bool = False,
    scip_stale: bool = False,
    truncated: bool = False,
) -> tuple[str, list[str]]:
    """(epistemic, causes). Order of causes is the enum order above so two
    answers with the same gaps render the same list."""
    causes: list[str] = []
    if unresolved > 0:
        causes.append(CAUSE_UNRESOLVED)
    if ambiguous > 0:
        causes.append(CAUSE_AMBIGUOUS)
    if dispatch_missing:
        causes.append(CAUSE_DISPATCH)
    unindexed = unindexed_paths or set()
    if any(p and p not in scip_paths and p not in unindexed for p in seed_paths):
        causes.append(CAUSE_TREESITTER)
    if any(p in unindexed for p in seed_paths):
        causes.append(CAUSE_EXTERNAL)
    if ctor_edges_absent:
        causes.append(CAUSE_CTOR)
    if scip_stale:
        causes.append(CAUSE_STALE)
    if truncated:
        causes.append(CAUSE_TRUNCATED)
    return ("exact" if not causes else "lower_bound"), causes


def scip_coverage_from_rows(rows: list[dict[str, Any]]) -> set[str]:
    """Paths proven compiler-indexed by rows already in hand (a symbol row
    with tier == 'scip' in that path). Whatever is not in the result is
    unproven, not disproven — the repo read settles the remainder."""
    return {
        str(r.get("path") or "")
        for r in rows
        if str(r.get("tier") or "") == "scip" and r.get("path")
    }


def envelope(
    *,
    unresolved: int,
    ambiguous: int,
    seed_paths: list[str],
    scip_paths: set[str],
    unindexed_paths: set[str] | None = None,
    dispatch_missing: bool = False,
    ctor_edges_absent: bool = False,
    scip_stale: bool = False,
    truncated: bool = False,
) -> Envelope:
    """Classify a graph answer: epistemic and causes."""
    ep, causes = classify(
        unresolved=unresolved,
        ambiguous=ambiguous,
        seed_paths=seed_paths,
        scip_paths=scip_paths,
        unindexed_paths=unindexed_paths,
        dispatch_missing=dispatch_missing,
        ctor_edges_absent=ctor_edges_absent,
        scip_stale=scip_stale,
        truncated=truncated,
    )
    return Envelope(epistemic=ep, causes=causes)
