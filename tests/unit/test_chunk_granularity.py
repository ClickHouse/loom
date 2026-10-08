"""bench/chunk_granularity.py — arm construction and scoring (network-free)."""

from __future__ import annotations

from bench.chunk_granularity import (
    chunks_by_chars,
    chunks_by_segments,
    chunks_by_turns,
    expand_neighbors,
    gold_turns,
    parse_segments,
    recall_at_budget,
    render_turn,
    score_hits,
    score_turn_hits,
    turn_precision,
)

LINES = [f"[user] Caroline: sentence number {i} about topic {i // 4}." for i in range(10)]


def test_turn_arms_tile_the_session() -> None:
    c3 = chunks_by_turns(7, LINES, 3)
    assert [(c.turn_start, c.turn_end) for c in c3] == [(0, 2), (3, 5), (6, 8), (9, 9)]
    assert all(c.session_no == 7 for c in c3) and c3[1].text.count("\n") == 2
    one = chunks_by_turns(7, LINES, 1)
    assert len(one) == 10 and one[4].text == LINES[4]


def test_char_arm_uses_looms_splitter_and_recovers_turn_spans() -> None:
    cs = chunks_by_chars(2, LINES, 120)
    assert len(cs) > 1
    assert cs[0].turn_start == 0
    assert all(c.turn_end >= c.turn_start for c in cs)
    assert cs[-1].turn_end == len(LINES) - 1


def test_segment_parse_requires_an_exact_tiling() -> None:
    good = '<segmentation>\n{"segment_id":0,"start_turn_number":0,"end_turn_number":3,"num_turns":4}\n{"segment_id":1,"start_turn_number":4,"end_turn_number":9,"num_turns":6}\n</segmentation>'
    assert parse_segments(good, 10) == [(0, 3), (4, 9)]
    gap = '{"start_turn_number":0,"end_turn_number":3}\n{"start_turn_number":5,"end_turn_number":9}'
    assert parse_segments(gap, 10) is None
    short = '{"start_turn_number":0,"end_turn_number":7}'
    assert parse_segments(short, 10) is None
    segs = chunks_by_segments(1, LINES, [(0, 3), (4, 9)])
    assert [c.text.count("\n") + 1 for c in segs] == [4, 6]


def test_scoring_over_gold_sessions() -> None:
    hit, recall, rr, dcg = score_hits([3, 3, 5, 9], {5, 9}, 10)
    assert hit == 1 and recall == 1.0 and rr == 1 / 3
    assert round(dcg, 3) == round(1 / 2 + 1 / 2.321928, 3)
    assert score_hits([1, 2], {5}, 10) == (0, 0.0, 0.0, 0.0)


def test_render_turn_matches_looms_chunk_line() -> None:
    line = render_turn({"speaker": "Melanie", "text": "Hi!", "blip_caption": "a dog"})
    assert line == "[user] Melanie: Hi! [shared a photo: a dog]"


def test_turn_level_scoring_uses_chunk_spans() -> None:
    q = {"evidence": ["D3:5", "D3:9", "D7:1"]}
    assert gold_turns(q) == {(3, 4), (3, 8), (7, 0)}
    # hits: (session, turn_start, turn_end); the first covers D3:5, the third covers D3:9
    spans = [(3, 3, 5), (1, 0, 4), (3, 6, 9), (7, 2, 4)]
    hit, recall, rr, dcg = score_turn_hits(spans, gold_turns(q), 10)
    assert hit == 1 and recall == 2 / 3 and rr == 1.0
    assert round(dcg, 3) == round(1.0 + 1 / 2.0, 3)
    assert score_turn_hits([(1, 0, 2)], gold_turns(q), 10) == (0, 0.0, 0.0, 0.0)


def test_budget_recall_and_precision() -> None:
    gold = {(3, 4), (3, 8)}
    spans = [(3, 3, 5), (1, 0, 4), (3, 6, 9)]
    chars = [1000, 2500, 1000]
    assert recall_at_budget(spans, chars, gold, 3000) == 0.5  # third hit does not fit
    assert recall_at_budget(spans, chars, gold, 6000) == 1.0
    assert recall_at_budget(spans, chars, gold, 500) == 0.5  # first hit always counts
    assert turn_precision(spans, gold) == 2 / 12


def test_neighbor_expansion_adds_adjacent_chunks_once() -> None:
    spans = {(3, 0): (0, 2), (3, 1): (3, 5), (3, 2): (6, 8), (3, 3): (9, 9)}
    chars = {"3:0": 100, "3:1": 100, "3:2": 100, "3:3": 50}
    out_spans, out_chars = expand_neighbors([(3, 3, 5), (3, 6, 8)], [100, 100], spans, chars, 1)
    # hit 1 (idx 1) brings idx 2 and idx 0; hit 2 (idx 2) is already present, brings idx 3
    assert out_spans == [(3, 3, 5), (3, 6, 8), (3, 0, 2), (3, 9, 9)]
    assert out_chars == [100, 100, 100, 50]
