"""What a namespace's code-linked memories go through when their code changes.

A memory about code describes something Loom indexes, so once that code is gone,
serving it as current is wrong. Loom never writes the memory; the namespace owner picks
what reads do:

  retire_removed (default)  search leaves out a memory whose linked code is all gone;
                            `memory.get` still shows it, and it is back the moment
                            the code is; changes are flagged
  flag                      links are marked, every read still serves the memory
  revise                    as retire_removed, and a changed claim is re-checked
                            against the new code by the LLM: one that still holds
                            is re-anchored, a contradicted one gets a suggested
                            correction on its link

A memory a person pinned, locked or verified is only flagged.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Literal, get_args

from server.memory.domain.lock import effective_mode
from server.memory.domain.trust import verification_state

# a served link's states from best to worst: a memory shows its worst link's
CodeState = Literal["fresh", "moved", "changed", "unresolved", "missing"]
STATE_RANK: dict[str, int] = {s: i for i, s in enumerate(get_args(CodeState))}

CodeChangePolicy = Literal["retire_removed", "flag", "revise"]
POLICIES: tuple[CodeChangePolicy, ...] = get_args(CodeChangePolicy)
RETIRE_REMOVED, FLAG, REVISE = POLICIES


def policy_of(raw: str) -> CodeChangePolicy:
    return next((p for p in POLICIES if p == raw), RETIRE_REMOVED)


def worst(states: Sequence[str]) -> CodeState | Literal[""]:
    """The worst of `states` that are served; '' when none is."""
    served = [s for s in get_args(CodeState) if s in states]
    return served[-1] if served else ""


# the states whose memory ranks below its match in search
DEMOTED = frozenset({"changed", "unresolved", "missing"})


# what a read says of a memory in each state, and what the reader should do
_NOTES = {
    "changed": ("its code changed since it was written", "check before relying on it"),
    "unresolved": ("its code was renamed or removed and could not be followed", "check it"),
    "missing": ("the code it is about is gone", "check before relying on it"),
}


def note(state: str, commit: str = "") -> str:
    """The note a read line carries for a memory whose worst link is in `state`, naming
    the commit that put it there; '' for a state that needs none."""
    if state not in _NOTES:
        return ""
    what, ask = _NOTES[state]
    at = f", commit {commit[:12]}" if commit else ""
    return f" ({what}{at}: {ask})"


def worst_commit(links: Sequence[Mapping[str, Any]]) -> str:
    """The commit that put a memory in its worst state, when that state is one a read
    notes: of its links in that state, the first by path and name."""
    state = worst([str(a.get("state") or "") for a in links])
    if state not in DEMOTED:
        return ""
    at = [a for a in links if a.get("state") == state]
    first = min(at, key=lambda a: (str(a.get("path") or ""), str(a.get("qualified_name") or "")))
    return str(first.get("changed_since") or "")


def rank_down(hits: list[Any], places: int) -> list[Any]:
    """`hits` with each one marked `_code_ranked_down` moved `places` down, never past the
    last. It takes the rank score of the hit it lands under, so a stable sort by score
    keeps the order, and `score_components.code_changed_places` says how far it moved
    (negative, in places, as a reranker's `rerank_delta`)."""
    out = list(hits)
    if places <= 0:
        return out
    for hit in [h for h in hits if getattr(h, "_code_ranked_down", False)]:
        at = next(i for i, h in enumerate(out) if h is hit)  # by identity: hits compare by value
        to = min(at + places, len(out) - 1)
        if to == at:
            continue
        out.insert(to, out.pop(at))
        score = min(float(hit.rank_score), float(out[to - 1].rank_score))
        hit.rank_score = score
        hit.score_components = {
            **hit.score_components,
            "code_changed_places": float(at - to),
            "rank_score": score,
        }
    return out


def code_removed(states: Sequence[str]) -> bool:
    """Every code link of a memory is missing: the code it is about is gone."""
    return bool(states) and all(s == "missing" for s in states)


def held(row: Mapping[str, Any]) -> bool:
    """A person's hold on the memory: pinned, under a lock in force, or verified by a human."""
    verified = verification_state(
        row.get("verified_by"), row.get("verified_sha256"), row.get("sha")
    )
    lock = SimpleNamespace(lock_mode=row.get("lock_mode"), lock_until=row.get("lock_until"))
    return bool(row.get("pinned") or effective_mode(lock) or verified == "human")


def hidden(states: Sequence[str], complete: bool, policy: str, is_held: bool) -> bool:
    """Search leaves the memory out: all its code is gone, no link was cut by the caps,
    and neither a `flag` policy nor a person's hold keeps it."""
    return policy != FLAG and not is_held and complete and code_removed(states)
