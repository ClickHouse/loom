"""bench/hard_distractors.py — the pure parts: mutation, distance filter, keys,
citation scoring and the report arithmetic."""

from __future__ import annotations

import random

from bench import hard_distractors as hd

GOLD = "Alice moved to Berlin on 2024-03-15 and the team size is 12; deploys are enabled."


def test_mutations_change_exactly_the_fact_that_matters() -> None:
    rng = random.Random(1)
    n = hd.mutate_numbers(GOLD, rng)
    assert n is not None and n != GOLD and "team size is" in n
    d = hd.mutate_dates(GOLD, rng)
    assert d is not None and d != GOLD and "2024-03-15" not in d and "-03-" in d or "-02-" in d
    e = hd.mutate_entities(GOLD, ["Oslo", "Bob"], rng)
    assert e is not None and e != GOLD and ("Oslo" in e or "Bob" in e)
    p = hd.mutate_polarity(GOLD, rng)
    assert p is not None and p != GOLD and ("disabled" in p or "is not" in p)
    assert hd.mutate_numbers("no digits here", rng) is None
    # a date is never touched by the number mutation, and zero-padding survives
    assert hd.mutate_numbers("on 2024-03-15 only", rng) is None
    z = hd.mutate_numbers("build 007 shipped", random.Random(3))
    assert z is not None and len(z.split()[1]) == 3
    assert hd.mutate_dates("no dates here", rng) is None
    assert hd.mutate_entities("lowercase only", ["Bob"], rng) is None
    assert hd.mutate_polarity("nothing to flip", rng) is None


def test_levenshtein_and_normalised_distance() -> None:
    assert hd.levenshtein("kitten", "sitting") == 3
    assert hd.levenshtein("", "abc") == 3 and hd.levenshtein("abc", "abc") == 0
    assert hd.normalised_distance("abcd", "abce") == 0.25
    assert hd.normalised_distance("", "") == 0.0


def test_make_distractors_is_deterministic_near_and_never_the_gold() -> None:
    pool = hd.entity_pool([GOLD, "Carol runs the Paris office."])
    assert pool == ["Berlin", "Paris"]  # sentence-initial words are not entities here
    a = hd.make_distractors(GOLD, per_gold=4, pool=pool, seed=7)
    b = hd.make_distractors(GOLD, per_gold=4, pool=pool, seed=7)
    assert a == b and 1 <= len(a) <= 4 and len(set(a)) == len(a)
    for d in a:
        assert d != GOLD
        assert 0.0 < hd.normalised_distance(GOLD, d) <= 0.35
    assert hd.make_distractors(GOLD, per_gold=4, pool=pool, seed=8) != a or len(a) <= 1


def test_distractor_keys_share_no_substring_with_the_gold_key() -> None:
    k = hd.distractor_key("user_task_deadline", 3)
    assert k.startswith(hd.KEY_PREFIX) and k.endswith("_03")
    assert "user_task_deadline" not in k and "deadline" not in k
    assert hd.distractor_key("user_task_deadline", 4) != k
    assert hd.distractor_key("other", 3) != k


def test_value_text_prefers_a_text_field_and_falls_back_to_json() -> None:
    assert hd.value_text("plain") == "plain"
    assert hd.value_text({"text": "t", "x": 1}) == "t"
    assert hd.value_text({"a": 1}) == '{"a": 1}'


def test_cite_score_is_plus_one_gold_minus_one_per_other() -> None:
    results = [{"memory_key": "gold_k"}, {"memory_key": "dstr_ab_00"}, {"memory_key": "other"}]
    cited = hd.cited_keys("Blue [gold_k] and also [#dstr_ab_00] and [`other`].", results)
    assert cited == ["gold_k", "dstr_ab_00", "other"]
    assert hd.cite_score(cited, ["gold_k"]) == (-1.0, True)
    assert hd.cite_score(["gold_k"], ["gold_k"]) == (1.0, True)
    assert hd.cite_score(["dstr_ab_00"], ["gold_k"]) == (-1.0, False)
    assert hd.cite_score([], ["gold_k"]) == (0.0, False)
    assert hd.cited_keys("no markers", results) == []


def test_summarise_and_render() -> None:
    rows = [
        hd.QRow(
            query="q1",
            gold_keys=["g1"],
            written=3,
            rank_before=1,
            rank_after=2,
            intrusion=0.3,
            top_intruder_rank=1,
            cite_before=1.0,
            cite_after=-1.0,
            gold_cited_before=True,
            gold_cited_after=True,
        ),
        hd.QRow(
            query="q2",
            gold_keys=["g2"],
            written=3,
            rank_before=2,
            rank_after=None,
            intrusion=0.5,
            top_intruder_rank=1,
            cite_before=1.0,
            cite_after=0.0,
            gold_cited_before=True,
            gold_cited_after=False,
        ),
        hd.QRow(query="q3", gold_keys=["g3"], error="before: boom"),
    ]
    rep = hd.summarise(rows, k=10, per_gold=3)
    assert rep.n == 2 and rep.written == 6
    assert rep.recall_before == 1.0 and rep.recall_after == 0.5
    assert rep.mrr_before == 0.75 and rep.mrr_after == 0.25
    assert rep.intrusion_mean == 0.4 and rep.gold_outranked == 2
    assert rep.cite_before == 1.0 and rep.cite_after == -0.5
    assert rep.gold_cited_before == 1.0 and rep.gold_cited_after == 0.5
    text = hd.render(rep)
    assert "| recall@10 | 1.000 | 0.500 |" in text and "cite score" in text
    assert "2/2" in text
