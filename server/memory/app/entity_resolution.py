"""Namespace-scoped canonical entity resolution.

This service maps mention-level entity strings to one canonical text inside a
single namespace. It never reads or writes aliases across namespaces: a shared
memory product must treat cross-namespace over-merge as a privacy bug.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from difflib import SequenceMatcher
from typing import Any, cast

from server.core.ch_writes import batched_insert_settings
from server.core.datetimes import naive_utc_now
from server.memory.domain.graph_quality import canonical_entity_text

_MIN_FUZZY_SCORE = 0.95
_MAX_NORMALIZED_ENTROPY = 0.55
_MIN_TOP_GAP = 0.02
_MAX_NAMESPACE_CANDIDATES = 5000


@dataclass(frozen=True)
class EntityResolutionDecision:
    mention_text: str
    canonical_text: str
    confidence: float
    strategy: str


def compact_entity_text(text: str) -> str:
    """Normalize away separators for near-exact product/org spellings."""

    return "".join(ch for ch in canonical_entity_text(text) if ch.isalnum())


def entity_similarity(a: str, b: str) -> float:
    """String similarity over canonical and compact forms."""

    a_canon = canonical_entity_text(a)
    b_canon = canonical_entity_text(b)
    if not a_canon or not b_canon:
        return 0.0
    if a_canon == b_canon:
        return 1.0
    compact_a = compact_entity_text(a_canon)
    compact_b = compact_entity_text(b_canon)
    ratios = [
        SequenceMatcher(None, a_canon, b_canon).ratio(),
        SequenceMatcher(None, compact_a, compact_b).ratio() if compact_a and compact_b else 0.0,
    ]
    return float(max(ratios))


def _short_name_mismatch(a: str, b: str) -> bool:
    """Guard against high-similarity merges of genuinely distinct SHORT
    names, where a one-character edit flips the referent. Pure
    separator/punctuation variants (identical compact forms) are always
    safe; otherwise reject when the whole compacted surface, or any
    aligned differing token, is <= 4 chars (the length where SequenceMatcher
    scores stay above the fuzzy gate for distinct names)."""

    compact_a = compact_entity_text(a)
    compact_b = compact_entity_text(b)
    if compact_a == compact_b:
        return False
    if min(len(compact_a), len(compact_b)) <= 4:
        return True
    tokens_a = canonical_entity_text(a).split()
    tokens_b = canonical_entity_text(b).split()
    if len(tokens_a) == len(tokens_b):
        for ta, tb in zip(tokens_a, tokens_b, strict=True):
            if ta != tb and min(len(ta), len(tb)) <= 4:
                return True
    return False


def normalized_entropy(scores: list[float]) -> float:
    """Return Shannon entropy in [0, 1] for candidate scores."""

    positive = [max(0.0, s) for s in scores if s > 0.0]
    if len(positive) <= 1:
        return 0.0
    total = sum(positive)
    if total <= 0.0:
        return 1.0
    entropy = -sum((s / total) * math.log(s / total) for s in positive)
    return float(entropy / math.log(len(positive)))


# `(canonical, compact, len(canonical), len(compact))` — see _prepare_candidates.
# Character multisets for the overlap bound live on PreparedCandidates, built
# lazily for the few candidates the length index admits.
PreparedCandidate = tuple[str, str, int, int]


# --- candidate prefilter -------------------------------------------------------
# Scoring every mention against every candidate in the namespace costs up to
# 100,000 `entity_similarity` calls per write (~20 mentions x 5,000 capped
# canonical-map rows), each running two `SequenceMatcher` passes: ~5.7 s for a
# cold write on a 5,000-row namespace, against ~85 ms on a small one. Only
# mentions NOT already in the canonical map pay it (a known mention is answered
# from `by_mention`), so the slow path is a write that introduces new entities.
# With the prefilter the same cold write takes ~1.2 s.
# The prefilter is EXACT, not heuristic. `SequenceMatcher.ratio()` is 2*M/T where
# M is matched characters and T the combined length, so it is bounded above by
# 2*min(la, lb) / (la + lb). If that bound is below `min_score` the pair cannot
# clear the gate, so skipping it cannot change a decision. At min_score=0.95 the
# admissible length window is roughly +/-10%.
# Decision-preservation also holds for the two gates that look at OTHER
# candidates, which is the part worth being careful about:
#   * `min_gap`: removing candidates can only LOWER the runner-up score, which
#     widens the gap. And a genuine near-tie has both candidates within ~0.02 of
#     `best`, hence both above min_score, hence both inside the window — so the
#     tie is preserved and still rejected.
#   * `normalized_entropy`: computed only over scores >= min_score, every one of
#     which the filter provably keeps.
# Blocking: two more steps, both exact, on top of the length bound above:
#   1. The length bound is applied as an INDEX, not a scan. Solving
#      2*min(m, c) / (m + c) >= s for c gives the admissible window
#      [ceil(s*m / (2-s)), floor(m*(2-s) / s)] — at 0.95 that is ±~5% of the
#      mention's length. Candidates are bucketed by canonical length and by
#      compact length once (`_prepare_candidates`), and a mention visits only
#      the buckets inside its two windows instead of every candidate. 5,000
#      candidates become a few hundred before any per-pair work.
#   2. A character-overlap bound on what the windows admit. M — the matched
#      characters — is a common sub-multiset of both strings (matching blocks
#      are equal substrings at disjoint positions), so
#          M <= sum_c min(count_a[c], count_b[c])
#      and ratio <= 2 * that / (la + lb). At 0.95 a candidate must share 95% of
#      the mention's characters as a multiset; one that differs by a word
#      cannot. Exact for the same reason as the length bound (it IS the length
#      bound with the intersection in place of min(la, lb), and the
#      intersection never exceeds min(la, lb)).
# On a 20 x 5,000 cold write (every mention new, names 1-3 tokens from a
# 60-word vocabulary), decisions are identical on all 40 probes (20 new, 20
# near-duplicates of which 9 merge either way):
#     length scan                     725 ms   15,043 pairs reach SequenceMatcher
#     + overlap bound, still a scan   216 ms        0 pairs reach it
#     + length index (this file)       73 ms
# Preparing the candidates costs ~36 ms either way: the indexes
# are two dicts of positions and the multisets are built lazily, only for
# candidates a mention's window admits.


class PreparedCandidates(list[PreparedCandidate]):
    """The prepared list plus two length indexes (positions bucketed by
    canonical length and by compact length) so a mention can visit only the
    buckets its admissible window covers, and a lazy cache of character
    multisets for the overlap bound. Behaves as a plain list."""

    by_len: dict[int, list[int]]
    by_clen: dict[int, list[int]]
    _chars: dict[int, tuple[Counter[str], Counter[str]]]

    def __init__(self, items: Iterable[PreparedCandidate] = ()) -> None:
        super().__init__(items)
        self.by_len = {}
        self.by_clen = {}
        self._chars = {}
        for pos, (_canon, _compact, c_len, c_clen) in enumerate(self):
            self.by_len.setdefault(c_len, []).append(pos)
            self.by_clen.setdefault(c_clen, []).append(pos)

    def chars(self, pos: int) -> tuple[Counter[str], Counter[str]]:
        """Character multisets of the candidate at `pos` (canonical, compact),
        computed on first use — building them for a whole namespace up front
        costs more than the scan they save."""
        got = self._chars.get(pos)
        if got is None:
            canon, compact, _l, _cl = self[pos]
            got = (Counter(canon), Counter(compact))
            self._chars[pos] = got
        return got


def _prepare_candidates(candidates: Iterable[str]) -> PreparedCandidates:
    """(canonical, compact, len(canonical), len(compact), chars, compact chars)
    once per resolution, sorted, with the length indexes built.

    Hoisted out of the per-mention loop so `choose_canonical_candidate` does
    not re-canonicalize, de-duplicate and sort the whole candidate list on
    every call (a further ~6% of the cost).
    """
    prepared: dict[str, PreparedCandidate] = {}
    for raw in candidates:
        canon = canonical_entity_text(raw) if raw else ""
        if not canon or canon in prepared:
            continue
        compact = compact_entity_text(canon)
        prepared[canon] = (canon, compact, len(canon), len(compact))
    return PreparedCandidates(prepared[k] for k in sorted(prepared))


def _length_window(m_len: int, min_score: float) -> tuple[int, int]:
    """Inclusive candidate-length window inside which `_ratio_upper_bound`
    can still reach `min_score`; empty (hi < lo) for a zero-length mention."""
    if m_len <= 0 or min_score <= 0.0:
        return (1, 0) if m_len <= 0 else (1, 10**9)
    lo = math.ceil(min_score * m_len / (2.0 - min_score) - 1e-9)
    hi = math.floor(m_len * (2.0 - min_score) / min_score + 1e-9)
    return max(1, lo), hi


def _overlap_admits(
    prepared: PreparedCandidates,
    pos: int,
    m_len: int,
    m_clen: int,
    m_chars: Counter[str],
    m_cchars: Counter[str],
    min_score: float,
) -> bool:
    """Keep the candidate if EITHER surface (canonical or compact) can still
    reach `min_score` under both the length and the character-overlap bound.
    Length first (integer arithmetic), multisets only when it passes."""
    _canon, _compact, c_len, c_clen = prepared[pos]
    if _ratio_upper_bound(m_len, c_len) >= min_score:
        c_chars, _ = prepared.chars(pos)
        if _overlap_upper_bound(m_chars, m_len, c_chars, c_len) >= min_score:
            return True
    if _ratio_upper_bound(m_clen, c_clen) >= min_score:
        _, c_cchars = prepared.chars(pos)
        if _overlap_upper_bound(m_cchars, m_clen, c_cchars, c_clen) >= min_score:
            return True
    return False


def _blocked_positions(
    prepared: PreparedCandidates, m_len: int, m_clen: int, min_score: float
) -> list[int]:
    """Positions of every candidate whose canonical OR compact length falls in
    the mention's admissible window — exactly the set the length scan kept,
    found by bucket instead of by visiting all candidates. Ascending, so the
    downstream stable sort breaks score ties alphabetically."""
    lo, hi = _length_window(m_len, min_score)
    clo, chi = _length_window(m_clen, min_score)
    positions: set[int] = set()
    for length in range(lo, hi + 1):
        positions.update(prepared.by_len.get(length, ()))
    for length in range(clo, chi + 1):
        positions.update(prepared.by_clen.get(length, ()))
    return sorted(positions)


def _ratio_upper_bound(la: int, lb: int) -> float:
    """Largest `SequenceMatcher.ratio()` two strings of these lengths can reach."""
    if la <= 0 or lb <= 0:
        return 0.0
    return 2.0 * min(la, lb) / (la + lb)


def _overlap_upper_bound(chars_a: Counter[str], la: int, chars_b: Counter[str], lb: int) -> float:
    """Largest `SequenceMatcher.ratio()` two strings with these character
    multisets can reach: matched blocks are a common sub-multiset, so
    2 * |a ∩ b| / (la + lb) bounds 2 * M / T from above."""
    if la <= 0 or lb <= 0:
        return 0.0
    shared = sum(min(n, chars_b[c]) for c, n in chars_a.items() if c in chars_b)
    return 2.0 * shared / (la + lb)


def choose_canonical_candidate(
    mention_text: str,
    candidates: Sequence[str] | Sequence[PreparedCandidate],
    *,
    min_score: float = _MIN_FUZZY_SCORE,
    max_entropy: float = _MAX_NORMALIZED_ENTROPY,
    min_gap: float = _MIN_TOP_GAP,
) -> EntityResolutionDecision:
    """Resolve one mention against namespace-local candidates.

    The gate is intentionally conservative: a candidate must be very similar,
    the top score must dominate the runner-up, and the score distribution must
    be low-entropy. Ambiguous short names therefore become self-canonical.
    """

    mention = canonical_entity_text(mention_text)
    if not mention:
        return EntityResolutionDecision("", "", 0.0, "empty")
    prepared: PreparedCandidates
    if isinstance(candidates, PreparedCandidates):
        prepared = candidates
    elif candidates and isinstance(candidates[0], tuple):
        prepared = PreparedCandidates(cast("Sequence[PreparedCandidate]", candidates))
    else:
        prepared = _prepare_candidates(cast("Sequence[str]", candidates))
    if any(c[0] == mention for c in prepared):
        return EntityResolutionDecision(mention, mention, 1.0, "exact")
    m_compact = compact_entity_text(mention)
    m_len, m_clen = len(mention), len(m_compact)
    m_chars, m_cchars = Counter(mention), Counter(m_compact)
    # Block by length (index), then bound by character overlap, then — only
    # for what survives both — run the real similarity. Both bounds are exact
    # (see the note above), so this decides exactly what the full scan would.
    scored = sorted(
        (
            (prepared[i][0], entity_similarity(mention, prepared[i][0]))
            for i in _blocked_positions(prepared, m_len, m_clen, min_score)
            if prepared[i][0] != mention
            and _overlap_admits(prepared, i, m_len, m_clen, m_chars, m_cchars, min_score)
        ),
        key=lambda item: item[1],
        reverse=True,
    )
    if not scored:
        return EntityResolutionDecision(mention, mention, 1.0, "self")
    best, best_score = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0.0
    entropy = normalized_entropy([score for _, score in scored if score >= min_score])
    if (
        best_score >= min_score
        and (best_score - second) >= min_gap
        and entropy <= max_entropy
        and not _short_name_mismatch(mention, best)
    ):
        return EntityResolutionDecision(mention, best, best_score, "fuzzy")
    return EntityResolutionDecision(mention, mention, 1.0, "self")


async def resolve_entities_for_write(
    ch: Any,
    *,
    org: str,
    namespace: str,
    entity_texts: list[str],
) -> dict[str, str]:
    """Resolve and persist namespace-local mention→canonical mappings."""

    mentions = sorted({canonical_entity_text(e) for e in entity_texts if canonical_entity_text(e)})
    if not mentions:
        return {}
    try:
        rows = await ch.query_all(
            """
            SELECT mention_text, canonical_text, confidence, evidence_count,
                   created_at
            FROM loom.entity_canonical_map FINAL
            WHERE org = {org:String}
              AND namespace = {ns:String}
              AND tombstoned_at IS NULL
            LIMIT {lim:UInt32}
            """,
            params={
                "org": org,
                "ns": namespace,
                "lim": _MAX_NAMESPACE_CANDIDATES,
            },
        )
    except Exception:  # noqa: BLE001 — a failed candidate read keeps mentions as written
        return {mention: mention for mention in mentions}

    by_mention = {
        canonical_entity_text(str(row.get("mention_text") or "")): row
        for row in rows
        if row.get("mention_text")
    }
    candidates = sorted(
        {
            canonical_entity_text(str(row.get("canonical_text") or ""))
            for row in rows
            if row.get("canonical_text")
        }
    )
    # Prepared ONCE for all mentions rather than rebuilt inside every
    # choose_canonical_candidate call.
    prepared_candidates = _prepare_candidates(candidates)
    # Direct attestation per surface: the self-row evidence_count records
    # how often that exact text was itself observed as a mention. Canonical
    # DIRECTION follows attestation — a merge points at the better-evidenced
    # variant, so a typo that happened to be written first cannot
    # permanently canonicalize a correct later spelling into the typo.
    # Ties keep the earlier-seen canonical.
    self_evidence: dict[str, int] = {}
    for row in rows:
        m_text = canonical_entity_text(str(row.get("mention_text") or ""))
        c_text = canonical_entity_text(str(row.get("canonical_text") or ""))
        if m_text and m_text == c_text:
            self_evidence[m_text] = max(
                self_evidence.get(m_text, 0), int(row.get("evidence_count") or 0)
            )

    decisions: list[EntityResolutionDecision] = []
    redirects: dict[str, str] = {}  # flipped canonical -> new canonical
    for mention in mentions:
        existing = by_mention.get(mention)
        if existing is not None:
            mapped = (
                canonical_entity_text(str(existing.get("canonical_text") or mention)) or mention
            )
            mention_evidence = int(existing.get("evidence_count") or 0) + 1
            if mapped != mention and mention_evidence > self_evidence.get(mapped, 0):
                # This observation tips the mention's direct attestation
                # past its current canonical's: flip direction and re-point
                # the old canonical's cluster below.
                redirects[mapped] = mention
                decisions.append(EntityResolutionDecision(mention, mention, 1.0, "self"))
                continue
            decisions.append(
                EntityResolutionDecision(
                    mention,
                    mapped,
                    float(existing.get("confidence") or 1.0),
                    "existing",
                )
            )
            continue
        decision = choose_canonical_candidate(mention, prepared_candidates)
        if decision.strategy == "fuzzy" and self_evidence.get(decision.canonical_text, 0) < 1:
            # The matched canonical was never directly observed as a
            # mention itself — the observed surface is the better-attested
            # variant, so it becomes the canonical.
            redirects[decision.canonical_text] = mention
            decisions.append(
                EntityResolutionDecision(mention, mention, decision.confidence, "self")
            )
            continue
        decisions.append(decision)

    now = naive_utc_now()
    insert_rows: list[dict[str, Any]] = []
    for decision in decisions:
        if not decision.mention_text:
            continue
        existing = by_mention.get(decision.mention_text) or {}
        canonical = redirects.get(decision.canonical_text, decision.canonical_text)
        insert_rows.append(
            {
                "org": org,
                "namespace": namespace,
                "mention_text": decision.mention_text,
                "canonical_text": canonical,
                "confidence": float(decision.confidence),
                "strategy": decision.strategy,
                "evidence_count": int(existing.get("evidence_count") or 0) + 1,
                "created_at": existing.get("created_at") or now,
                "updated_at": now,
                "tombstoned_at": None,
            }
        )
    # Re-point every row in a flipped canonical's cluster (including the old
    # canonical's own self-row) so later lookups — and later sessions of the
    # SAME ingest — resolve through the new canonical. The map is
    # ReplacingMergeTree(version): every persisted column is mirrored from
    # the fetched row; only canonical_text/confidence/updated_at change.
    if redirects:
        decided = {d.mention_text for d in decisions if d.mention_text}
        for row in rows:
            m_text = canonical_entity_text(str(row.get("mention_text") or ""))
            c_text = canonical_entity_text(str(row.get("canonical_text") or ""))
            new_canonical = redirects.get(c_text)
            if not m_text or new_canonical is None or m_text in decided:
                continue
            insert_rows.append(
                {
                    "org": org,
                    "namespace": namespace,
                    "mention_text": m_text,
                    "canonical_text": new_canonical,
                    "confidence": entity_similarity(m_text, new_canonical),
                    "strategy": "fuzzy",
                    "evidence_count": int(row.get("evidence_count") or 0),
                    "created_at": row.get("created_at") or now,
                    "updated_at": now,
                    "tombstoned_at": None,
                }
            )
    if insert_rows:
        try:
            # Waited for: the next session of the same ingest must read these
            # resolutions.
            await ch.insert(
                "loom.entity_canonical_map",
                insert_rows,
                settings=batched_insert_settings(),
            )
        except Exception:  # noqa: BLE001 — a failed map write must not fail the ingest
            pass
    return {
        d.mention_text: redirects.get(d.canonical_text, d.canonical_text)
        for d in decisions
        if d.mention_text
    }


async def lookup_canonical_entities(
    ch: Any,
    *,
    org: str,
    namespaces: list[str],
    entity_texts: list[str],
) -> list[str]:
    """Return raw canonical texts plus namespace-local resolved aliases."""

    # Input order is kept (a cited entity precedes a mentioned
    # one, and the bodies="unit" budget reads the list in order); resolved aliases
    # that were not mentioned follow, sorted.
    ordered: list[str] = []
    for e in entity_texts:
        c = canonical_entity_text(e)
        if c and c not in ordered:
            ordered.append(c)
    mentions = sorted(ordered)
    if not mentions:
        return []
    out = set(mentions)
    try:
        rows = await ch.query_all(
            """
            SELECT canonical_text
            FROM loom.entity_canonical_map FINAL
            WHERE org = {org:String}
              AND namespace IN {namespaces:Array(String)}
              AND mention_text IN {mentions:Array(String)}
              AND tombstoned_at IS NULL
            """,
            params={
                "org": org,
                "namespaces": sorted(set(namespaces)),
                "mentions": mentions,
            },
        )
    except Exception:  # noqa: BLE001 — a failed alias read keeps the given entities
        return ordered
    for row in rows:
        canon = canonical_entity_text(str(row.get("canonical_text") or ""))
        if canon:
            out.add(canon)
    return ordered + sorted(out - set(ordered))


def apply_entity_resolution(raw: str, resolved: dict[str, str] | None) -> str:
    """Apply a precomputed resolution map to one entity surface."""

    canon = canonical_entity_text(raw)
    if not canon:
        return ""
    return (resolved or {}).get(canon, canon)
