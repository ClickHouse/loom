"""Digest-anchored expansion and the recent-turns block:
the pure parts — which sessions descend, the switch precedence, how the
caller's turns render, and the request models."""

from __future__ import annotations

from types import SimpleNamespace

from server.memory.api.models_ask import MemoryAskInput
from server.memory.api.models_search import MemorySearchInput
from server.memory.app.ask.context_blocks import recent_turns_block
from server.memory.app.search.digest_descent import descent_targets, digest_descent_enabled


def _hit(key: str, source: str, cos: float, ns: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        memory_key=key,
        source=source,
        namespace=ns,
        rank_score=cos,
        score_components={"summary_cosine": cos} if source == "summary" else {"semantic": cos},
    )


def test_descent_targets_are_the_top_m_digest_sessions() -> None:
    hits = [
        _hit("fact_a", "fact", 0.9),
        _hit("summary:s-low", "summary", 0.40),
        _hit("summary:s-high", "summary", 0.80, ns="other"),
        _hit("summary:s-mid", "summary", 0.60),
        _hit("summary:s-high", "summary", 0.79),  # same session, other agent's row
        _hit("summary:-", "summary", 0.99),  # the identity sentinel is not a session
        _hit("entity_summary:x", "entity_summary", 0.95),
    ]
    # a session is (namespace, id): the same id under two namespaces is two
    assert descent_targets(hits, top_m=2, default_namespace="ns") == [
        ("other", "s-high"),
        ("ns", "s-high"),
    ]
    assert descent_targets(hits, top_m=10, default_namespace="ns") == [
        ("other", "s-high"),
        ("ns", "s-high"),
        ("ns", "s-mid"),
        ("ns", "s-low"),
    ]
    assert descent_targets([_hit("fact_a", "fact", 0.9)], top_m=3, default_namespace="ns") == []
    assert descent_targets(hits, top_m=0, default_namespace="ns") == []


def test_descent_is_on_unless_the_request_turns_it_off() -> None:
    assert digest_descent_enabled(SimpleNamespace(digest_descent=None)) is True
    assert digest_descent_enabled(SimpleNamespace(digest_descent=True)) is True
    assert digest_descent_enabled(SimpleNamespace(digest_descent=False)) is False


def test_recent_turns_block_renders_bounded_newest_last() -> None:
    assert recent_turns_block([]) == ""
    assert recent_turns_block([{"role": "user", "content": "   "}]) == ""
    block = recent_turns_block(
        [
            {
                "role": "user",
                "name": "Caroline",
                "content": "Did she like the plan?",
                "timestamp": "2026-09-12T10:00:00",
            },
            {"role": "assistant", "content": "She said it needed a smaller first step."},
        ]
    )
    lines = block.split("\n")
    assert lines[0].startswith("RECENT TURNS")
    assert lines[1] == "[user Caroline @ 2026-09-12T10:00:00] Did she like the plan?"
    assert lines[2] == "[assistant] She said it needed a smaller first step."
    # one long turn is clipped; many turns drop the oldest first
    long = recent_turns_block([{"role": "user", "content": "x" * 5000}])
    assert len(long) < 1700 and long.endswith("…[truncated]")
    many = recent_turns_block(
        [{"role": "user", "content": f"turn {i} " + "y" * 900} for i in range(60)]
    )
    assert "turn 59 " in many and "turn 0 " not in many
    assert len(many) <= 12000 + 80


def test_request_models_carry_the_new_fields() -> None:
    ask = MemoryAskInput(
        org="o",
        namespace="n",
        agent="*",
        query="and what did she say about that?",
        recent_messages=[{"role": "user", "content": "We talked about the plan."}],
        digest_descent=True,
    )
    assert ask.recent_messages[0].content == "We talked about the plan."
    assert ask.digest_descent is True
    assert MemoryAskInput(org="o", namespace="n", agent="*", query="q").recent_messages == []
    assert MemorySearchInput(org="o", namespace="n", agent="*", query="q").digest_descent is None


def test_presupposition_rule_switch_and_field() -> None:
    # The request field is None by default and the note exists.
    from server.memory.app.ask.prompts import _PRESUPPOSITION_NOTE

    ask = MemoryAskInput(org="o", namespace="n", agent="*", query="q")
    assert ask.presupposition_rule is None
    assert (
        MemoryAskInput(
            org="o", namespace="n", agent="*", query="q", presupposition_rule=False
        ).presupposition_rule
        is False
    )
    assert "PRESUPPOSES" in _PRESUPPOSITION_NOTE and "FIRST sentence" in _PRESUPPOSITION_NOTE
    assert "do not lead with a value" in _PRESUPPOSITION_NOTE


def test_set_questions_are_detected_and_single_facts_are_not() -> None:
    from server.memory.domain.synth_policy import is_set_question

    for q in (
        "What books has Melanie read?",
        "Which places or events have John and James planned to meet at?",
        "Who has written notes of gratitude to Maria?",
        "What desserts has Maria made?",
        "What martial arts has John done?",
        "What kinds of activities has Caroline participated in?",
        "What are the symbols that are important to Caroline?",
        "Which of the tests failed last night?",
    ):
        assert is_set_question(q), q
    for q in (
        "Where did Caroline move from 4 years ago?",
        "What is Melanie's dog's name?",
        "When did Melanie run a charity race?",
        "How old is Xu Jia?",
        "Did Caroline make the black and white bowl?",
    ):
        assert not is_set_question(q), q
    assert MemoryAskInput(org="o", namespace="n", agent="*", query="q").set_window is None
