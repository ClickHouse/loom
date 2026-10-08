"""Curated context-block assembler.

A token-budgeted curated block that removes distractor cost WITHOUT starving
breadth. Pure CPU (no LLM, no embedding, no CH), linear in the pool.

  1. DENSE planes always survive. Observations, derived/computed pinned rows,
     session summaries, and extracted facts are the distilled signal and are
     cheap; they are admitted FIRST and unconditionally (no budget pressure). A
     dense session summary is what lets the block shrink without starving — it
     carries several sessions' answer content in a fraction of the tokens of
     the raw chunks it replaces.
  2. VERBATIM CHUNKS face the budget. Raw passages are the bulk of the token
     cost and the distractor dilution; they are packed in relevance order to a
     token budget.
  3. MMR / near-duplicate dedup. A chunk whose token-set is ~contained in an
     already-admitted item (neighbor-expansion produces overlapping passages)
     is dropped — UNLESS it carries a unique signal (see #4).
  4. SOLE-CARRIER GUARD (the recall-safe floor). A chunk is NEVER dropped — by
     dedup OR by budget — if it carries a NUMBER, DATE, $ amount, or MULTI-WORD
     PROPER NAME not already present in an admitted item. The answer may hinge
     on that exact token and the read path cannot see the gold answer, so a sole
     carrier survives past budget. This is why the block does not chase an
     arbitrary low budget: accuracy > token target.

Hard constraints:
  1. NO score-knee cutoff — the tier-1 tail is bounded by top_k; the budget only
     trims redundant/signal-free chunks past it, never a relevance knee.
  2. NO chronological reorder of the reader context — relevance order; the
     event date is a per-line LABEL only.
  3. NEVER de-atomize stored facts — fact-vs-fact dedup on the numeric-stripped
     signal is forbidden; dense planes are admitted whole, and a chunk is only
     dropped when it is BOTH a near-duplicate AND signal-free.
"""

from __future__ import annotations

import re
from typing import Any

# Token budget for the verbatim-chunk tail. A module constant, NOT a wire knob —
# context.pack threads its per-section budget down via max_block_tokens. The
# value mirrors the read-side reference footprint we curate toward. It is a
# TARGET, not a hard cap: the sole-carrier guard (recall-safe floor) keeps a
# signal-bearing chunk past this budget rather than drop the answer.
DEFAULT_BLOCK_TOKEN_BUDGET = 4408

# Near-duplicate threshold. A chunk whose token-set is >= (1 - this) contained
# in an already-admitted item is a redundant passage (neighbor-expansion
# overlap) — drop it UNLESS it is a sole carrier of a unique signal. 0.35 means
# "65%+ of this chunk's tokens are already present" => near-duplicate.
_MMR_CONTAINED_THRESHOLD = 0.35

# Dense planes — distilled, cheap, answer-bearing. Admitted FIRST and
# unconditionally (no budget pressure). "fact" rows from memory_embeddings join
# this set; chunks are the only plane the budget trims. "timeline" rows (the
# dedicated dated-timeline section) are dense too: distilled dated
# events presented as their own labeled chronological block, never deduped /
# budget-trimmed, never de-atomized (two short dated events that share prose are
# BOTH kept).
_DENSE_SOURCES = frozenset(
    {
        "fact",
        "summary",
        "entity_summary",
        "observation",
        "derived",
        "computed",
        "timeline",
    }
)

# Exact computed/resolved answers — pinned above the pool, never deduped, never
# dropped. derived/computed are injected elsewhere as pool_max+1 evidence rows;
# observation is the current-state knowledge-update lever, additive here.
_TIER0_SOURCES = frozenset({"derived", "computed", "observation"})

# Sole-carrier signal extraction. NARROW by design: a too-broad signal (every
# capitalized word) would protect every chunk and the budget would never bite;
# a too-narrow one would drop a chunk carrying the answer. The tuned set is
# NUMBERS / DATES / $ amounts and MULTI-WORD proper names — the tokens an
# answer actually hinges on (counts, prices, intervals, named
# entities). Single capitalized words are deliberately excluded (too common to
# be load-bearing, and the dense fact plane already carries named single
# entities).
# A digit run that is NOT glued to letters — so "$1,247.50", "2024-04-15", "43"
# register, but a digit embedded in an alphanumeric token ("w0x", a SKU like
# "AB12C") does NOT (those carry no numeric answer signal). The negative
# look-around enforces the standalone-number boundary.
_NUM_DATE_RE = re.compile(r"(?<![A-Za-z0-9])\$?\d[\d,./:%-]*(?![A-Za-z0-9])")
_MULTI_PROPER_RE = re.compile(r'"[^"]+"|\b[A-Z][a-zA-Z0-9.&-]+(?:\s+[A-Z][a-zA-Z0-9.&-]+)+\b')
_NORM_RE = re.compile(r"[^a-z0-9]+")
# Generic "carriers" that are not load-bearing (the user/assistant framing).
_SIGNAL_STOP = frozenset({"the user", "user", "i"})


def _tokens_estimate(text: str) -> int:
    """~4 chars/token. Matches context_layout._tokens_estimate / passage_trim."""
    return max(1, len(text) // 4) if text else 0


def _norm(text: str) -> str:
    return _NORM_RE.sub(" ", (text or "").lower()).strip()


def _token_set(text: str) -> frozenset[str]:
    return frozenset(_norm(text).split())


def _signal_tokens(excerpt: str) -> frozenset[str]:
    """Numbers / dates / $ amounts / multi-word proper names in an excerpt.

    The sole-carrier payload an answer may hinge on. Returned normalized so the
    membership test against already-admitted signals is punctuation-stable.
    """
    sig: set[str] = set()
    for m in _NUM_DATE_RE.findall(excerpt or ""):
        n = _norm(m)
        if n and any(c.isdigit() for c in n):
            sig.add(n)
    for m in _MULTI_PROPER_RE.findall(excerpt or ""):
        n = _norm(m)
        if n and len(n) > 4 and n not in _SIGNAL_STOP:
            sig.add(n)
    sig.discard("")
    return frozenset(sig)


def _source_of(hit: Any) -> str:
    return getattr(hit, "source", "fact") or "fact"


def _rank_of(hit: Any) -> float:
    return float(getattr(hit, "rank_score", 0.0) or 0.0)


def _raw_rank_of(hit: Any) -> float:
    """The fact's score before the fusion boosts (entity-ANN, date, key match)
    were added; the fused ``rank_score`` when the arm never recorded one."""
    comps = getattr(hit, "score_components", None) or {}
    raw = comps.get("rank_score_raw") if isinstance(comps, dict) else None
    if raw is None:
        return _rank_of(hit)
    try:
        return float(raw)
    except (TypeError, ValueError):
        return _rank_of(hit)


def _calibrate_digests(summaries: list[Any], facts: list[Any]) -> None:
    """Calibrate digest cosines into the fact band, then apply the digest cap
    here as well: a digest whose text cites a fact in this pool is capped just
    under that fact. The fusion pass capped it once, but the band calibration
    below rewrites every digest's rank_score afterwards (a capped summary can
    come back above its fact), so the cap has to follow the calibration."""
    _normalize_summary_scores(summaries, facts)
    from server.memory.domain.fact_citations import defer_citing_rows

    defer_citing_rows([*facts, *summaries])


def _normalize_summary_scores(summaries: list[Any], pool: list[Any]) -> None:
    """Map each summary's raw cosine into the pool's rank_score band, in place.

    A summary leg returns ``rank_score = cosine`` (~[0, 1]); the fused fact pool
    emits a mode-dependent scale (rrf ~[0, 0.033]; additive ~[0, 2]). Appending a
    raw-cosine summary would either dominate an rrf pool or sink in an additive
    one — neither is "compete fairly by relevance". So linearly map a summary's
    cosine onto [pool_min, pool_max] preserving its RELATIVE strength: a summary
    that is near-top by cosine lands near the top of the pool band, a weak one
    near the floor. This keeps summaries as additive candidates that earn a
    high slot on relevance without swamping or being swamped by the facts.

    The band is the facts' RAW score band, before fusion boosts. A boost is
    additive on one row (an entity-ANN hit, a key match); reading the band off
    the fused score would let one boosted fact at 1.25 stretch the band over
    twenty un-boosted facts at 0.81, and a summary with cosine 0.53 would map
    to 0.94 and jump every one of them (with the key-match arm on, eight
    summaries could displace eight facts in one top ten).

    No-op when there is no fact pool to calibrate against (summaries then keep
    their cosine, which is a sensible standalone order).
    """
    if not summaries or not pool:
        return
    pool_scores = [_raw_rank_of(h) for h in pool]
    lo, hi = min(pool_scores), max(pool_scores)
    if hi <= lo:
        # Degenerate pool band — leave summaries just below the pool so they are
        # candidates but never outrank a real fact on a flat pool.
        for s in summaries:
            s.rank_score = lo
        return
    for s in summaries:
        cos = max(0.0, min(1.0, _rank_of(s)))
        s.rank_score = lo + cos * (hi - lo)


def _bound_lifts(rest: list[Any], on: bool) -> list[Any]:
    """`assemble_context_block(bound_component_lifts=True)`: after the
    summaries take their normalised scores and before the block's sort, chunks lifted by
    the parent boost above every un-lifted row are re-placed at the pool's ceiling + k·ε
    (`query_planner.bound_component_lift`) — here and not at fusion because the
    summaries join the pool only here, and the ceiling must be the whole pool's. Runs
    over the same stable sort the block uses, so ties keep the block's order; returns
    `rest` unchanged in membership. Off by default (every other caller unchanged); the
    search handler passes it for every mode but `rrf`, whose fused value is a rank sum
    the boost was never added to."""
    if on:
        from server.memory.domain.query_planner import bound_component_lift

        bound_component_lift(sorted(rest, key=_rank_of, reverse=True))
    return rest


def apply_speaker_quota(ranked: list[Any], quota: int) -> list[Any]:
    """Re-compose a rank-ordered list so every speaker (hit.speaker) gets up
    to `quota` of its best rows before the rest fills by rank.

    Per-speaker retrieval keeps one voice of a two-speaker dialogue from
    filling the window. Speaker order is the order of first appearance in `ranked`, so the
    best-ranked speaker still leads; rows without a speaker are remainder.
    Stable: within a speaker and within the remainder, rank order is kept.
    With fewer than two speakers there is nothing to balance.
    """
    if quota <= 0 or not ranked:
        return list(ranked)
    by_speaker: dict[str, list[Any]] = {}
    order: list[str] = []
    for h in ranked:
        sp = str(getattr(h, "speaker", "") or "")
        if not sp:
            continue
        if sp not in by_speaker:
            by_speaker[sp] = []
            order.append(sp)
        by_speaker[sp].append(h)
    if len(order) < 2:
        return list(ranked)
    head: list[Any] = []
    taken: set[int] = set()
    for sp in order:
        for h in by_speaker[sp][:quota]:
            head.append(h)
            taken.add(id(h))
    tail = [h for h in ranked if id(h) not in taken]
    return head + tail


PERIOD_KEY_PREFIX = "period_digest_"


def is_period_hit(hit: Any) -> bool:
    """A period digest: one derived row over K consecutive
    sessions, keyed `period_digest_<first>_<last>`."""
    return str(getattr(hit, "memory_key", "") or "").startswith(PERIOD_KEY_PREFIX)


def is_chunk_hit(hit: Any) -> bool:
    """A verbatim chunk row (source ``chunk``, type ``memory_chunk`` or a
    ``chunk:`` key): the transcript window, not an extracted fact."""
    if _source_of(hit) == "chunk":
        return True
    if str(getattr(hit, "memory_type", "") or "") == "memory_chunk":
        return True
    return str(getattr(hit, "memory_key", "") or "").startswith("chunk:")


def cap_chunk_share(
    final: list[Any],
    pool: list[Any],
    *,
    top_k: int,
    max_share: float,
) -> tuple[list[Any], int]:
    """Fact rows first for a single-fact question. When more
    than ``max_share`` of the block is verbatim chunks, the lowest-ranked
    surplus chunks give their slots to the best-ranked non-chunk rows the
    pool holds outside the block. On single-hop conversation-QA windows (k=10), chunks
    outnumbered facts, and about a third of the misses with the gold in a
    stored fact row would have shown it in a facts-first top ten. Tier-0 rows and
    period digests are never displaced or admitted here. Returns
    (block, rows_replaced); the block keeps its rank order."""
    k = max(1, int(top_k))
    allowed = int(k * max(0.0, min(1.0, float(max_share))))
    chunks = [h for h in final if is_chunk_hit(h)]
    if len(chunks) <= allowed:
        return final, 0
    in_block = {id(h) for h in final}
    candidates = [
        h for h in pool if id(h) not in in_block and not is_chunk_hit(h) and not is_period_hit(h)
    ]
    if not candidates:
        return final, 0
    candidates.sort(key=lambda h: (-_rank_of(h), float(getattr(h, "distance", 1.0) or 1.0)))
    surplus = sorted(chunks, key=_rank_of)[: len(chunks) - allowed]
    replace = min(len(surplus), len(candidates))
    if replace <= 0:
        return final, 0
    drop = {id(h) for h in surplus[:replace]}
    out = [h for h in final if id(h) not in drop] + candidates[:replace]
    out.sort(key=lambda h: -_rank_of(h))
    return out, replace


def session_of_hit(hit: Any) -> str:
    """The source session a hit came from: the row's
    `session_id` when the leg projected it (facts), else the
    session encoded in a chunk or summary key — `chunk:<session_id>:<idx>`,
    `summary:<session_id>` — else ''."""
    sid = str(getattr(hit, "session_id", "") or "")
    if sid:
        return sid
    key = str(getattr(hit, "memory_key", "") or "")
    if key.startswith("chunk:"):
        rest = key[len("chunk:") :]
        return rest.rsplit(":", 1)[0] if ":" in rest else ""
    if key.startswith("summary:"):
        return key[len("summary:") :]
    return ""


def assemble_context_block(
    pool: list[Any],
    *,
    top_k: int,
    max_block_tokens: int = DEFAULT_BLOCK_TOKEN_BUDGET,
    per_key_cap: int = 0,
    per_speaker_quota: int = 0,
    per_session_cap: int = 0,
    summary_cap: int = 0,
    bound_component_lifts: bool = False,
) -> list[Any]:
    """Curate the pool into a GENTLE, relevance-ordered, token-budgeted block.

    `pool` is the fully-fused, rank-ordered hit list (post global fusion,
    chronological override, rerank, chunk dedup) WITH the summary + observation
    hits already appended by the caller. `max_block_tokens` governs the
    VERBATIM-CHUNK tail only; dense planes are admitted unconditionally. The
    assembler is embedding-free.

    Behaviour:
      1. Tier-0 rows (derived/computed/observation) are PINNED first, in their
         incoming order, never deduped, never dropped.
      2. Session summaries are normalised into the pool's score band so they
         compete by relevance, then folded into the ranked rest.
      3. The tier-1 rest sorts by rank_score desc (STABLE — facts keep baseline
         order) and is bounded by ``top_k`` (the breadth bound; constraint #1).
      4. DENSE planes (fact/summary/observation) within that slice are admitted
         unconditionally — distilled, cheap, answer-bearing.
      5. VERBATIM CHUNKS are packed in relevance order to the effective budget.
         A chunk is dropped only when it is a near-duplicate of an admitted item
         (token-set ~contained) with no unique signal, OR it is past budget and
         carries no unique number/date/proper-name (the sole-carrier guard /
         recall-safe floor — a sole carrier survives past budget).
      6. Exact-duplicate excerpts always collapse (keep the higher-ranked).
      7. With ``per_key_cap`` > 0, at most that many tier-1 rows share one
         (namespace, memory_key); the rest of the key's rows yield their slot to the
         next key in rank order. Tier-0 rows and the timeline section are not counted.
      8. With ``per_session_cap`` > 0, at most that many tier-1 rows share one
         (namespace, session_id); the rest of the session's rows yield their
         slot to the next session in rank order. The session
         is the row's session_id, else the one a chunk or summary key encodes
         (`session_of_hit`); rows with neither are never counted or capped.
         The cap applies after the per-key cap and before the top_k slice, so
         the block is filled from the ranked pool, never left short.
      9. With ``summary_cap`` > 0, at most that many digest rows (source
         summary / entity_summary) stay in the tier-1 slice, in rank order; the rest
         step aside for the next fact. A digest that dates an event at its session date
         otherwise outranks the anchored fact that dates it right (conversation-QA windows carry
         3–4 digests of 15). Tier-0 rows are not counted.
    """
    if not pool:
        return []

    tier0 = [h for h in pool if _source_of(h) in _TIER0_SOURCES]
    rest = [h for h in pool if _source_of(h) not in _TIER0_SOURCES]

    # Session summaries (source="summary") and pre-built entity summaries
    # (source="entity_summary") are additive dense candidates: calibrate their
    # cosine into the fact pool's band so they slot in by relevance. The sort
    # below is stable, so facts keep their baseline order among themselves and
    # summaries interleave purely by their normalised relevance — they can never
    # displace a fact.
    _SUMMARY_SOURCES = ("summary", "entity_summary")
    _DIGEST_SOURCES = ("summary", "entity_summary")
    summaries = [h for h in rest if _source_of(h) in _SUMMARY_SOURCES]
    facts = [h for h in rest if _source_of(h) not in _SUMMARY_SOURCES]
    if summaries:
        _calibrate_digests(summaries, facts)
    ranked = sorted(_bound_lifts(rest, bound_component_lifts), key=_rank_of, reverse=True)

    # Per-key cap: the (k+1)-th row of a key steps aside for the next key.
    if int(per_key_cap) > 0:
        per_key: dict[tuple[str, str], int] = {}
        capped: list[Any] = []
        for h in ranked:
            ident = (
                str(getattr(h, "namespace", "") or ""),
                str(getattr(h, "memory_key", "") or ""),
            )
            n = per_key.get(ident, 0)
            if n >= int(per_key_cap):
                continue
            per_key[ident] = n + 1
            capped.append(h)
        ranked = capped

    # Per-speaker quota: each speaker's best rows first, up to the quota, in
    # order of the speakers' first appearance; then the remainder by rank.
    # Unattributed rows are remainder. A pure re-composition of the pool.
    if int(per_speaker_quota) > 0:
        ranked = apply_speaker_quota(ranked, int(per_speaker_quota))
    # Digest cap: the (n+1)-th summary row steps aside for the next fact.
    if int(summary_cap) > 0:
        n_digests = 0
        kept_rows: list[Any] = []
        for h in ranked:
            if _source_of(h) in _DIGEST_SOURCES:
                if n_digests >= int(summary_cap):
                    continue
                n_digests += 1
            kept_rows.append(h)
        ranked = kept_rows

    # Per-session cap: evidence completion, not early hit. The
    # (k+1)-th row of a session steps aside for the next session in rank order.
    if int(per_session_cap) > 0:
        per_session: dict[tuple[str, str], int] = {}
        spread: list[Any] = []
        for h in ranked:
            sid = session_of_hit(h)
            if not sid:
                spread.append(h)
                continue
            ident_s = (str(getattr(h, "namespace", "") or ""), sid)
            n = per_session.get(ident_s, 0)
            if n >= int(per_session_cap):
                continue
            per_session[ident_s] = n + 1
            spread.append(h)
        ranked = spread
    # Breadth bound (constraint #1): the tier-1 tail is the baseline top_k slice.
    # The curated budget only trims redundant / signal-free chunks WITHIN it; it
    # never imposes a relevance knee.
    if int(top_k) > 0:
        ranked = ranked[: int(top_k)]

    budget = int(max_block_tokens) if int(max_block_tokens) > 0 else DEFAULT_BLOCK_TOKEN_BUDGET

    kept: list[Any] = []
    used = 0
    seen_exact: set[str] = set()
    admitted_tokensets: list[frozenset[str]] = []
    admitted_signals: set[str] = set()

    for h in ranked:
        excerpt = getattr(h, "content_excerpt", "") or ""
        exact = _norm(excerpt)
        # Exact-duplicate excerpt always collapses (constraint #3 carve-out).
        if exact and exact in seen_exact:
            continue

        tokset = _token_set(excerpt)
        sig = _signal_tokens(excerpt)
        unique_sig = sig - admitted_signals
        cost = _tokens_estimate(excerpt)
        is_dense = _source_of(h) in _DENSE_SOURCES

        # Dense planes (fact/summary/observation) are admitted unconditionally —
        # distilled, cheap, answer-bearing, and the mechanism that lets the block
        # shrink without starving (a summary replaces several raw chunks). They
        # face NEITHER the near-duplicate dedup NOR the budget: only the exact-
        # duplicate collapse above can drop a dense row. This is constraint #3 —
        # facts are never de-atomized; two distinct short facts that happen to
        # share most tokens ("fact about ev_a" / "fact about ev_b") are BOTH
        # kept.
        if not is_dense:
            # Near-duplicate (token-set ~contained in an admitted item) with NO
            # unique signal is a pure distractor chunk — drop it. A sole carrier
            # of a unique number/date/proper-name is kept even when redundant in
            # prose (the recall-safe override).
            if not unique_sig and tokset:
                is_near_dup = any(
                    len(tokset - at) / max(1, len(tokset)) <= _MMR_CONTAINED_THRESHOLD
                    for at in admitted_tokensets
                )
                if is_near_dup:
                    continue
            # Verbatim chunk past budget with no sole-carrier signal: drop.
            # Recall-safe floor: a chunk carrying a unique signal survives.
            if used + cost > budget and not unique_sig:
                continue

        kept.append(h)
        used += cost
        admitted_tokensets.append(tokset)
        admitted_signals |= sig
        if exact:
            seen_exact.add(exact)

    # Tier-0 rows are exact answers — keep them all on top of the curated tail.
    return list(tier0) + kept
