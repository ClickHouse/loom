"""Unit tests for query-aware transcript-chunk passage trimming + dedup.

The trimmer reduces a multi-turn chunk excerpt to its most query-relevant
turn(s) at read time, shrinking the per-query context-token footprint without
dropping the answer span or speaker attribution. The dedup drops a chunk whose
passage is near-identical to a higher-ranked item already in the pool.

Safety is the load-bearing property: the gold answer span and its speaker tag
must survive every trim, and the only-carrier of an entity must never be
deduped.
"""

from __future__ import annotations

from server.memory.domain.passage_trim import (
    _content_token_set,
    is_near_duplicate,
    token_set_is_near_duplicate,
    trim_chunk_excerpt,
)

# A realistic over-budget transcript chunk (multi-turn, speaker-marked).
_PILSNER_CHUNK = (
    "[user] I've never made a lamb stew before. Do you have any "
    "recommendations for a good type of beer to use in the recipe? I want "
    "something authentic to the region and not too overpowering for the dish.\n"
    "[assistant] I don't have personal preferences, but in general, it's "
    "recommended to use a light or medium-bodied beer for this recipe to "
    "avoid overpowering the flavors of the lamb and spices. A Pilsner or "
    "lager would work well, but you can also use a local beer from Ancash to "
    "give the dish a more authentic touch. Ultimately it comes down to your "
    "personal preference.\n"
    "[user] I'll definitely try this recipe with a Pilsner. Is there "
    "anything else I should know before I start cooking the seco de cordero?"
)


def test_under_budget_passthrough() -> None:
    short = "[assistant] Jaipur Rugs is a private company."
    assert trim_chunk_excerpt(short, "what company") == short


def test_empty_inputs() -> None:
    assert trim_chunk_excerpt("", "query") == ""
    # No query signal: cannot score units, so keep the excerpt unchanged
    # rather than trim arbitrarily and risk dropping the answer.
    assert trim_chunk_excerpt(_PILSNER_CHUNK, "") == _PILSNER_CHUNK


def test_turn_split_selects_relevant_turn() -> None:
    # Query about the beer recommendation should land on the assistant turn
    # that names the beer (+ the adjacent user turns as window).
    out = trim_chunk_excerpt(_PILSNER_CHUNK, "what beer to use in the lamb recipe")
    assert "Pilsner" in out
    # Shorter than the original (a real trim happened).
    assert len(out) < len(_PILSNER_CHUNK)


def test_gold_span_survives_trim() -> None:
    # The gold-answer phrase ("try this recipe with a Pilsner") must survive
    # when the query targets it.
    out = trim_chunk_excerpt(_PILSNER_CHUNK, "what beer will I try the recipe with")
    assert "Pilsner" in out


def test_speaker_marker_preserved() -> None:
    out = trim_chunk_excerpt(_PILSNER_CHUNK, "what beer to use")
    # Whatever turn(s) survive, they carry a speaker tag — attribution is
    # never dropped to a bare sentence fragment.
    assert "[user]" in out or "[assistant]" in out


def test_no_mid_attribution_cut() -> None:
    # Every kept line that has content should begin with a speaker marker (or
    # be a windowed continuation glued to a marker'd turn). Assert no kept
    # line is an attribution-less fragment of a turn that *had* a marker.
    out = trim_chunk_excerpt(_PILSNER_CHUNK, "beer recommendation pilsner")
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        # Each non-empty line of the windowed output is a full turn and must
        # carry its speaker tag.
        assert line.startswith("["), f"line lost its speaker tag: {line!r}"


def test_long_single_turn_keeps_marker() -> None:
    # A single very long turn (no second speaker) should be sentence-windowed
    # but keep its marker.
    long_turn = (
        "[assistant] "
        + " ".join(f"Sentence number {i} about miscellaneous topics here." for i in range(40))
        + " The secret password is hunter2 and that is the key fact."
    )
    out = trim_chunk_excerpt(long_turn, "what is the secret password")
    assert out.startswith("[assistant]")
    assert "hunter2" in out
    assert len(out) < len(long_turn)


def test_budget_is_soft_for_answer_bearing_turn() -> None:
    # A single best turn that itself exceeds the budget but has no internal
    # sentence boundaries is kept whole rather than cut mid-span.
    one_sentence_turn = "[assistant] " + "word " * 200 + "Pilsner"
    out = trim_chunk_excerpt(one_sentence_turn, "pilsner beer")
    assert "Pilsner" in out
    assert out.startswith("[assistant]")


def test_window_includes_adjacent_turn() -> None:
    # Best turn + at least one adjacent turn (window) when budget allows.
    chunk = (
        "[user] Tell me about the meeting.\n"
        "[assistant] The meeting is at noon in room 5.\n"
        "[user] Thanks, I'll be there at noon.\n"
        "[assistant] Great, see you then in room 5.\n"
    ) * 3  # repeat to push over budget
    out = trim_chunk_excerpt(chunk, "where is the meeting room")
    assert "room 5" in out


# --- dedup ------------------------------------------------------------------


def test_near_dup_dropped() -> None:
    a = "[assistant] The capital of France is Paris and it is on the Seine."
    b = "[assistant] The capital of France is Paris and it is on the Seine river."
    assert is_near_duplicate(b, a) is True


def test_unique_kept() -> None:
    a = "[assistant] The capital of France is Paris."
    b = "[user] My favorite movie is the Soviet cartoon Nu pogodi."
    assert is_near_duplicate(b, a) is False


def test_dedup_never_drops_sole_answer_carrier() -> None:
    # A chunk that adds the answer entity the kept item lacks is NOT a
    # near-duplicate even if it shares boilerplate framing.
    kept = "[assistant] Here are some recommendations for you to consider today."
    candidate = (
        "[assistant] Here are some recommendations: the website Mindful.org "
        "offers free guided meditation exercises."
    )
    assert is_near_duplicate(candidate, kept) is False


def test_tiny_passages_never_dedup() -> None:
    # Too few tokens to be confident two short passages are the same evidence.
    assert is_near_duplicate("yes okay", "yes okay sure") is False


def test_containment_drops_subset() -> None:
    kept = (
        "[assistant] Jessica Poole (@jessica_poole_jewellery) is a UK-based "
        "jewelry designer who handcrafts bespoke rings."
    )
    candidate = "[assistant] Jessica Poole is a UK-based jewelry designer."
    # candidate's tokens are essentially a subset of the kept item.
    assert is_near_duplicate(candidate, kept) is True


def test_token_set_variant_matches_string_variant() -> None:
    # The pre-tokenized comparator (used by the O(n^2) dedup pool to avoid
    # re-tokenizing both sides on every pairwise call) must make the exact same
    # near-duplicate decision as the string-level helper.
    cases = [
        # (candidate, kept)
        (
            "Jessica Poole UK jewelry designer unusual gemstones rings",
            "Jessica Poole UK jewelry designer unusual gemstones rings timeless",
        ),
        (
            "Jessica Poole UK jewelry designer",
            "hiking trails mountains winter season completely unrelated text here",
        ),
        (
            "[assistant] Jessica Poole is a UK-based jewelry designer",
            "[assistant] Jessica Poole (@jessica_poole_jewellery) UK-based jewelry "
            "designer handcrafts bespoke rings",
        ),
        ("yes okay", "yes okay sure"),  # tiny — never dedup
        ("", "anything"),
    ]
    for cand, kept in cases:
        str_decision = is_near_duplicate(cand, kept)
        set_decision = token_set_is_near_duplicate(
            _content_token_set(cand), _content_token_set(kept)
        )
        assert str_decision == set_decision, (cand, kept)
