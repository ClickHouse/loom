"""bodies + floor_mode: a hit is a handle unless the caller
asks for the body, and the similarity floor can be a bound, not a label."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.api.models_search import _MemorySearchHit
from server.memory.app.search.evidence import _apply_min_similarity_floor
from server.memory.app.search.knobs import _floor_mode
from server.memory.app.search.payload import _apply_bodies


def _hit(key: str, *, distance: float, excerpt: str = "") -> _MemorySearchHit:
    return _MemorySearchHit(
        memory_key=key,
        memory_type="semantic",
        content_excerpt=excerpt or f"content for {key}",
        distance=distance,
        similarity=1.0 - distance,
        rank_score=1.0 - distance,
        score_components={},
    )


# --------------------------------------------------------------------------
# floor_mode
# --------------------------------------------------------------------------


def test_readmit_is_unchanged_default_behaviour():
    hits = [_hit("a", distance=0.6), _hit("b", distance=0.75)]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2, mode="readmit")
    assert [h.memory_key for h in kept] == ["a", "b"]
    assert kept[1].content_excerpt == "content for b"
    assert kept[1].score_components["below_floor"] == 1.0


def test_ids_only_keeps_the_key_and_withholds_the_body():
    hits = [_hit("a", distance=0.6), _hit("b", distance=0.75)]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2, mode="ids_only")
    assert [h.memory_key for h in kept] == ["a", "b"]
    assert kept[0].content_excerpt == "content for a"
    assert kept[1].content_excerpt == ""
    assert kept[1].score_components["below_floor"] == 1.0


def test_drop_removes_sub_floor_rows_and_ignores_the_small_pool_exemption():
    hits = [_hit("a", distance=0.6), _hit("b", distance=0.75), _hit("c", distance=0.8)]
    # pool (3) < top_k (10): readmit exempts, drop does not.
    assert _apply_min_similarity_floor(hits, 0.30, top_k=10, mode="readmit") == hits
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=10, mode="drop")
    assert [h.memory_key for h in kept] == ["a"]


def test_unknown_mode_falls_back_to_readmit():
    hits = [_hit("a", distance=0.6), _hit("b", distance=0.75)]
    kept = _apply_min_similarity_floor(hits, 0.30, top_k=2, mode="whatever")
    assert [h.memory_key for h in kept] == ["a", "b"]
    assert kept[1].content_excerpt == "content for b"


def test_floor_mode_request_overrides_deployment_default(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_floor_mode", "drop", raising=False)
    assert _floor_mode(SimpleNamespace(floor_mode=None)) == "drop"
    assert _floor_mode(SimpleNamespace(floor_mode="ids_only")) == "ids_only"
    monkeypatch.setattr(settings, "memory_search_floor_mode", "bogus", raising=False)
    assert _floor_mode(SimpleNamespace(floor_mode=None)) == "readmit"


# --------------------------------------------------------------------------
# bodies
# --------------------------------------------------------------------------


def test_bodies_full_is_a_no_op():
    hits = [_hit("a", distance=0.5, excerpt="x" * 500)]
    _apply_bodies(hits, SimpleNamespace(bodies="full"))
    assert len(hits[0].content_excerpt) == 500


def test_bodies_excerpt_clips_to_the_setting(monkeypatch):
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_excerpt_chars", 50, raising=False)
    hits = [_hit("a", distance=0.5, excerpt="x" * 500), _hit("b", distance=0.5, excerpt="short")]
    _apply_bodies(hits, SimpleNamespace(bodies="excerpt"))
    assert len(hits[0].content_excerpt) == 50 and hits[0].content_excerpt.endswith("…")
    assert hits[1].content_excerpt == "short"
    assert hits[0].memory_key == "a" and hits[0].similarity == 0.5


def test_an_excerpt_serves_a_decision_or_constraint_as_stored(monkeypatch):
    from server.memory.domain.decision_statement import memory_value
    from server.memory.domain.excerpt import build_excerpt
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_search_excerpt_chars", 50)
    statement = memory_value(
        {
            "scenario": "how the final merge reads spilled files",
            "outcome": "merge 64 files at a time",
            "reasoning": "the final merge holds one block of every file",
            "alternatives": ["read every file at once — rejected: memory grows with files"],
        }
    )
    # The body a decision hit carries is the stored excerpt the write built.
    stored = build_excerpt(statement, memory_key="decision:fan-in")

    def hit(key: str, *, memory_type: str = "semantic", **attrs: object) -> _MemorySearchHit:
        h = _hit(key, distance=0.5, excerpt=stored)
        h.memory_type = memory_type
        for name, value in attrs.items():
            setattr(h, name, value)
        return h

    typed = hit("d", memory_type="decision")
    row_tagged = hit("t", _row_tags=("decision",))
    constraint = hit("c", _row_tags=("constraint",))
    plain = hit("p")
    _apply_bodies([typed, row_tagged, constraint, plain], SimpleNamespace(bodies="excerpt"))
    assert "rejected: memory grows with files" in stored
    for whole in (typed, row_tagged, constraint):
        assert whole.content_excerpt == stored
    assert len(plain.content_excerpt) == 50 and plain.content_excerpt.endswith("…")


def test_none_still_blanks_a_decision():
    decision = _hit("d", distance=0.5, excerpt="Decided x because y.")
    decision.memory_type = "decision"
    _apply_bodies([decision], SimpleNamespace(bodies="none"))
    assert decision.content_excerpt == ""


def test_bodies_none_blanks_every_body():
    hits = [_hit("a", distance=0.5), _hit("b", distance=0.6)]
    _apply_bodies(hits, SimpleNamespace(bodies="none"))
    assert [h.content_excerpt for h in hits] == ["", ""]
    assert [h.memory_key for h in hits] == ["a", "b"]


def test_search_input_carries_both_knobs_with_wire_safe_defaults():
    from server.memory.api.models_search import MemorySearchInput

    s = MemorySearchInput(org="o", namespace="n", query="q")
    assert s.bodies == "full" and s.floor_mode is None
    s2 = MemorySearchInput(org="o", namespace="n", query="q", bodies="none", floor_mode="drop")
    assert (s2.bodies, s2.floor_mode) == ("none", "drop")


def test_mcp_search_tool_defaults_to_excerpt_bodies():
    import inspect

    from server.transport import mcp_defaults, mcp_tools

    assert 'arguments.get("bodies") or "excerpt"' in inspect.getsource(mcp_defaults)
    assert '"bodies": {' in inspect.getsource(mcp_tools)


# --------------------------------------------------------------------------
# bodies="unit": whole stored values by rank until the budget is spent
# --------------------------------------------------------------------------


def test_unit_bodies_fill_by_rank_then_escalate(monkeypatch):
    import asyncio

    from server.memory import store as memory_repo
    from server.memory.app.search import payload as ms

    long_a = "A" * 4000  # 1000 tokens
    long_b = "B" * 8000  # 2000 tokens

    async def fake_get_many(identity, keys, **kw):
        assert (
            identity.namespace == "n" and identity.agent == "*" and identity.user_id == "*"
        )  # No user named -> every user
        return [
            SimpleNamespace(memory_key="a", value=long_a, sensitivity=""),
            SimpleNamespace(memory_key="b", value=long_b, sensitivity=""),
            SimpleNamespace(memory_key="c", value={"x": 1}, sensitivity=""),
        ]

    monkeypatch.setattr(memory_repo, "get_many_by_keys", fake_get_many)
    hits = [
        _hit("a", distance=0.1, excerpt="clip a"),
        _hit("b", distance=0.2, excerpt="clip b"),
        _hit("c", distance=0.3, excerpt="clip c"),
        _hit("d", distance=0.4, excerpt="clip d"),
    ]
    inp = SimpleNamespace(
        bodies="unit",
        body_budget_tokens=2500,
        org="o",
        namespace="n",
        agent="*",
        user_id=None,
        session_id=None,
    )
    returned, elided, tokens = asyncio.run(ms._expand_unit_bodies(hits, inp))
    assert hits[0].content_excerpt == long_a and hits[0].body_truncated is False
    assert hits[1].content_excerpt == "clip b" and hits[1].body_truncated is True
    assert hits[1].next == 'memory.get(memory_key="b")', (
        "over budget: clip stays, one-call escalation"
    )
    assert hits[2].content_excerpt == '{"x": 1}' and hits[2].body_truncated is False, (
        "dict values serialise"
    )
    assert hits[3].content_excerpt == "clip d" and hits[3].body_truncated is False, (
        "unknown key: untouched"
    )
    assert (returned, elided) == (2, 1) and tokens == 1000 + 2
    # _apply_bodies must not clip unit bodies afterwards
    _apply_bodies(hits, inp)
    assert hits[0].content_excerpt == long_a


def test_unit_bodies_is_a_no_op_for_other_modes_and_fails_open(monkeypatch):
    import asyncio

    from server.memory import store as memory_repo
    from server.memory.app.search import payload as ms

    hits = [_hit("a", distance=0.1, excerpt="clip a")]
    assert asyncio.run(ms._expand_unit_bodies(hits, SimpleNamespace(bodies="full"))) == (0, 0, 0)
    assert hits[0].content_excerpt == "clip a"

    async def boom(identity, keys, **kw):
        raise RuntimeError("ch down")

    monkeypatch.setattr(memory_repo, "get_many_by_keys", boom)
    inp = SimpleNamespace(
        bodies="unit",
        body_budget_tokens=4000,
        org="o",
        namespace="n",
        agent="*",
        user_id=None,
        session_id=None,
    )
    assert asyncio.run(ms._expand_unit_bodies(hits, inp)) == (0, 0, 0)
    assert hits[0].content_excerpt == "clip a" and hits[0].body_truncated is False


# --------------------------------------------------------------------------
# recent_first tier + min_body_chars
# --------------------------------------------------------------------------


def test_recent_first_places_the_runs_latest_memories_ahead_of_similarity(monkeypatch):
    import asyncio

    from server.memory import store as memory_repo
    from server.memory.app.search import tiers as ms

    rows = [
        SimpleNamespace(
            memory_key="s3",
            memory_type="semantic",
            value="third step",
            run_id="r1",
            created_at="2026-09-12T03:00:00",
        ),
        SimpleNamespace(
            memory_key="x9",
            memory_type="semantic",
            value="other run",
            run_id="r2",
            created_at="2026-09-12T02:30:00",
        ),
        SimpleNamespace(
            memory_key="s2",
            memory_type="semantic",
            value="second step",
            run_id="r1",
            created_at="2026-09-12T02:00:00",
        ),
    ]

    async def fake_list(identity, **kw):
        assert kw["order_by"] == "created_at_desc" and identity.agent == "*"
        return rows

    monkeypatch.setattr(memory_repo, "list_by_namespace", fake_list)
    hits = [_hit("s2", distance=0.2), _hit("a", distance=0.3), _hit("b", distance=0.4)]
    inp = SimpleNamespace(
        recent_first=2,
        top_k=3,
        run_id="r1",
        org="o",
        namespace="n",
        agent="*",
        user_id=None,
        session_id=None,
    )
    out, _ = asyncio.run(ms._prepend_recent(hits, inp, {}))
    assert [h.memory_key for h in out] == ["s3", "s2", "a"], (
        "run's latest first, dedup, other run skipped, k kept"
    )
    assert out[0].score_components == {"recent_first": 1.0} and out[0].rank_score == 0.0
    assert out[1].score_components == {}, "an existing similarity hit keeps its own scores"
    assert asyncio.run(ms._prepend_recent(hits, SimpleNamespace(recent_first=0), {}))[0] is hits


def test_min_body_chars_drops_near_empty_bodies_and_is_off_by_default():
    from server.memory.app.search.tiers import _apply_min_body_chars

    hits = [
        _hit("a", distance=0.1, excerpt="Initial result: Empty"),
        _hit("b", distance=0.2, excerpt="x" * 300),
    ]
    assert _apply_min_body_chars(hits, SimpleNamespace(min_body_chars=0)) == hits
    kept = _apply_min_body_chars(hits, SimpleNamespace(min_body_chars=60))
    assert [h.memory_key for h in kept] == ["b"]


def test_a_tier_hit_carries_its_row_tags_so_a_constraint_keeps_its_excerpt(monkeypatch):
    import asyncio

    from server.memory import store as memory_repo
    from server.memory.app.search import tiers as ms
    from server.settings import settings

    rows = [
        SimpleNamespace(
            memory_key="c",
            memory_type="semantic",
            value="Never hold a lock across a read. " * 10,
            tags=["constraint"],
            created_at="2026-09-12T02:00:00",
        )
    ]

    async def fake_list(identity, **kw):
        return rows

    monkeypatch.setattr(memory_repo, "list_by_namespace", fake_list)
    monkeypatch.setattr(settings, "memory_search_excerpt_chars", 50)
    inp = SimpleNamespace(
        recent_first=1,
        top_k=1,
        run_id="",
        org="o",
        namespace="n",
        agent="*",
        user_id=None,
        session_id=None,
        bodies="excerpt",
    )
    (hit,), _stale = asyncio.run(ms._prepend_recent([], inp, {}))
    _apply_bodies([hit], inp)
    assert hit.content_excerpt == rows[0].value
