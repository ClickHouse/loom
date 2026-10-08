"""Speaker attribution on memory.set_from_messages.

Two humans in one transcript both arrive as `user` turns; a fact that keeps
the subject and drops who said it answers "according to X" from Y's
statement. A message may carry `name`: the extractor sees `[user Caroline]`, and a fact cited
to one speaker's turns gets `speaker:<name>` and the name as an entity.
CH-free."""

from __future__ import annotations

from server.memory.api.models_bulk import MemorySetFromMessagesInput, _Message
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.handler import _message_dict
from server.memory.app.extract.speakers import apply_message_speakers
from server.memory.app.extract.turns import _format_messages


def test_name_rides_the_message_dict_only_when_set() -> None:
    assert _message_dict(_Message(role="user", content="hi", name="Caroline")) == {
        "role": "user",
        "content": "hi",
        "name": "Caroline",
    }
    assert _message_dict(_Message(role="user", content="hi")) == {"role": "user", "content": "hi"}
    inp = MemorySetFromMessagesInput(
        org="o",
        namespace="n",
        agent="a",
        messages=[{"role": "user", "content": "x", "name": "  Melanie "}],
    )
    assert inp.messages[0].name == "  Melanie "  # stripped at render, not at parse


def test_format_messages_shows_the_speaker_after_the_role() -> None:
    msgs = [
        {"role": "user", "content": "I play clarinet.", "name": "Melanie"},
        {"role": "user", "content": "Nice!", "name": "Caroline", "timestamp": "2023-05-08T13:56"},
        {"role": "assistant", "content": "Noted."},
    ]
    lines = _format_messages(msgs).splitlines()
    assert lines[0] == "[user Melanie] I play clarinet."
    assert lines[1] == "[user Caroline @ 2023-05-08 13:56] Nice!"
    assert lines[2] == "[assistant] Noted."
    assert (
        _format_messages(msgs, include_indices=True).splitlines()[0]
        == "[0:user Melanie] I play clarinet."
    )


_MSGS = [
    {"role": "user", "content": "Max is awesome at catching frisbees mid-air.", "name": "James"},
    {"role": "user", "content": "Ha, he never misses!", "name": "John"},
    {"role": "user", "content": "I started painting classes.", "name": "Evan"},
    {"role": "user", "content": "no name here"},
]


def test_single_speaker_facts_get_a_tag_and_an_entity() -> None:
    facts = [
        ExtractedFact(
            memory_key="max",
            value="Max is awesome at catching frisbees mid-air.",
            entities=["max"],
            source_message_indices=[0],
        ),
        ExtractedFact(
            memory_key="evan",
            value="Evan started painting classes.",
            entities=["evan"],
            tags=["hobby"],
            source_message_indices=[2],
        ),
        # cited to two speakers: an exchange, left alone
        ExtractedFact(
            memory_key="both", value="James and John admire Max.", source_message_indices=[0, 1]
        ),
        # cited to an unnamed turn
        ExtractedFact(memory_key="anon", value="something", source_message_indices=[3]),
        # uncited
        ExtractedFact(memory_key="none", value="uncited"),
    ]
    out = {f.memory_key: f for f in apply_message_speakers(facts, _MSGS)}
    assert out["max"].tags == ["speaker:james"]
    assert out["max"].entities == ["max", "james"]
    assert out["evan"].tags == ["hobby", "speaker:evan"]
    assert out["evan"].entities == ["evan"]  # already present, not duplicated
    assert out["both"].tags == [] and out["both"].entities == []
    assert out["anon"].tags == []
    assert out["none"].tags == []
    # values untouched by default
    assert out["max"].value == "Max is awesome at catching frisbees mid-air."


def test_no_names_is_a_no_op_returning_the_same_list() -> None:
    facts = [ExtractedFact(memory_key="a", value="x", source_message_indices=[0])]
    msgs = [{"role": "user", "content": "x"}]
    assert apply_message_speakers(facts, msgs) is facts


def test_an_unnamed_assistant_turn_adds_no_speaker_tag() -> None:
    msgs = [{"role": "assistant", "content": "Day 1: Al Seef."}]
    out = apply_message_speakers(
        [ExtractedFact(memory_key="k", value="v", source_message_indices=[0])], msgs
    )
    assert "speaker:assistant" not in out[0].tags


def test_a_named_assistant_turn_carries_its_speaker() -> None:
    msgs = [{"role": "assistant", "content": "x", "name": "Caroline"}]
    out = apply_message_speakers(
        [ExtractedFact(memory_key="k", value="v", source_message_indices=[0])], msgs
    )
    assert "speaker:caroline" in out[0].tags and "speaker:assistant" not in out[0].tags
