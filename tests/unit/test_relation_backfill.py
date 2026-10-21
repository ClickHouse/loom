"""Facts the first pass left without a typed relation get one
more, smaller call; the reply is canonicalised like the first pass."""

from __future__ import annotations

import json

import pytest

from server.llm.client import LLMResult
from server.memory.app.extract import ExtractedFact, extract_from_messages
from server.memory.app.extract.relations import _backfill_relations


class _Client:
    provider = "stub"
    model = "stub-1"

    def __init__(self, replies: list[str]) -> None:
        self.replies = list(replies)
        self.calls: list[dict[str, str]] = []

    async def complete(
        self,
        system: str,
        user: str,
        max_tokens: int = 1024,
        response_format: dict[str, str] | None = None,
    ) -> LLMResult:
        self.calls.append({"system": system, "user": user})
        text = self.replies.pop(0) if self.replies else "{}"
        return LLMResult(
            text=text, tokens_in=10, tokens_out=5, provider=self.provider, model=self.model
        )


@pytest.mark.asyncio
async def test_backfill_sends_only_the_relationless_two_entity_facts(monkeypatch) -> None:
    facts = [
        ExtractedFact(
            "apple_ceo",
            "The chief executive officer of Apple Inc. is Tim Cook.",
            entities=["apple inc", "tim cook"],
        ),
        ExtractedFact(
            "thomas_kyd_birthplace",
            "Thomas Kyd was born in London.",
            entities=["thomas kyd", "london"],
            relations=[("thomas kyd", "born_in", "london")],
        ),
        ExtractedFact("user_mood", "User is tired today.", entities=["user"]),
        ExtractedFact(
            "fatah_chairperson",
            "The chairperson of Fatah is Mahmoud Abbas.",
            entities=["fatah", "mahmoud abbas"],
        ),
    ]
    reply = json.dumps(
        {
            "relations": [
                {"i": 0, "s": "Apple Inc.", "v": "ceo_of", "o": "Tim Cook"},
                {"i": 1, "s": "fatah", "v": "chairperson_of", "o": "mahmoud abbas"},
                {"i": 7, "s": "x", "v": "y", "o": "z"},  # out of range: ignored
                {"i": 0, "s": "a", "v": "", "o": "b"},  # empty verb: ignored
                "not a dict",
            ]
        }
    )
    client = _Client([reply])
    monkeypatch.setattr("server.memory.app.extract.relations.get_llm_client", lambda: client)
    out, result, gained = await _backfill_relations(facts)
    assert gained == 2 and result is not None
    assert len(client.calls) == 1
    user = client.calls[0]["user"]
    assert "0. The chief executive officer of Apple Inc. is Tim Cook." in user
    assert "1. The chairperson of Fatah is Mahmoud Abbas." in user
    assert "Thomas Kyd" not in user and "tired" not in user  # already related / one entity
    assert out[0].relations[0][0] == "apple inc." and out[0].relations[0][2] == "tim cook"
    assert out[0].relations[0][1]  # canonicalised verb, non-empty
    assert out[3].relations and out[3].relations[0][2] == "mahmoud abbas"
    assert out[1] is facts[1] and out[2] is facts[2]


@pytest.mark.asyncio
async def test_nothing_missing_means_no_call_and_garbage_is_fail_open(monkeypatch) -> None:
    related = [ExtractedFact("k", "A is B.", entities=["a", "b"], relations=[("a", "is", "b")])]
    client = _Client(["should not be called"])
    monkeypatch.setattr("server.memory.app.extract.relations.get_llm_client", lambda: client)
    out, result, gained = await _backfill_relations(related)
    assert out == related and result is None and gained == 0 and client.calls == []
    missing = [ExtractedFact("k", "A leads B.", entities=["a", "b"])]
    client = _Client(["not json at all"])
    monkeypatch.setattr("server.memory.app.extract.relations.get_llm_client", lambda: client)
    out, result, gained = await _backfill_relations(missing)
    assert out == missing and gained == 0 and result is not None


@pytest.mark.asyncio
async def test_extract_from_messages_runs_the_backfill_after_the_first_pass(monkeypatch) -> None:

    first = json.dumps(
        {
            "facts": [
                {
                    "key": "apple_ceo",
                    "value": "The chief executive officer of Apple Inc. is Tim Cook.",
                    "entities": ["apple inc", "tim cook"],
                    "source_message_indices": [0],
                },
            ]
        }
    )
    second = json.dumps({"relations": [{"i": 0, "s": "apple inc", "v": "ceo_of", "o": "tim cook"}]})
    client = _Client([first, second])
    monkeypatch.setattr("server.memory.app.extract.client.get_llm_client", lambda: client)
    res = await extract_from_messages(
        [{"role": "user", "content": "The chief executive officer of Apple Inc. is Tim Cook."}],
        judge=False,
        operator_policy="",
    )
    assert len(client.calls) == 2 and "typed triples" in client.calls[1]["system"]
    assert res.relations_backfilled == 1
    assert res.facts[0].relations and res.facts[0].relations[0][2] == "tim cook"
    assert res.llm_result.tokens_in == 20  # both calls accounted


def test_backfill_vocabulary_names_the_social_relationships() -> None:
    # PerLTQA's relationship categories (50 of them) reach the triple plane
    # only if a verb names them.
    from server.memory.app.extract import _RELATION_BACKFILL_SYSTEM_PROMPT as p

    for verb in (
        "sibling_of",
        "friend_of",
        "colleague_of",
        "neighbour_of",
        "partner_of",
        "mentor_of",
        "student_of",
        "classmate_of",
        "roommate_of",
        "cousin_of",
        "works_with",
        "parent_of",
    ):
        assert verb in p, verb
    assert "elder sister is Z" in p and "v = sibling_of" in p
