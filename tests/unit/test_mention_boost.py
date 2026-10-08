"""Pool-consensus mention boost."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.domain.mention_boost import apply_mention_boost, entity_consensus


def _hit(key: str, score: float, ents: list[str]) -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key, rank_score=score, distance=1 - score, entities=ents, score_components={}
    )


def test_consensus_drops_ubiquitous_entities_and_counts_bearing_hits() -> None:
    hits = [
        _hit("a", 0.5, ["Caroline", "adoption"]),
        _hit("b", 0.4, ["caroline", "adoption", "mentor"]),
        _hit("c", 0.3, ["caroline", "hiking"]),
        _hit("c2", 0.25, ["caroline"]),
        _hit("d", 0.2, []),
    ]
    counts, bearing = entity_consensus(hits, ubiquity=0.5)
    assert bearing == 4
    assert "caroline" not in counts  # 4 of 4 entity-bearing hits > 50 %: no signal
    assert counts["adoption"] == 2 and counts["mentor"] == 1 and counts["hiking"] == 1


def test_boost_moves_agreeing_hit_up_is_capped_and_recorded() -> None:
    hits = [
        _hit("top", 0.50, ["hiking"]),  # singleton entity: no boost
        _hit("agree", 0.48, ["adoption", "mentor"]),
        _hit("agree2", 0.30, ["adoption", "mentor"]),
        _hit("agree3", 0.10, ["adoption"]),
        _hit("bare", 0.05, []),
    ]
    work = apply_mention_boost(hits, weight=0.1, cap=0.08, ubiquity=0.9)
    assert work["mention_boost_applied"] == 3 and 0 < work["mention_boost_max"] <= 0.08
    assert [h.memory_key for h in hits][:2] == ["agree", "top"]  # 0.48 + boost > 0.50
    assert hits[0].score_components["mention_boost"] == work["mention_boost_max"]
    assert next(h for h in hits if h.memory_key == "top").rank_score == 0.50
    assert next(h for h in hits if h.memory_key == "bare").rank_score == 0.05
    # cap binds: weight 1.0 still adds at most prior_cap
    hits2 = [_hit("x", 0.1, ["a"]), _hit("y", 0.1, ["a"]), _hit("z", 0.1, ["a"])]
    work2 = apply_mention_boost(hits2, weight=1.0, cap=0.02, ubiquity=1.0)
    assert work2["mention_boost_max"] == 0.02 and all(
        abs(h.rank_score - 0.12) < 1e-9 for h in hits2
    )


def test_boost_is_a_no_op_when_off_or_without_consensus() -> None:
    hits = [_hit("a", 0.5, ["x"]), _hit("b", 0.4, ["y"])]
    before = [(h.memory_key, h.rank_score) for h in hits]
    assert apply_mention_boost(hits, weight=0.0, cap=0.08) == {
        "mention_boost_applied": 0,
        "mention_boost_max": 0.0,
    }
    assert apply_mention_boost(hits, weight=0.2, cap=0.08)["mention_boost_applied"] == 0
    assert [(h.memory_key, h.rank_score) for h in hits] == before
    assert apply_mention_boost([], weight=0.2, cap=0.08)["mention_boost_applied"] == 0


def test_entities_by_key_serves_hits_without_an_entities_field() -> None:
    class _PoolHit:  # the pool's pydantic hit has rank_score but no entities
        def __init__(self, key: str, score: float) -> None:
            self.memory_key, self.rank_score, self.distance = key, score, 1 - score

    hits = [
        _PoolHit("top", 0.50),
        _PoolHit("agree", 0.48),
        _PoolHit("agree2", 0.30),
        _PoolHit("x", 0.2),
    ]
    by_key = {
        "top": ["solo"],
        "agree": ["adoption", "mentor"],
        "agree2": ["adoption", "mentor"],
        "x": ["adoption"],
    }
    work = apply_mention_boost(hits, weight=0.1, cap=0.08, ubiquity=0.9, entities_by_key=by_key)
    assert work["mention_boost_applied"] == 3 and hits[0].memory_key == "agree"
    assert not hasattr(hits[0], "score_components")  # nothing is written onto a hit type without it


def test_request_field_defaults_off() -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.knobs import _mention_boost_weight

    assert MemorySearchInput.model_fields["mention_boost"].default is None
    inp = MemorySearchInput(org="o", namespace="n", agent="a", query="q")
    assert _mention_boost_weight(inp) == 0.0
    assert _mention_boost_weight(inp.model_copy(update={"mention_boost": 0.2})) == 0.2
