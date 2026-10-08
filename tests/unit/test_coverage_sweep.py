"""The turns the first extraction pass cited nowhere get a
second reading on their own; the sweep's facts must cite an uncovered turn,
never repeat a first-pass key, and the pass fails open."""

from __future__ import annotations

import json

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.coverage import (
    uncovered_turn_indices,
)
from server.memory.app.extract.sweep import _coverage_sweep
from server.memory.app.extract.turns import _format_indexed_turns


class _Client:
    provider = "stub"
    model = "stub-1"

    def __init__(self, replies: list[str], *, boom: bool = False) -> None:
        self.replies = list(replies)
        self.boom = boom
        self.calls: list[dict[str, str]] = []

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        if self.boom:
            raise RuntimeError("provider down")
        self.calls.append({"system": system, "user": user})
        text = self.replies.pop(0) if self.replies else "{}"
        return LLMResult(
            text=text, tokens_in=10, tokens_out=5, provider=self.provider, model=self.model
        )


MSGS = [
    {
        "role": "user",
        "name": "Caroline",
        "content": "I finally passed the adoption agency interviews last Friday and I am thrilled about it.",
    },
    {
        "role": "user",
        "name": "Melanie",
        "content": "Wow, Caroline! That's great! I just signed up for a pottery class yesterday.",
    },
    {"role": "user", "name": "Caroline", "content": "Thanks!"},
    {
        "role": "user",
        "name": "Melanie",
        "content": "Luna and Oliver are so sweet and playful. Just got some new shoes, too!",
    },
]
FIRST = [
    ExtractedFact(
        "caroline_adoption_interviews",
        "Caroline passed the adoption agency interviews.",
        source_message_indices=[0],
    )
]


def test_uncovered_turns_skip_cited_and_short_turns() -> None:
    assert uncovered_turn_indices(MSGS, FIRST, min_chars=40) == [1, 3]
    # A fact citing turn 3 covers it.
    more = FIRST + [
        ExtractedFact(
            "melanie_pets", "Melanie has pets Luna and Oliver.", source_message_indices=[3]
        )
    ]
    assert uncovered_turn_indices(MSGS, more, min_chars=40) == [1]
    # No facts at all: every long turn is uncovered.
    assert uncovered_turn_indices(MSGS, [], min_chars=40) == [0, 1, 3]


def test_indexed_turns_keep_original_indices_and_speakers() -> None:
    text = _format_indexed_turns(MSGS, [1, 3])
    assert text.startswith("[1:user Melanie] Wow, Caroline!")
    assert "\n[3:user Melanie] Luna and Oliver" in text
    assert "[0:" not in text and "[2:" not in text


@pytest.mark.asyncio
async def test_sweep_adds_only_facts_that_cite_an_uncovered_turn(monkeypatch) -> None:
    reply = json.dumps(
        {
            "facts": [
                {
                    "key": "melanie_pottery_class",
                    "value": "Melanie signed up for a pottery class yesterday.",
                    "source_message_indices": [1],
                },
                {
                    "key": "melanie_new_shoes",
                    "value": "Melanie got new shoes.",
                    "source_message_indices": [3],
                },
                {
                    "key": "caroline_adoption_interviews",
                    "value": "Caroline passed the interviews.",
                    "source_message_indices": [0],
                },  # cites a covered turn: dropped
                {
                    "key": "melanie_mood",
                    "value": "Melanie is happy.",
                    "source_message_indices": [],
                },  # uncited: dropped
            ]
        }
    )
    client = _Client([reply])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    out, result, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date="2023-07-03",
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert added == 2 and result is not None
    assert [f.memory_key for f in out] == [
        "caroline_adoption_interviews",
        "melanie_pottery_class",
        "melanie_new_shoes",
    ]
    sent = client.calls[0]
    assert "SECOND READING" in sent["system"]
    assert "observation_date: 2023-07-03" in sent["user"]
    assert (
        "already_extracted" in sent["user"]
        and "Caroline passed the adoption agency interviews." in sent["user"]
    )
    # Only the uncovered turns are offered for extraction, with their real indices.
    tail = sent["user"].split("uncovered_turns", 1)[1]
    assert "[1:user Melanie]" in tail and "[3:user Melanie]" in tail and "[0:" not in tail


@pytest.mark.asyncio
async def test_sweep_same_key_as_first_pass_gets_a_turn_distinct_key(monkeypatch) -> None:
    first = [
        ExtractedFact(
            "melanie_camping_trip", "Melanie camped in the mountains.", source_message_indices=[0]
        )
    ]
    msgs = [
        {
            "role": "user",
            "content": "We took the family camping in the mountains last week, it was lovely.",
        },
        {
            "role": "user",
            "content": "Here's a pic of my family camping at the beach. We love it, it brings us closer!",
        },
    ]
    reply = json.dumps(
        {
            "facts": [
                {
                    "key": "melanie_camping_trip",
                    "value": "Melanie's family camped at the beach.",
                    "source_message_indices": [1],
                },
            ]
        }
    )
    client = _Client([reply])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    out, _r, added = await _coverage_sweep(
        msgs,
        first,
        system_prompt="SYS",
        observation_date=None,
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert added == 1
    assert out[1].memory_key == "melanie_camping_trip_t1"


@pytest.mark.asyncio
async def test_sweep_is_silent_when_everything_is_cited_and_fails_open(monkeypatch) -> None:
    covered = [ExtractedFact("k", "v", source_message_indices=[0, 1, 3])]
    client = _Client([])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    out, result, added = await _coverage_sweep(
        MSGS,
        covered,
        system_prompt="SYS",
        observation_date=None,
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert (out, result, added) == (covered, None, 0) and client.calls == []
    boom = _Client([], boom=True)
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: boom)
    out, result, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date=None,
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert out == FIRST and result is None and added == 0
    garbage = _Client(["not json at all"])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: garbage)
    out, result, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date=None,
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert out == FIRST and added == 0


def test_noise_filter_drops_exchange_facts_and_keeps_life_facts() -> None:
    from server.memory.app.extract.sweep import sweep_fact_is_noise

    noise = [
        ExtractedFact(
            "melanie_inquires_about_event",
            "Melanie inquired about the poetry reading Caroline attended.",
        ),
        ExtractedFact(
            "caroline_running_inquiry", "Caroline inquires if Melanie's new shoes are for running."
        ),
        ExtractedFact(
            "melanie_congratulations", "Melanie congratulated Caroline on the adoption step."
        ),
        ExtractedFact(
            "caroline_family_rock",
            "Caroline expressed that it is sweet to see Melanie's love for her family.",
        ),
        ExtractedFact(
            "caroline_importance_of_joy",
            "Caroline emphasizes the importance of surrounding ourselves with joy.",
        ),
        ExtractedFact(
            "user_melanie_greeting", "Melanie greeted Caroline and asked if there's anything new."
        ),
    ]
    assert all(sweep_fact_is_noise(f) for f in noise)
    keep = [
        ExtractedFact(
            "user_caroline_picnic", "Caroline had a picnic last week with her support network."
        ),
        ExtractedFact(
            "melanie_family_love_figurines",
            "Melanie bought figurines that remind her of family love.",
        ),
        ExtractedFact(
            "melanie_instrument_playing",
            "Melanie plays the clarinet, which she started when she was young.",
        ),
        ExtractedFact("melanie_swimming", "Melanie is off to go swimming."),
    ]
    assert not any(sweep_fact_is_noise(f) for f in keep)


@pytest.mark.asyncio
async def test_sweep_tags_its_facts_and_filters_noise(monkeypatch) -> None:
    reply = json.dumps(
        {
            "facts": [
                {
                    "key": "melanie_pottery_class",
                    "value": "Melanie signed up for a pottery class yesterday.",
                    "source_message_indices": [1],
                },
                {
                    "key": "melanie_reaction",
                    "value": "Melanie congratulated Caroline on passing the interviews.",
                    "source_message_indices": [1],
                },
            ]
        }
    )
    client = _Client([reply])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    out, _r, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date="2023-07-03",
        last_messages=[{"role": "user", "content": "old"}],
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert (
        added == 1
        and out[-1].memory_key == "melanie_pottery_class"
        and "coverage_sweep" in out[-1].tags
    )
    sent = client.calls[0]["user"]
    # Compact context: the predecessor of an uncovered turn, marked, and no prior-session turns.
    assert "(context) [0:user Caroline]" in sent and "(context) [2:user Caroline]" in sent
    assert "old" not in sent
    assert "spoken on this date" in sent


def test_pipeline_stores_sweep_facts_at_lower_confidence() -> None:
    from server.memory.app import pipeline as mp

    assert mp._SWEEP_CONFIDENCE < 1.0 and mp._SWEEP_CONFIDENCE > mp._UNCERTAIN_CONFIDENCE


FACT_LIST = [
    {
        "role": "user",
        "content": (
            "Here is a list of facts:\n0. Thomas Kyd was born in the city of London.\n"
            "1. The chairperson of Fatah is Mahmoud Abbas.\n2. Amy Winehouse died in the city of Camden Town.\n"
            "3. Bengaluru is located in the continent of Asia.\n4. The chairperson of Fatah is Moshe Kahlon."
        ),
    }
]
LIST_FIRST = [
    ExtractedFact(
        "thomas_kyd_birthplace", "Thomas Kyd was born in London.", source_message_indices=[0]
    ),
    ExtractedFact(
        "fatah_chairperson",
        "The chairperson of Fatah is Mahmoud Abbas.",
        source_message_indices=[0],
    ),
    ExtractedFact(
        "bengaluru_continent", "Bengaluru is located in Asia.", source_message_indices=[0]
    ),
]


def test_uncovered_sentences_inside_a_cited_statement_dense_turn() -> None:
    """One fact 'covers' a 35-statement turn at turn
    granularity; the statements no fact shares two content words with are
    what the second reading needs. The update (Fatah → Moshe Kahlon) shares
    'chairperson' and 'fatah' with the first-pass fact but not its object:
    flagged, so the store can take the newer value. 'Thomas Kyd was born in
    the city of London' is covered by 'Thomas Kyd was born in London'."""
    from server.memory.app.extract.coverage import uncovered_sentences

    got = uncovered_sentences(FACT_LIST, LIST_FIRST)
    assert list(got) == [0]
    assert got[0] == [
        "Amy Winehouse died in the city of Camden Town.",
        "The chairperson of Fatah is Moshe Kahlon.",
    ]
    # a dialogue turn with one long sentence is not split
    assert (
        uncovered_sentences(MSGS, [ExtractedFact("k", "v", source_message_indices=[0, 1, 3])]) == {}
    )


@pytest.mark.asyncio
async def test_sweep_reads_the_uncovered_sentences_of_a_cited_turn(monkeypatch) -> None:
    reply = json.dumps(
        {
            "facts": [
                {
                    "key": "amy_winehouse_death_place",
                    "value": "Amy Winehouse died in Camden Town.",
                    "source_message_indices": [0],
                },
            ]
        }
    )
    client = _Client([reply])
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    out, result, added = await _coverage_sweep(
        FACT_LIST,
        LIST_FIRST,
        system_prompt="SYS",
        observation_date=None,
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    assert added == 1 and out[-1].memory_key == "amy_winehouse_death_place"
    sent = client.calls[0]["user"]
    assert (
        "uncovered_sentences" in sent
        and "[0] Amy Winehouse died in the city of Camden Town." in sent
    )
    assert "uncovered_turns" not in sent  # the turn itself was cited


@pytest.mark.asyncio
async def test_request_level_coverage_sweep_override(monkeypatch) -> None:
    """`coverage_sweep` on the call turns the sweep on; None leaves it to
    the density gates."""
    from server.memory.app.extract import client as extract_client
    from server.memory.app.extract import extract

    calls: list[bool] = []

    async def fake_sweep(messages, facts, **kw):
        calls.append(True)
        return facts, None, 0

    monkeypatch.setattr(extract, "_coverage_sweep", fake_sweep)

    class _LLM:
        async def complete(self, system, user, max_tokens=1024, response_format=None):
            from server.llm.client import LLMResult

            return LLMResult(
                text=json.dumps(
                    {
                        "facts": [
                            {
                                "key": "k",
                                "value": "Caroline passed the interviews.",
                                "source_message_indices": [0],
                            }
                        ]
                    }
                ),
                tokens_in=1,
                tokens_out=1,
                provider="stub",
                model="stub",
            )

    monkeypatch.setattr(extract_client, "get_llm_client", lambda: _LLM())
    await extract.extract_from_messages(MSGS, judge=False, coverage_sweep=True)
    assert calls == [True]
    await extract.extract_from_messages(MSGS, judge=False, coverage_sweep=None)
    assert calls == [True]  # no override, no dense turn → no sweep


def test_uncovered_sentences_flag_a_one_word_update_by_its_salient_token() -> None:
    """'Thomas Arne was born in the city of Bengaluru' shares eight of nine
    content words with the first-pass London fact and would pass the
    three-quarters test as covered, losing the update. The covering fact must
    also carry the sentence's salient tokens (proper nouns past the first
    word, numbers)."""
    from server.memory.app.extract.coverage import _salient_tokens, uncovered_sentences

    assert _salient_tokens("Thomas Arne was born in the city of Bengaluru.") == {
        "arne",
        "bengaluru",
    }
    assert _salient_tokens("My personal best is 27:12 now.") == {"27", "12"}
    msgs = [
        {
            "role": "user",
            "content": (
                "Here are the facts. Thomas Arne was born in the city of London. "
                "The capital of Italy is Rome. Thomas Arne was born in the city of Bengaluru. "
                "Thomas Kyd was born in the city of London."
            ),
        }
    ]
    facts = [
        ExtractedFact(
            "thomas_arne_born_in",
            "Thomas Arne was born in the city of London.",
            source_message_indices=[0],
        ),
        ExtractedFact("italy_capital", "The capital of Italy is Rome.", source_message_indices=[0]),
        ExtractedFact(
            "thomas_kyd_born_in", "Thomas Kyd was born in London.", source_message_indices=[0]
        ),
    ]
    got = uncovered_sentences(msgs, facts)
    assert got == {0: ["Thomas Arne was born in the city of Bengaluru."]}


# ---------------------------------------------------------------------------
# The listed items are read in groups, and a
# sentence that cannot hold a fact is not listed.
# ---------------------------------------------------------------------------


def test_sweep_batches_cut_turns_then_sentences() -> None:
    from server.memory.app.extract.sweep import sweep_batches

    groups = sweep_batches([1, 3, 5], {2: ["a", "b"], 4: ["c"]}, 2)
    assert groups == [([1, 3], {}), ([5], {2: ["a"]}), ([], {2: ["b"], 4: ["c"]})]
    # 0 = one group.
    assert sweep_batches([1, 3], {2: ["a"]}, 0) == [([1, 3], {2: ["a"]})]
    assert sweep_batches([1, 3], {2: ["a"]}, 8) == [([1, 3], {2: ["a"]})]


@pytest.mark.parametrize(
    ("sentence", "listable"),
    [
        ("Melanie: Hi Caroline!", False),
        ("Thanks, Mel!", False),
        ("Guess what I did this week?", False),
        ("It was great.", False),
        ("[shared a photo: a photo of a cup with a dog face on it]", True),
        ("We got another cat named Bailey too.", True),
        ("Caroline: I used to go horseback riding with my dad when I was a kid.", True),
    ],
)
def test_sentence_is_listable(sentence: str, listable: bool) -> None:
    from server.memory.app.extract.coverage import sentence_is_listable

    assert sentence_is_listable(sentence) is listable


def _fact_reply(key: str, value: str, idx: int) -> str:
    return json.dumps({"facts": [{"key": key, "value": value, "source_message_indices": [idx]}]})


class _ByTurn(_Client):
    """Answers each sweep call from the turn it lists; the order calls arrive
    in is not part of the contract (the groups run concurrently)."""

    def __init__(self, *, down: str = "") -> None:
        super().__init__([])
        self.down = down

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        tail = user.split("uncovered_turns", 1)[1] if "uncovered_turns" in user else ""
        if self.down and self.down in tail:
            raise RuntimeError("provider down")
        if "[1:user Melanie]" in tail:
            text = _fact_reply(
                "melanie_pottery_class", "Melanie signed up for a pottery class yesterday.", 1
            )
        elif "[3:user Melanie]" in tail:
            text = _fact_reply("melanie_new_shoes", "Melanie got new shoes.", 3)
        else:
            text = json.dumps({"facts": []})
        return LLMResult(text=text, tokens_in=10, tokens_out=5, provider="stub", model="stub-1")


@pytest.mark.asyncio
async def test_batched_sweep_reads_items_in_groups_and_merges(monkeypatch) -> None:
    client = _ByTurn()
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    monkeypatch.setattr(
        "server.memory.app.extract.sweep.settings.memory_extract_coverage_sweep_batch_items",
        1,
        raising=False,
    )
    out, result, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date="2023-07-03",
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    # Two uncovered turns, one item a call: two calls, each listing one turn
    # and carrying the first pass's facts; the facts merge, the usage sums.
    assert len(client.calls) == 2 and added == 2
    assert result is not None and (result.tokens_in, result.tokens_out) == (20, 10)
    assert sorted(f.memory_key for f in out[1:]) == ["melanie_new_shoes", "melanie_pottery_class"]
    for call in client.calls:
        assert "already_extracted" in call["user"] and "SECOND READING" in call["system"]
        tail = call["user"].split("uncovered_turns", 1)[1]
        assert ("[1:user Melanie]" in tail) != ("[3:user Melanie]" in tail)


@pytest.mark.asyncio
async def test_batched_sweep_fails_open_per_group(monkeypatch) -> None:
    client = _ByTurn(down="[1:user Melanie]")
    monkeypatch.setattr("server.memory.app.extract.sweep.get_llm_client", lambda: client)
    monkeypatch.setattr(
        "server.memory.app.extract.sweep.settings.memory_extract_coverage_sweep_batch_items",
        1,
        raising=False,
    )
    out, result, added = await _coverage_sweep(
        MSGS,
        FIRST,
        system_prompt="SYS",
        observation_date="2023-07-03",
        last_messages=None,
        max_tokens=512,
        min_chars=40,
        max_turns=60,
    )
    # The group whose call failed contributes nothing; the other lands.
    assert len(client.calls) == 2 and added == 1
    assert [f.memory_key for f in out[1:]] == ["melanie_new_shoes"]
    assert result is not None and result.tokens_in == 10


def test_batch_items_default_is_one_call() -> None:
    from server.settings import settings

    assert type(settings).model_fields["memory_extract_coverage_sweep_batch_items"].default == 0
