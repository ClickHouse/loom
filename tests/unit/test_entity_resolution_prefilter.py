"""The candidate prefilter must be an optimization, never a behaviour change.

Entity resolution decides which mention texts collapse into one graph node. A
prefilter that silently changed a merge would corrupt the graph quietly and
permanently — nothing downstream would error, the edges would just point at the
wrong node. So these tests are about EQUIVALENCE first and speed second.

The prefilter is exact rather than heuristic: `SequenceMatcher.ratio()` is 2*M/T,
bounded above by `2*min(la, lb) / (la + lb)`, so a candidate whose length puts
that bound under `min_score` cannot clear the gate.
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from server.memory.app.entity_resolution import (  # noqa: E402
    _MAX_NORMALIZED_ENTROPY,
    _MIN_FUZZY_SCORE,
    _MIN_TOP_GAP,
    EntityResolutionDecision,
    _blocked_positions,
    _length_window,
    _overlap_upper_bound,
    _prepare_candidates,
    _ratio_upper_bound,
    _short_name_mismatch,
    canonical_entity_text,
    choose_canonical_candidate,
    compact_entity_text,
    entity_similarity,
    normalized_entropy,
)


def _reference(
    mention_text: str,
    candidates: list[str],
    min_score: float = _MIN_FUZZY_SCORE,
    max_entropy: float = _MAX_NORMALIZED_ENTROPY,
    min_gap: float = _MIN_TOP_GAP,
) -> EntityResolutionDecision:
    """The unfiltered implementation, kept verbatim as the oracle."""
    mention = canonical_entity_text(mention_text)
    if not mention:
        return EntityResolutionDecision("", "", 0.0, "empty")
    unique = sorted({canonical_entity_text(c) for c in candidates if c})
    if mention in unique:
        return EntityResolutionDecision(mention, mention, 1.0, "exact")
    scored = sorted(
        ((c, entity_similarity(mention, c)) for c in unique if c != mention),
        key=lambda i: i[1],
        reverse=True,
    )
    if not scored:
        return EntityResolutionDecision(mention, mention, 1.0, "self")
    best, best_score = scored[0]
    second = scored[1][1] if len(scored) > 1 else 0.0
    entropy = normalized_entropy([s for _, s in scored if s >= min_score])
    if (
        best_score >= min_score
        and (best_score - second) >= min_gap
        and entropy <= max_entropy
        and not _short_name_mismatch(mention, best)
    ):
        return EntityResolutionDecision(mention, best, best_score, "fuzzy")
    return EntityResolutionDecision(mention, mention, 1.0, "self")


CANDIDATES = [
    "clickhouse cloud",
    "clickhouse",
    "clickhouse keeper",
    "replicatedmergetree",
    "replacingmergetree",
    "mergetree",
    "aggregatingmergetree",
    "grace hash join",
    "hash join",
    "parallel hash join",
    "s3 multipart upload",
    "s3",
    "azure blob",
    "materialized view",
    "live view",
    "window view",
    "projection",
    "vector similarity index",
    "skip index",
    "primary index",
    "kafka engine",
    "rabbitmq engine",
    "nats engine",
    "async insert",
    "sync insert",
    "lightweight delete",
    "lightweight update",
    "undrop table",
    "drop table",
    "explain actions",
    "explain plan",
    "explain syntax",
    "system drop cache",
    "system reload dictionary",
    "dictget",
    "dictgetordefault",
    "regexp tree",
    "parsedatetimebesteffort",
    "todatetime64",
    "datetrunc",
    "oliver mills",
    "a",
    "ab",
    "abc",
    "abcd",
]


def _mutations(word: str) -> list[str]:
    out = [word, word.replace(" ", "-"), word.replace(" ", "_"), word.upper()]
    if len(word) > 4:
        out += [word[:-1], word[1:], word[: len(word) // 2], word + "s"]
        mid = len(word) // 2
        out.append(word[:mid] + word[mid + 1 :])
        out.append(word[:mid] + "x" + word[mid:])
    return out


@pytest.mark.parametrize("candidate", CANDIDATES)
def test_prefilter_preserves_every_decision(candidate: str) -> None:
    """Each real-shaped candidate, plus typos/truncations/separator variants of
    it, must resolve identically with and without the prefilter."""
    prepared = _prepare_candidates(CANDIDATES)
    for mention in _mutations(candidate):
        got = choose_canonical_candidate(mention, prepared)
        want = _reference(mention, CANDIDATES)
        assert (got.canonical_text, got.strategy) == (
            want.canonical_text,
            want.strategy,
        ), f"prefilter changed the decision for {mention!r}"


def test_prefilter_preserves_decisions_on_random_strings() -> None:
    """Fuzz the space the curated list cannot cover."""
    rng = random.Random(4242)
    alphabet = "abcdefghijklmnopqrstuvwxyz _-"
    pool = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 24))) for _ in range(150)]
    prepared = _prepare_candidates(CANDIDATES)
    for mention in pool:
        got = choose_canonical_candidate(mention, prepared)
        want = _reference(mention, CANDIDATES)
        assert (got.canonical_text, got.strategy) == (
            want.canonical_text,
            want.strategy,
        ), f"prefilter changed the decision for {mention!r}"


def test_ratio_upper_bound_is_actually_an_upper_bound() -> None:
    """The whole argument rests on this. If it is not a bound, the prefilter can
    drop a candidate that would have won."""
    rng = random.Random(7)
    for _ in range(4000):
        a = "".join(rng.choice("abcde") for _ in range(rng.randint(1, 20)))
        b = "".join(rng.choice("abcde") for _ in range(rng.randint(1, 20)))
        assert entity_similarity(a, b) <= _ratio_upper_bound(len(a), len(b)) + 1e-9


def test_bound_degenerates_safely() -> None:
    assert _ratio_upper_bound(0, 5) == 0.0
    assert _ratio_upper_bound(5, 0) == 0.0
    assert _ratio_upper_bound(4, 4) == 1.0


def test_prepare_candidates_dedupes_and_normalises() -> None:
    prepared = _prepare_candidates(["ClickHouse", "clickhouse", " CLICKHOUSE ", ""])
    assert len(prepared) == 1
    canon, compact, lc, lk = prepared[0]
    assert canon == "clickhouse" and compact == "clickhouse"
    assert (lc, lk) == (len(canon), len(compact))
    chars, cchars = prepared.chars(0)
    assert chars["c"] == 2 and sum(cchars.values()) == lk
    assert prepared.by_len == {lc: [0]} and prepared.by_clen == {lk: [0]}


def test_exact_match_still_short_circuits() -> None:
    prepared = _prepare_candidates(CANDIDATES)
    got = choose_canonical_candidate("ClickHouse Cloud", prepared)
    assert (got.canonical_text, got.strategy) == ("clickhouse cloud", "exact")


def test_plain_string_list_still_accepted() -> None:
    """The public signature must keep working — callers outside the hot path pass
    a list of strings."""
    got = choose_canonical_candidate("clickhouse", CANDIDATES)
    assert got.strategy == "exact"


def test_prefilter_is_substantially_cheaper() -> None:
    """The point of the change. Not a tight benchmark — just proof that the
    filter eliminates most of the work at a realistic namespace size."""
    rng = random.Random(3)
    many = [
        "".join(rng.choice("abcdefghijklmnop") for _ in range(rng.randint(4, 30)))
        for _ in range(1500)
    ]
    prepared = _prepare_candidates(many)
    mention = "abcdefghij"
    m_len = len(mention)
    survivors = sum(
        1
        for _c, _k, cl, ckl in prepared
        if _ratio_upper_bound(m_len, cl) >= _MIN_FUZZY_SCORE
        or _ratio_upper_bound(len(mention), ckl) >= _MIN_FUZZY_SCORE
    )
    assert survivors < len(prepared) / 4, (
        f"prefilter kept {survivors} of {len(prepared)} — the length window "
        "should exclude far more than that at min_score=0.95"
    )


# --- character-overlap bound ----------------------------------


def test_overlap_upper_bound_is_actually_an_upper_bound() -> None:
    """Same argument as the length bound: if this is not a bound, a winning
    candidate could be dropped. Fuzzed over a small alphabet so collisions
    and near-anagrams are common."""
    from collections import Counter

    rng = random.Random(11)
    for _ in range(4000):
        a = "".join(rng.choice("abcde -") for _ in range(rng.randint(1, 20)))
        b = "".join(rng.choice("abcde -") for _ in range(rng.randint(1, 20)))
        ca, cb = canonical_entity_text(a), canonical_entity_text(b)
        if not ca or not cb:
            continue
        bound = _overlap_upper_bound(Counter(ca), len(ca), Counter(cb), len(cb))
        assert bound <= _ratio_upper_bound(len(ca), len(cb)) + 1e-9
        # entity_similarity also compares compact forms; bound each surface.
        from difflib import SequenceMatcher

        assert SequenceMatcher(None, ca, cb).ratio() <= bound + 1e-9


def test_overlap_bound_degenerates_safely() -> None:
    from collections import Counter

    assert _overlap_upper_bound(Counter(), 0, Counter("ab"), 2) == 0.0
    assert _overlap_upper_bound(Counter("ab"), 2, Counter("ba"), 2) == 1.0
    assert _overlap_upper_bound(Counter("abc"), 3, Counter("xyz"), 3) == 0.0


def test_overlap_bound_preserves_decisions_on_near_anagrams() -> None:
    """Near-anagrams are exactly where a multiset bound is loosest — they pass
    it and must still be decided by the real similarity, identically to the
    oracle."""
    pool = [
        "clickhouse cloud",
        "cloud clickhouse",
        "clickhosue cloud",
        "clickhouse clud",
        "housecl ick cloud",
        "materialized view",
        "view materialized",
        "materialised view",
    ]
    prepared = _prepare_candidates(CANDIDATES + pool)
    for mention in pool + _mutations("clickhouse cloud") + _mutations("materialized view"):
        got = choose_canonical_candidate(mention, prepared)
        want = _reference(mention, CANDIDATES + pool)
        assert (got.canonical_text, got.strategy) == (want.canonical_text, want.strategy), mention


def test_overlap_bound_removes_most_length_window_survivors() -> None:
    """The point of the second bound: names that merely share a length are
    not near-duplicates, and the character multiset says so before any
    SequenceMatcher pass runs."""
    from collections import Counter

    rng = random.Random(5)
    vocab = [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "omega",
        "sigma",
        "kappa",
        "engine",
        "server",
        "client",
        "router",
        "worker",
        "index",
        "vector",
        "table",
        "query",
    ]
    many = sorted(
        {" ".join(rng.choice(vocab) for _ in range(rng.randint(1, 3))) for _ in range(3000)}
    )
    prepared = _prepare_candidates(many)
    mention = canonical_entity_text("gamma router zeta")
    m_len, m_chars = len(mention), Counter(mention)
    by_length = [
        i for i, c in enumerate(prepared) if _ratio_upper_bound(m_len, c[2]) >= _MIN_FUZZY_SCORE
    ]
    by_overlap = [
        i
        for i in by_length
        if _overlap_upper_bound(m_chars, m_len, prepared.chars(i)[0], prepared[i][2])
        >= _MIN_FUZZY_SCORE
    ]
    assert len(by_length) > 20, "fixture too small to say anything"
    assert len(by_overlap) <= len(by_length) // 10, (len(by_overlap), len(by_length))


def test_length_window_matches_the_bound_exactly() -> None:
    """The index must admit precisely the lengths the scan's bound admitted —
    one off at either edge would drop a candidate the scan kept, or keep one
    it dropped (harmless, but then the window is not what the comment says)."""
    for min_score in (0.95, 0.9, 0.8, 0.5):
        for m in range(1, 80):
            lo, hi = _length_window(m, min_score)
            for c in range(1, 200):
                inside = lo <= c <= hi
                assert inside == (_ratio_upper_bound(m, c) >= min_score - 1e-12), (min_score, m, c)


def test_blocked_positions_equal_the_length_scan() -> None:
    """Bucket lookup and full scan must select the same candidates, in the same
    (alphabetical) order, on either surface."""
    rng = random.Random(9)
    alphabet = "abcdefghijklmnopqrstuvwxyz -_"
    many = ["".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30))) for _ in range(800)]
    prepared = _prepare_candidates(many)
    for _ in range(60):
        mention = canonical_entity_text(
            "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 30)))
        )
        if not mention:
            continue
        m_len, m_clen = len(mention), len(compact_entity_text(mention))
        scan = [
            i
            for i, (_c, _k, cl, ckl) in enumerate(prepared)
            if _ratio_upper_bound(m_len, cl) >= _MIN_FUZZY_SCORE
            or _ratio_upper_bound(m_clen, ckl) >= _MIN_FUZZY_SCORE
        ]
        assert _blocked_positions(prepared, m_len, m_clen, _MIN_FUZZY_SCORE) == scan, mention


def test_blocking_is_substantially_cheaper_than_the_scan() -> None:
    """The prefilter's reason to exist. Loose on purpose (CI machines vary), but
    the order of magnitude must hold: the cold path on a 5,000-row namespace
    goes from ~728 ms to ~56 ms for 20 new mentions."""
    import time

    rng = random.Random(21)
    vocab = [
        "alpha",
        "beta",
        "gamma",
        "delta",
        "omega",
        "sigma",
        "kappa",
        "engine",
        "server",
        "client",
        "router",
        "worker",
        "index",
        "vector",
        "table",
        "query",
    ]
    many = sorted(
        {
            " ".join(rng.choice(vocab) + str(rng.randint(0, 99)) for _ in range(rng.randint(1, 3)))
            for _ in range(6000)
        }
    )[:3000]
    mentions = [" ".join(rng.choice(vocab) for _ in range(2)) + f" zz{i}" for i in range(10)]
    prepared = _prepare_candidates(many)
    t0 = time.perf_counter()
    for m in mentions:
        choose_canonical_candidate(m, prepared)
    blocked = time.perf_counter() - t0
    t0 = time.perf_counter()
    for m in mentions:
        _reference(m, many)
    scan = time.perf_counter() - t0
    assert blocked * 3 < scan, f"blocked {blocked * 1000:.0f} ms vs scan {scan * 1000:.0f} ms"
