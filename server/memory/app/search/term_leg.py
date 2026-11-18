"""The term leg: a query that names a governed definition's term or alias is served that
definition first (docs/concepts/identities-and-namespaces.md#governed-definitions-in-search).

The leg reads the addressed namespace's shared definitions concurrently with the other
legs; the search waits a bounded time more for it after them, and its hits join the
served list after the verdict, so no floor or calibration of the search reads them. When
no name matches, the served list is the one the search built without it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field, replace
from typing import Any, NamedTuple

from server.core.background import create_deferred_task
from server.core.error_summary import error_summary
from server.core.hashing import query_text, valid_unicode
from server.memory.api.models_search import MemorySearchInput, TermLegStatus, _MemorySearchHit
from server.memory.app.definitions import source_reasons
from server.memory.app.search.context import SearchContext
from server.memory.app.search.evidence import EVIDENCE_LIFT_EPS
from server.memory.app.sensitivity_level import ON_LOOP_CHARS
from server.memory.app.spans import cosine
from server.memory.domain.definition_check import SharedDefinition, copy_lapse, lost_names
from server.memory.domain.definitions import (
    Name,
    fold_name,
    joined_chars,
    name_start,
    written_names,
)
from server.memory.domain.excerpt import build_excerpt
from server.memory.domain.search_types import SHARED_AGENT, hit_row, is_computed_row
from server.memory.store import shared as shared_repo
from server.memory.store.embeddings import live_vectors
from server.settings import settings

log = logging.getLogger(__name__)


@dataclass
class TermLeg:
    status: TermLegStatus
    hits: list[_MemorySearchHit] = field(default_factory=list)


def leg_on(input: MemorySearchInput) -> bool:
    """Whether the leg runs: not for a page handle or a pass another search merges, nor
    for a field that asks for other content than a definition (a type, a hard tag or
    category filter, a replay, a body floor, rows placed first)."""
    if input._merged_into_parent:
        return False
    if input.page or input.memory_type is not None or input.as_of is not None:
        return False
    if (input.tags and not input.tags_soft) or (input.category and not input.category_soft):
        return False
    if input.min_body_chars or input.recent_first or input.anchor_first:
        return False
    if input.legs is not None and "memory" not in input.legs:
        return False
    # An excessive query is withheld by the work budget before normalization.
    return len(input.query) > ON_LOOP_CHARS or valid_unicode(input.query)


def launch(
    input: MemorySearchInput, query_vec: list[float] | None, model: str
) -> asyncio.Task[TermLeg] | None:
    """The leg as a task that scores its hits against `query_vec`, the search's embedding
    of `model`, or None when it is off."""
    if not leg_on(input):
        return None
    read = _read(
        input.org,
        input.namespace,
        input.query,
        set(input.exclude_keys),
        query_vec,
        model,
        whole=input.bodies in ("unit", "source"),
    )
    task = create_deferred_task(read)
    # A pass that returns before it joins the leg (a re-read) leaves no unread failure.
    task.add_done_callback(_retain_task_result)
    return task


def _retain_task_result(task: asyncio.Task[TermLeg]) -> None:
    if not task.cancelled():
        task.exception()


async def refresh_parked(
    org: str, namespace: str, query: str, hits: list[_MemorySearchHit], *, whole: bool
) -> list[_MemorySearchHit | None]:
    """A parked governed hit is served only while the same text is governed for the
    original query. Reuse the leg's source, name and promotion checks, with its wait bound."""
    if not any(h.governed for h in hits):
        return list(hits)
    task = create_deferred_task(_read(org, namespace, query, set(), None, "", whole=True))
    current = await outcome(task, settings.memory_term_leg_wait_ms / 1000)
    values = {h.memory_key: h.content_excerpt for h in current.hits}
    refreshed: list[_MemorySearchHit | None] = []
    for h in hits:
        if not h.governed:
            refreshed.append(h)
            continue
        if (
            h.namespace not in ("", namespace)
            or h.memory_key not in values
            or h.content_excerpt
            not in (
                values[h.memory_key],
                build_excerpt(values[h.memory_key], memory_key=h.memory_key),
            )
        ):
            refreshed.append(None)
            continue
        text = values[h.memory_key]
        h.content_excerpt = text if whole else build_excerpt(text, memory_key=h.memory_key)
        refreshed.append(h)
    return refreshed


async def outcome(task: asyncio.Task[TermLeg] | None, wait_s: float) -> TermLeg:
    """What the leg found, waiting at most `wait_s` more for it; a leg past that or whose
    reads failed adds nothing."""
    if task is None:
        return TermLeg("off")
    try:
        return await asyncio.wait_for(task, wait_s)
    except TimeoutError:
        return TermLeg("timed_out")
    except Exception as e:  # noqa: BLE001 — a failed read withholds the definitions only
        log.warning("term leg failed, its definitions withheld: %s", error_summary(e))
        return TermLeg("failed")


class _Match(NamedTuple):
    start: int  # where the name starts in the query, in joined characters
    length: int


@dataclass
class _Candidate:
    """A listed definition, its names as written and folded."""

    definition: SharedDefinition
    names: list[str]
    folds: set[str]


@dataclass
class _Work:
    """Bound synchronous normalization and matching before calling domain helpers."""

    left: int = 16 * ON_LOOP_CHARS

    def charge(self, units: int) -> None:
        if units > self.left:
            raise TimeoutError
        self.left -= units

    def names(self, term: str, aliases: list[str]) -> None:
        # Count empty names too; check the count before walking an arbitrary list.
        chars = 32 * (1 + len(aliases))
        self.charge(chars)
        if chars > ON_LOOP_CHARS:
            raise TimeoutError
        for name in (term, *aliases):
            # Candidate preparation normalizes the entire definition without yielding.
            chars += len(name)
            if chars > ON_LOOP_CHARS:
                raise TimeoutError
            self.charge(4 * len(name))

    def first_name(self, query: list[str], folded: str, names: list[str]) -> _Match | None:
        query_size = sum(len(c) for c in query)
        prepared = []
        for name in names:
            folded_name = query_text(name).casefold()
            if folded_name and folded_name in folded:
                # Normalization can expand a name: charge before building its pattern.
                self.charge(query_size * len(folded_name))
                prepared.append(Name.of(name))
        return _first_name(query, folded, prepared)


async def _read(
    org: str,
    namespace: str,
    query: str,
    excluded: set[str],
    query_vec: list[float] | None,
    model: str,
    *,
    whole: bool = False,
) -> TermLeg:
    try:
        return await _read_bounded(org, namespace, query, excluded, query_vec, model, whole=whole)
    except TimeoutError:
        return TermLeg("timed_out")


async def _read_bounded(
    org: str,
    namespace: str,
    query: str,
    excluded: set[str],
    query_vec: list[float] | None,
    model: str,
    *,
    whole: bool,
) -> TermLeg:
    work = _Work()
    work.charge(32 * len(query))
    ceiling = settings.memory_shared_definitions_max
    rows = await shared_repo.shared_definitions(org, namespace, ceiling + 1, strong=False)
    if not rows:
        return TermLeg("empty")
    status: TermLegStatus = "capped" if len(rows) > ceiling else "complete"
    query_chars = joined_chars(query_text(query))
    folded_query = "".join(query_chars).casefold()
    named: list[_Candidate] = []
    candidates: list[_Candidate] = []
    for row in rows[:ceiling]:
        await asyncio.sleep(0)
        work.names(row["term"], row["aliases"])
        c = _candidate(row, org, namespace)
        if c is None:
            continue
        candidates.append(c)
        if c.definition.memory_key in excluded:
            continue
        if work.first_name(query_chars, folded_query, c.names) is not None:
            named.append(c)
    if not named:
        return TermLeg(status)
    # A definition that shares a name with a named one may hold that name, whether the
    # query matched it or not (another case, or excluded): it is judged too.
    named_keys = {c.definition.memory_key for c in named}
    named_folds = set().union(*(c.folds for c in named))
    judged = named + [
        c for c in candidates if c.definition.memory_key not in named_keys and c.folds & named_folds
    ]
    governed = await _governed(org, namespace, [c.definition for c in judged], work)
    lost = lost_names(list(governed.values()))
    matches: list[tuple[_Match, SharedDefinition]] = []
    for c in named:
        d = governed.get(c.definition.memory_key)
        if d is None:
            continue
        lost_folds = {fold_name(n) for n, _ in lost.get(d.memory_key, [])}
        # The names the served value holds, which a re-promotion since the first read may
        # have changed.
        kept = [n for n in written_names(d.value) if fold_name(n) not in lost_folds]
        if (m := work.first_name(query_chars, folded_query, kept)) is not None:
            matches.append((m, d))
    # Query order; at one start the longer name, then the latest promotion.
    matches.sort(key=lambda md: (md[1].promoted_at, md[1].promotion_event), reverse=True)
    matches.sort(key=lambda md: (md[0].start, -md[0].length))
    hits = [_hit(d, namespace, whole=whole) for _, d in matches]
    await _score(hits, org, namespace, query_vec, model)
    return TermLeg(status, hits)


def _candidate(row: dict[str, Any], org: str, namespace: str) -> _Candidate | None:
    """A listed definition prepared for matching, or None when its copy lapsed (no term,
    or not promoted from its namespace-wide row)."""
    d = SharedDefinition(
        memory_key=row["memory_key"],
        value={"term": row["term"], "aliases": list(row["aliases"])},
        promoted_from=tuple(row["promoted_from"]),
        promoted_at=row["promoted_at"],
        promotion_event=str(row["promotion_event"]),
    )
    if copy_lapse(d, org=org, namespace=namespace) is not None:
        return None
    names = [row["term"], *row["aliases"]]
    return _Candidate(d, names, {fold_name(n) for n in names})


async def _governed(
    org: str, namespace: str, definitions: list[SharedDefinition], work: _Work
) -> dict[str, SharedDefinition]:
    """The governed ones of `definitions`, by key, with their full values. Read
    without sequential consistency: a promotion another replica just made may miss a
    search."""
    values = await shared_repo.definition_values(
        org, namespace, [d.memory_key for d in definitions]
    )
    full = []
    for d in definitions:
        if d.memory_key not in values:
            continue
        await asyncio.sleep(0)
        work.charge(len(values[d.memory_key]))
        value = json.loads(values[d.memory_key])
        if isinstance(value, dict) and isinstance(value.get("term"), str):
            aliases = value.get("aliases") or []
            if isinstance(aliases, list) and all(isinstance(a, str) for a in aliases):
                work.names(value["term"], aliases)
        full.append(replace(d, value=value))
    full = [d for d in full if copy_lapse(d, org=org, namespace=namespace) is None]
    reasons = await source_reasons(org, namespace, full, strong=False, max_value_chars=work.left)
    return {d.memory_key: d for d in full if reasons[d.memory_key] is None}


def _first_name(query_chars: list[str], folded_query: str, names: list[Name]) -> _Match | None:
    """Where the first of `names` the query names starts, and its length, or None."""
    found = [
        _Match(start, len(n.chars))
        for n in names
        if (start := name_start(query_chars, n, folded_query)) is not None
    ]
    return min(found, key=lambda m: (m.start, -m.length)) if found else None


def _hit(d: SharedDefinition, namespace: str, *, whole: bool = False) -> _MemorySearchHit:
    """The governed hit for `d`: its value in full when the search serves whole bodies,
    else the excerpt any row shows."""
    # From the decoded value: the stored one escapes every non-ASCII character.
    text = json.dumps(d.value, ensure_ascii=False)
    hit = _MemorySearchHit(
        memory_key=d.memory_key,
        memory_type="definition",
        content_excerpt=text if whole else build_excerpt(text, memory_key=d.memory_key),
        distance=1.0,
        similarity=0.0,
        created_at=d.promoted_at.isoformat(),
        namespace=namespace,
        governed=True,
        score_components={"governed": 1.0},
    )
    hit._row_identity = (SHARED_AGENT, "-", "-")
    hit._row_scope = "shared"
    return hit


async def _score(
    hits: list[_MemorySearchHit],
    org: str,
    namespace: str,
    query_vec: list[float] | None,
    model: str,
) -> None:
    """Set each governed hit's similarity and distance from its shared embedding; a hit
    with none keeps similarity 0 and distance 1."""
    if not hits or not query_vec:
        return
    idents = [(SHARED_AGENT, "-", "-", h.memory_key) for h in hits]
    try:
        vectors = await live_vectors(org, namespace, idents, model=model, sequential=False)
    except Exception as e:  # noqa: BLE001 — a score is not worth failing the search over
        log.warning("term leg scores not read: %s", error_summary(e))
        return
    for ident, hit in zip(idents, hits, strict=True):
        await asyncio.sleep(0)
        vec = vectors.get(ident)
        if vec and len(vec) == len(query_vec):
            hit.similarity = max(0.0, min(1.0, cosine(query_vec, vec)))
            hit.distance = 1.0 - hit.similarity


async def join(
    ctx: SearchContext,
    task: asyncio.Task[TermLeg] | None,
    hits: list[_MemorySearchHit],
    abstain_verdict: tuple[bool, str, str],
) -> tuple[list[_MemorySearchHit], tuple[bool, str, str]]:
    """The served hits with the leg's placed first and the verdict (no abstain once one is
    served); what the leg did goes on `ctx.term_leg_status`."""
    # The leg ran beside the others; the search waits at most the bound more for it, and a
    # re-entrant pass (a fallback, a re-read) no longer than its parent's time left.
    wait_s = settings.memory_term_leg_wait_ms / 1000
    if ctx.input._pass_depth:
        wait_s = min(wait_s, max(ctx.search_budget.wall_left_s(), 0.0))
    term = await outcome(task, wait_s)
    if term.hits:
        input = ctx.input
        before = len(hits) + len(ctx.page_tail)
        hits, ctx.page_tail = merge(hits, term.hits, ctx.page_tail, input.namespace, input.top_k)
        # A governed key the pool already held was replaced, not added.
        ctx.candidates_total += len(hits) + len(ctx.page_tail) - before
        abstain_verdict = (False, "", "")
    ctx.actual_work["term_leg_served"] = sum(1 for h in hits if h.governed)
    ctx.term_leg_status = term.status
    return hits, abstain_verdict


def merge(
    hits: list[_MemorySearchHit],
    governed: list[_MemorySearchHit],
    tail: list[_MemorySearchHit],
    namespace: str,
    top_k: int,
) -> tuple[list[_MemorySearchHit], list[_MemorySearchHit]]:
    """The served hits and the parked tail with `governed` placed first, after a computed
    row; any other hit under a governed key in `namespace` is left out. Governed hits
    count within `top_k` and are cut last; what is cut leads the tail."""
    keys = {(namespace, h.memory_key) for h in governed}

    def ordinary(rows: list[_MemorySearchHit]) -> list[_MemorySearchHit]:
        return [h for h in rows if hit_row(h, namespace) not in keys]

    computed = [h for h in hits if is_computed_row(h)]
    rest = ordinary([h for h in hits if not is_computed_row(h)])
    # Above the pool, below a computed row (the pool's ceiling plus EVIDENCE_LIFT_EPS).
    top = max((h.rank_score for h in rest), default=0.0)
    for i, h in enumerate(governed):
        h.rank_score = top + EVIDENCE_LIFT_EPS * (len(governed) - i) / (len(governed) + 1)
    lead = computed + governed
    head = lead[:top_k]
    room = top_k - len(head)
    return head + rest[:room], lead[top_k:] + rest[room:] + ordinary(tail)
