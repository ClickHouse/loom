"""A score is a cosine, abstain is legible per row.

Three contracts: the identifier lift is bounded to the pool's
own ceiling instead of `ceiling + 1.0`; every served row whose un-boosted
similarity sits below the abstain threshold carries `below_threshold`; and a
row above the threshold serialises byte-for-byte as before (the flag is off
the wire unless set)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.verdict import _flag_below_threshold
from server.memory.domain.query_planner import pin_identifier_hits


def _hit(key: str, text: str, score: float, **comps: float) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        content_excerpt=text,
        rank_score=score,
        distance=1.0 - min(score, 1.0),
        score_components={"rank_score": score, **comps},
    )


# ---------------------------------------------------------------- the lift


def test_identifier_lift_is_bounded_to_the_pool_ceiling() -> None:
    """The matching row lands just above the best non-match — its score
    stays on the pool's scale, never `ceiling + 1.0`."""
    hits = [
        _hit("a", "unrelated prose", 0.62),
        _hit("b", "mentions BackupIO_S3 here", 0.55),
        _hit("c", "another unrelated", 0.50),
    ]
    assert pin_identifier_hits(hits, ["BackupIO_S3"]) == 1
    assert [h.memory_key for h in hits] == ["b", "a", "c"]
    lifted = hits[0].rank_score
    assert 0.62 < lifted < 0.62 + 1e-4
    assert lifted <= 1.0
    assert hits[0].score_components["identifier_match"] == 1.0
    assert abs(hits[0].score_components["identifier_lift"] - (lifted - 0.55)) < 1e-12
    assert hits[0].score_components["rank_score"] == lifted
    # Non-matches are untouched.
    assert hits[1].rank_score == 0.62 and "identifier_lift" not in hits[1].score_components


def test_identifier_lift_keeps_the_matched_order_and_never_lowers() -> None:
    """Matched rows keep their relative order; a row already above every
    non-match keeps its own score (lift 0 up to the ε tiebreak)."""
    hits = [
        _hit("top", "id 0f3a9c2e7b1d has it", 0.90),
        _hit("mid", "plain", 0.70),
        _hit("low", "0f3a9c2e7b1d again", 0.40),
        _hit("tail", "plain", 0.30),
    ]
    assert pin_identifier_hits(hits, ["0f3a9c2e7b1d"]) == 2
    assert [h.memory_key for h in hits] == ["top", "low", "mid", "tail"]
    assert hits[0].rank_score >= 0.90  # own score kept (+ε)
    assert hits[0].rank_score - 0.90 < 1e-5
    assert hits[0].score_components["identifier_lift"] < 1e-5
    assert 0.70 < hits[1].rank_score < 0.70 + 1e-4  # lifted to the ceiling, not +1.0
    assert hits[0].rank_score > hits[1].rank_score > hits[2].rank_score > hits[3].rank_score


def test_identifier_lift_under_rrf_scale_stays_under_rrf_scale() -> None:
    """RRF fused scores are ≈ 1/(60+rank); the lift must not leave that band."""
    hits = [_hit("a", "plain", 0.0328), _hit("b", "PR 116016 fixed it", 0.0161)]
    assert pin_identifier_hits(hits, ["116016"]) == 1
    assert hits[0].memory_key == "b"
    assert 0.0328 < hits[0].rank_score < 0.04


def test_all_matching_rows_need_no_lift() -> None:
    hits = [_hit("a", "abc123xyz", 0.7), _hit("b", "abc123xyz too", 0.6)]
    assert pin_identifier_hits(hits, ["abc123xyz"]) == 2
    assert hits[0].rank_score == 0.7 and hits[1].rank_score == 0.6
    assert "identifier_lift" not in hits[0].score_components


# ------------------------------------------------------------ the row flag


def _wire_hit(key: str, distance: float, source: str = "fact", **comps: float) -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt="x",
        distance=distance,
        similarity=1.0 - distance,
        rank_score=1.0 - distance,
        source=source,
        score_components=dict(comps),
    )


def test_below_threshold_flags_only_cosine_rows_under_the_gate() -> None:
    hits = [
        _wire_hit("strong", 0.40),  # 0.60 ≥ 0.30
        _wire_hit("weak", 0.75),  # 0.25 < 0.30
        _wire_hit("chunk:weak", 0.80, source="chunk"),  # 0.20 < 0.30
        _wire_hit("record", 0.90, source="record"),  # distance is not a cosine
        _wire_hit("derived_aggregate", 0.0, source="derived"),
        _wire_hit("anchor", 1.0, anchor_first=1.0),  # tier row: similarity 0 by construction
        _wire_hit("recent", 1.0, recent_first=1.0),
    ]
    assert _flag_below_threshold(hits, 0.30) == 2
    flagged = {h.memory_key for h in hits if h.below_threshold}
    assert flagged == {"weak", "chunk:weak"}
    for h in hits:
        if h.memory_key not in flagged:
            assert h.below_threshold is None


def test_below_threshold_is_a_no_op_without_a_threshold() -> None:
    hits = [_wire_hit("weak", 0.9)]
    assert _flag_below_threshold(hits, 0.0) == 0
    assert hits[0].below_threshold is None
    assert _flag_below_threshold([], 0.3) == 0


def test_below_threshold_boundary_is_strict() -> None:
    hits = [_wire_hit("at", 0.70)]  # similarity exactly 0.30
    assert _flag_below_threshold(hits, 0.30) == 0


# ----------------------------------------------------------------- the wire


def test_unflagged_hit_serialises_byte_identically() -> None:
    """A row the flag never touched carries no `below_threshold` key: the
    payload every existing consumer parses is unchanged."""
    h = _wire_hit("k", 0.4)
    data = h.model_dump(mode="json")
    assert "below_threshold" not in data
    assert "below_threshold" not in json.dumps(data)


def test_flagged_hit_carries_the_key() -> None:
    h = _wire_hit("k", 0.9)
    assert _flag_below_threshold([h], 0.3) == 1
    data = h.model_dump(mode="json")
    assert data["below_threshold"] is True
    # Round trip: a parked page re-reads the row with the flag intact.
    again = _MemorySearchHit.model_validate(data)
    assert again.below_threshold is True


def test_similarity_is_bounded_on_the_wire() -> None:
    h = _wire_hit("k", 0.0)
    assert h.model_dump(mode="json")["similarity"] <= 1.0


# ------------------------------------------------------ the abstain envelope


def test_finalize_abstain_flags_rows_and_counts_them_into_work(monkeypatch) -> None:
    """The response-level verdict and the row-level flag come from one call:
    the gate reads rank 1 (0.60 → no abstain) while the tail rows under the
    threshold are marked and counted."""
    from server.memory.app.search.verdict import _finalize_abstain
    from server.memory.domain.retrieval_calibration import RetrievalCalibration
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_identifier_grounding", False, raising=False)
    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "first", raising=False)
    hits = [_wire_hit("a", 0.40), _wire_hit("b", 0.65), _wire_hit("c", 0.80)]
    work: dict[str, object] = {}
    abstain, code, _reason = _finalize_abstain(
        hits,
        calibration=RetrievalCalibration(0.6, "medium", 1.0, ""),
        abstain_threshold=0.30,
        query_text="plain prose",
        work=work,
    )
    assert (abstain, code) == (False, "")
    assert [h.below_threshold for h in hits] == [None, None, True]
    assert work == {"below_threshold_hits": 1}


def test_finalize_abstain_below_threshold_flags_every_row(monkeypatch) -> None:
    from server.memory.app.search.verdict import _finalize_abstain
    from server.memory.domain.retrieval_calibration import RetrievalCalibration
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_identifier_grounding", False, raising=False)
    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "first", raising=False)
    hits = [_wire_hit("a", 0.75), _wire_hit("b", 0.80)]
    abstain, code, _reason = _finalize_abstain(
        hits,
        calibration=RetrievalCalibration(0.1, "abstain", 1.0, ""),
        abstain_threshold=0.30,
    )
    assert (abstain, code) == (True, "below_threshold")
    assert all(h.below_threshold for h in hits)


# ------------------------------------------------- the grounding leg's reach


def test_identifier_grounding_applies_to_questions_not_documents() -> None:
    from server.memory.domain.retrieval_calibration import identifier_grounding_applies

    assert identifier_grounding_applies("fix status of PR 113594", 32)
    doc = " ".join(["src", "Storages", "MergeTree", "X", "cpp"] * 10) + " Closes #116016"
    assert len(doc.split()) > 32
    assert not identifier_grounding_applies(doc, 32)
    assert identifier_grounding_applies(doc, 0)  # 0 = no length limit
    assert identifier_grounding_applies("", 32)


def test_long_query_is_not_abstained_for_an_incidental_number(monkeypatch) -> None:
    """A tokenised path list with a PR number nobody's excerpt names keeps its
    on-topic hits; the same identifier in a short question still abstains."""
    from server.memory.app.search.verdict import _finalize_abstain
    from server.memory.domain.retrieval_calibration import RetrievalCalibration
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_identifier_grounding", True, raising=False)
    monkeypatch.setattr(
        settings, "memory_search_identifier_grounding_max_tokens", 32, raising=False
    )
    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "first", raising=False)
    cal = RetrievalCalibration(0.6, "medium", 1.0, "")
    hits = [_wire_hit("finding_h_pr20650_001", 0.04), _wire_hit("lesson_1", 0.35)]
    hits[0].content_excerpt = "TPC-H Q6 keeps float bounds; discount 0.07 never matches"
    long_q = " ".join(["tests", "private_performance", "tpch", "query"] * 10) + " see PR 70136"
    abstain, code, _ = _finalize_abstain(
        hits, calibration=cal, abstain_threshold=0.3, query_text=long_q
    )
    assert (abstain, code) == (False, "")
    abstain, code, reason = _finalize_abstain(
        hits, calibration=cal, abstain_threshold=0.3, query_text="fix status of PR 70136"
    )
    assert (abstain, code) == (True, "entity_absent") and "70136" in reason


def test_grounding_reads_the_memory_key(monkeypatch) -> None:
    """`finding_h_pr116016` names its PR in the key: a self-hit grounds the
    query even when the excerpt clip does not repeat the number."""
    from server.memory.app.search.verdict import _finalize_abstain
    from server.memory.domain.retrieval_calibration import RetrievalCalibration
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_identifier_grounding", True, raising=False)
    monkeypatch.setattr(
        settings, "memory_search_identifier_grounding_max_tokens", 32, raising=False
    )
    monkeypatch.setattr(settings, "memory_search_abstain_top_sim_scope", "first", raising=False)
    hits = [_wire_hit("finding_h_pr116016", 0.4)]
    hits[0].content_excerpt = "Recursive CTE with a self-join branch hangs"
    abstain, code, _ = _finalize_abstain(
        hits,
        calibration=RetrievalCalibration(0.6, "medium", 1.0, ""),
        abstain_threshold=0.3,
        query_text="what happened with PR 116016",
    )
    assert (abstain, code) == (False, "")


def test_grounding_cap_default_and_comment() -> None:
    import inspect

    from server.settings import Settings, catalog

    assert Settings.model_fields["memory_search_identifier_grounding_max_tokens"].default == 32
    src = inspect.getsource(catalog.MemorySearchSettings)
    i = src.index("memory_search_identifier_grounding_max_tokens")
    assert "#" in src[i - 900 : i]
