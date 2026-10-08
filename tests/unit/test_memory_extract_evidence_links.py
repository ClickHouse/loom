from server.memory.app.chunk_projection import (
    message_index_to_evidence_id,
    message_line_evidence_refs,
)
from server.memory.app.extract.handler import (
    _evidence_ids_by_memory_key,
    _evidence_refs_by_memory_key,
)


def test_message_index_to_evidence_id_skips_empty_messages() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "   "},
        {"role": "user", "content": "third"},
    ]

    assert message_index_to_evidence_id(msgs, ["e0", "e2"]) == {
        0: "e0",
        2: "e2",
    }


def test_evidence_ids_by_memory_key_uses_source_indices() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
        {"role": "user", "content": "third"},
    ]
    facts = [
        {"key": "a", "source_message_indices": [0, 2, 0]},
        {"key": "b", "source_message_indices": ["1"]},
        {"key": "c", "source_message_indices": [99]},
    ]

    assert _evidence_ids_by_memory_key(msgs, facts, ["e0", "e1", "e2"]) == {
        "a": ["e0", "e2"],
        "b": ["e1"],
    }


def test_evidence_refs_by_memory_key_adds_best_effort_spans() -> None:
    msgs = [
        {
            "role": "user",
            "content": "I use the dark theme in the dashboard. Other text.",
        },
    ]
    facts = [
        {
            "key": "profile_theme",
            "value": "dark theme",
            "source_message_indices": [0],
        }
    ]

    assert _evidence_refs_by_memory_key(msgs, facts, ["e0"]) == {"profile_theme": [("e0", 10, 20)]}


def test_evidence_refs_falls_back_to_relevant_sentence() -> None:
    msgs = [
        {
            "role": "user",
            "content": "Unrelated sentence. My dashboard is configured for dark mode.",
        },
    ]
    facts = [
        {
            "key": "dashboard_mode",
            "value": "prefers a dark dashboard appearance",
            "source_message_indices": [0],
        }
    ]

    refs = _evidence_refs_by_memory_key(msgs, facts, ["e0"])
    evidence_id, start, end = refs["dashboard_mode"][0]
    assert evidence_id == "e0"
    assert msgs[0]["content"][start:end] == (" My dashboard is configured for dark mode.")


def test_message_line_evidence_refs_preserves_empty_message_line_offsets() -> None:
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": ""},
        {"role": "user", "content": "second\nthird"},
    ]

    assert message_line_evidence_refs(msgs, ["e0", "e2"]) == [
        (1, 1, "e0"),
        (3, 4, "e2"),
    ]


# ---- lexical fallback: a fact the extractor did not cite still links to the
# message that supports it, never to the whole window -----------------------

from server.memory.app.extract.evidence import (  # noqa: E402
    SCOPE_CITED,
    SCOPE_LEXICAL,
    SCOPE_NONE,
    _lexical_support_scores,
    _scoped_evidence_refs,
)

_MSGS = [
    {"role": "user", "content": "Hey, quick one before the standup."},
    {"role": "assistant", "content": "Sure, go ahead."},
    {
        "role": "user",
        "content": "My dog Bowie is seven years old and weighs 32 pounds.",
    },
    {"role": "assistant", "content": "Bowie sounds like a healthy dog."},
    {"role": "user", "content": "Also I switched my IDE to Zed last week."},
]
_EVIDENCE = ["e0", "e1", "e2", "e3", "e4"]


def test_lexical_support_scores_ranks_the_supporting_turn_first() -> None:
    fact = {
        "key": "user_dog_weight",
        "value": "Bowie weighs 32 pounds.",
        "entities": ["bowie"],
    }
    scored = _lexical_support_scores(_MSGS, fact)
    assert scored[0][1] == 2
    # The greeting and "go ahead" share no 4+ char token with the fact.
    assert all(idx not in (0, 1) for _score, idx in scored)


def test_scoped_refs_prefer_cited_indices_over_the_lexical_match() -> None:
    facts = [
        {
            "key": "user_dog_weight",
            "value": "Bowie weighs 32 pounds.",
            "entities": ["bowie"],
            "source_message_indices": [3],  # the extractor cited the reply
        }
    ]
    refs, scope = _scoped_evidence_refs(_MSGS, facts, _EVIDENCE)
    assert scope == {"user_dog_weight": SCOPE_CITED}
    assert [ref[0] for ref in refs["user_dog_weight"]] == ["e3"]


def test_scoped_refs_fall_back_to_the_best_message_not_the_window() -> None:
    facts = [
        {"key": "user_dog_weight", "value": "Bowie weighs 32 pounds.", "entities": ["bowie"]},
        {"key": "user_ide", "value": "User's IDE is Zed.", "entities": ["zed"]},
    ]
    refs, scope = _scoped_evidence_refs(_MSGS, facts, _EVIDENCE)
    assert scope == {"user_dog_weight": SCOPE_LEXICAL, "user_ide": SCOPE_LEXICAL}
    dog_ids = [ref[0] for ref in refs["user_dog_weight"]]
    assert dog_ids[0] == "e2"
    assert len(dog_ids) <= 2  # never the five-message window
    ((ide_id, start, end),) = refs["user_ide"]
    assert ide_id == "e4"
    assert _MSGS[4]["content"][start:end].lower() == "zed"  # entity found verbatim


def test_scoped_refs_label_an_unsupported_fact_none() -> None:
    facts = [{"key": "user_mood", "value": "Cheerful.", "entities": []}]
    refs, scope = _scoped_evidence_refs(_MSGS, facts, _EVIDENCE)
    assert refs == {}
    assert scope == {"user_mood": SCOPE_NONE}


def test_scoped_refs_skip_facts_without_a_key() -> None:
    facts = [{"value": "Bowie weighs 32 pounds."}]
    refs, scope = _scoped_evidence_refs(_MSGS, facts, _EVIDENCE)
    assert refs == {} and scope == {}
