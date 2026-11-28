"""What a namespace's code-linked memories go through when their code changes.

A memory about code describes something Loom indexes, so once that code is gone,
serving it as current is wrong. No policy writes the memory; the namespace owner picks
what reads do:

  retire_removed (default)  search leaves out a memory whose linked code is all gone;
                            `memory.get` still shows it, and it is back the moment
                            the code is; changes are flagged
  flag                      links are marked, every read still serves the memory
  revise                    as retire_removed, and a changed claim is re-checked
                            against the new code by the LLM: one that still holds
                            is re-anchored, a contradicted one gets a suggested
                            correction on its link

Under every policy, a memory that a search still serves ranks lower than its match
(`rank_down`) on its first page when its worst link is changed, unresolved or missing.
A memory a person pinned, locked or verified is only flagged.

A link that sits at another name or path than the memory was written against is named
on the reads that serve its links (`relocation`, `note`) while the text still says the
old name. Under every policy the link pass brings that text up to a rename or move a
link followed with its code unchanged, where the swap is certain and the memory is held
in the repository's code namespace (`server.memory.app.code_renames.revise`); every
other text stays as written.
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from types import SimpleNamespace
from typing import Any, Literal, get_args

from server.memory.domain.lock import effective_mode
from server.memory.domain.search_types import TIER_COMPONENTS
from server.memory.domain.trust import verification_state

# a served link's states from best to worst: a memory shows its worst link's
CodeState = Literal["fresh", "moved", "changed", "unresolved", "missing"]
STATE_RANK: dict[str, int] = {s: i for i, s in enumerate(get_args(CodeState))}
# the states of a link that is at a definition or file: its path, name and lines are current
LOCATED: tuple[str, ...] = ("fresh", "moved", "changed")

CodeChangePolicy = Literal["retire_removed", "flag", "revise"]
POLICIES: tuple[CodeChangePolicy, ...] = get_args(CodeChangePolicy)
RETIRE_REMOVED, FLAG, REVISE = POLICIES


def policy_of(raw: str) -> CodeChangePolicy:
    return next((p for p in POLICIES if p == raw), RETIRE_REMOVED)


def worst(states: Sequence[str]) -> CodeState | Literal[""]:
    """The worst of `states` that are served; '' when none is."""
    served = [s for s in get_args(CodeState) if s in states]
    return served[-1] if served else ""


# what a read says of a memory in each state, and what the reader should do
_NOTES = {
    "changed": ("its code changed since it was written", "check before relying on it"),
    "unresolved": ("its code was renamed or removed and could not be followed", "check it"),
    "missing": ("the code it is about is gone", "check before relying on it"),
}
# the states whose memory ranks below its match in search: those a read notes
DEMOTED = frozenset(_NOTES)


def written_as(link: Mapping[str, Any]) -> tuple[str, str]:
    """(path, qualified name) the memory's text was written against: the oldest identity
    the link keeps, else its anchor (a claim the `revise` policy found to hold is
    re-anchored where the code is, and its text still names the old place). A definition
    link that replaced a file link was never that file: only an identity of the link's
    own kind counts."""
    of_symbol = bool(link.get("qualified_name"))
    former = [f for f in link.get("former") or [] if bool(f["name"]) == of_symbol]
    if former:
        return str(former[0]["path"]), str(former[0]["name"])
    return str(link.get("anchored_path") or ""), str(link.get("anchored_name") or "")


def relocation(link: Mapping[str, Any]) -> tuple[str, str] | None:
    """`written_as` of a link (a link row) that is at a definition or file under another
    path or name; None for one where it was written, renamed back, or not found."""
    was = written_as(link)
    here = (str(link.get("path") or ""), str(link.get("qualified_name") or ""))
    if link.get("state") not in LOCATED or not was[0] or was == here:
        return None
    return was


# relocated links a note names before it counts the rest, and the characters of each path
# or name it prints: a repository's own, so one line and bounded
_NAMED = 2
_NAME_CHARS = 120


def _field(link: Any, name: str) -> str:
    value = link.get(name) if isinstance(link, Mapping) else getattr(link, name, None)
    text = " ".join(str(value or "").replace("`", "'").split())
    return text if len(text) <= _NAME_CHARS else text[: _NAME_CHARS - 1] + "…"


def _now(link: Any) -> str:
    """`old` is now `new` at path:start-end, for one relocated link in its wire shape; a
    definition that kept its name is named once, a file link by its paths."""
    path, symbol, was = _field(link, "path"), _field(link, "symbol"), _field(link, "was_symbol")
    if not symbol:
        return f"`{_field(link, 'was_path')}` is now `{path}`"
    start, end = int(_field(link, "start_line") or 0), int(_field(link, "end_line") or 0)
    lines = f":{start}-{end}" if 0 < start <= end else ""
    name = f"`{symbol}` " if symbol != was else ""
    return f"`{was}` is now {name}at {path}{lines}"


def moved_note(moved: Sequence[Any]) -> str:
    """Where the code of a memory's relocated links (`moved`, each a code link in its wire
    shape with `was_path` set) is now: the first `_NAMED`, then how many more; '' for none."""
    named = "; ".join(_now(link) for link in moved[:_NAMED])
    more = f"; +{len(moved) - _NAMED} more" if len(moved) > _NAMED else ""
    return named + more


def note(state: str, commit: str = "", moved: Sequence[Any] = ()) -> str:
    """The note a read line carries for a memory whose worst link is in `state`, naming
    the commit that put it there and where its relocated links (`moved`) are now; '' for
    a state that needs none with nothing relocated."""
    now = moved_note(moved)
    if state not in _NOTES:
        return f" (code moved: {now})" if now else ""
    what, ask = _NOTES[state]
    at = f", commit {commit[:12]}" if commit else ""
    return f" ({what}{at}: {ask}{'; ' + now if now else ''})"


def label(state: str, commit: str = "") -> str:
    """`<state>@<commit>` for a line's labels (the state alone without a commit); '' for a
    state that needs none."""
    if state not in _NOTES:
        return ""
    return f"{state}@{commit[:12]}" if commit else state


def worst_commit(links: Sequence[Mapping[str, Any]]) -> str:
    """The commit that put a memory in its worst state, when that state is one a read
    notes: of its links in that state, the first by path and name."""
    state = worst([str(a.get("state") or "") for a in links])
    if state not in DEMOTED:
        return ""
    at = [a for a in links if a.get("state") == state]
    first = min(at, key=lambda a: (str(a.get("path") or ""), str(a.get("qualified_name") or "")))
    return str(first.get("changed_since") or "")


# rows placed by something other than their score, which a move leaves where they are
_PLACED = ("identifier_match", *TIER_COMPONENTS)


def _placed(hit: Any) -> bool:
    return any(k in hit.score_components for k in _PLACED)


def spend_marks(hits: list[Any]) -> list[Any]:
    """The hits marked `_code_ranked_down`, their marks cleared: no later merge moves them."""
    marked = [h for h in hits if getattr(h, "_code_ranked_down", False)]
    for hit in marked:
        hit._code_ranked_down = False
    return marked


def moved_for_stale_code(hit: Any) -> bool:
    """A row a search moved for stale code (`rank_down`), one recorded at 0 places
    included: no current row for a later move to pass, nor one a later step may lift."""
    return "code_changed_places" in hit.score_components


def _below_the_floor(hit: Any) -> bool:
    """A row the search serves only as filler: under the similarity floor, or flagged
    below the abstain threshold."""
    return bool(hit.score_components.get("below_floor") or getattr(hit, "below_threshold", False))


def rank_down(hits: list[Any], places: int, *, rescore: bool = True) -> list[Any]:
    """`hits` with each one marked `_code_ranked_down` moved below the next `places` rows
    that are neither marked, already moved, nor placed by an identifier match or a tier,
    never past the last nor below a row served only as filler (`_below_the_floor`). A
    placed row is never counted as a place and never moved down (one the move passes
    rises a slot).

    A moved hit scores between the rows around it (or ties the one above, when they
    tie), so a stable sort by score keeps the order; with `rescore` False it keeps its
    score, for a list placed by something other than score.
    `score_components.code_changed_places` counts the rows it moved below (negative, as
    a reranker's `rerank_delta`; 0 for one with nothing below it to pass, which a later
    step still must not lift). The marks are spent, so a search that merges more rows in
    moves only those."""
    marked = spend_marks(hits)
    moving = {id(h) for h in marked if not _placed(h)} if places > 0 else set()
    out = list(hits)
    # bottom-up: a stale row moves past the same rows whether or not one above it moves
    for hit in [h for h in reversed(hits) if id(h) in moving]:
        at = next(i for i, h in enumerate(out) if h is hit)  # by identity: hits compare by value
        passed, to = 0, at
        for i in range(at + 1, len(out)):
            if passed == places or _below_the_floor(out[i]):
                break
            if (
                id(out[i]) not in moving
                and not _placed(out[i])
                and not moved_for_stale_code(out[i])
            ):
                passed, to = passed + 1, i
        if not passed:
            hit.score_components = {**hit.score_components, "code_changed_places": 0.0}
            continue
        out.insert(to, out.pop(at))
        record = {"code_changed_places": float(-passed)}
        if rescore:
            above = float(out[to - 1].rank_score)
            below = float(out[to + 1].rank_score) if to + 1 < len(out) else None
            if below is None or below > above:
                hit.rank_score = math.nextafter(above, -math.inf)
            else:
                hit.rank_score = (above + below) / 2  # `above` itself when they tie
            record["rank_score"] = hit.rank_score
        hit.score_components = {**hit.score_components, **record}
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
