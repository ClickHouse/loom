"""Chain questions follow the first hop's answer entity into a
second retrieval; value-suffixed keys collapse onto the attribute's key."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.app.ask.completion import bridge_entities
from server.memory.app.ask.prompts import _CHAIN_NOTE
from server.memory.app.ask.question_shapes import is_chain_question
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.keys import strip_value_suffix, strip_value_suffixes


def test_chain_questions_are_detected_and_single_hops_are_not() -> None:
    for q in (
        "Which country does the current CEO of Twitter hold citizenship of?",
        "What is the country of citizenship of the spouse of the author of Our Mutual Friend?",
        "In which location did the spouse of Igor of Kiev pass away?",
        "What is the capital city of the country where the sport associated with John Harkes has its origin?",
        "Which faith is the partner of Laura Bush associated with?",
        "On which continent was the place of birth of Thomas Arne located?",
    ):
        assert is_chain_question(q), q
    for q in (
        "Who is the CEO of Twitter?",
        "What is Melanie's dog's name?",
        "When did Melanie run a charity race?",
        "Where has Melanie camped?",
        "How old is Xu Jia?",
    ):
        assert not is_chain_question(q), q
    assert "CHAINS two facts" in _CHAIN_NOTE and "memory wins" in _CHAIN_NOTE


def test_bridge_entities_are_the_hits_entities_the_question_does_not_name() -> None:
    q = "Which country does the current CEO of Twitter hold citizenship of?"
    hits = [
        SimpleNamespace(memory_key="twitter_ceo", entities=["twitter", "bernard arnault"]),
        SimpleNamespace(memory_key="twitter_hq", entities=["Twitter", "San Francisco"]),
        SimpleNamespace(memory_key="x", entities=["user", "the"]),
        SimpleNamespace(memory_key="y", entities=["Elon Musk"]),
    ]
    assert bridge_entities(hits, q) == ["bernard arnault", "San Francisco"]
    assert bridge_entities(hits, q, limit=3) == ["bernard arnault", "San Francisco", "Elon Musk"]
    assert bridge_entities([], q) == []
    # An entity the question already names is not a bridge.
    assert bridge_entities([SimpleNamespace(entities=["twitter", "ceo"])], q) == []
    # The wire hit model carries text, not entities: names come from the excerpt.
    wire = [
        SimpleNamespace(
            memory_key="twitter_ceo",
            content_excerpt='"The current CEO of Twitter is Bernard Arnault." (#twitter_ceo)',
        ),
        SimpleNamespace(memory_key="k2", content_excerpt="User asked about Twitter on Monday."),
        SimpleNamespace(
            memory_key="k3",
            content_excerpt="Thomas Arne was born in the city of Bengaluru. [thomas_arne_birthplace]",
        ),
    ]
    assert bridge_entities(wire, q, limit=4) == ["Bernard Arnault", "Thomas Arne", "Bengaluru"]


def test_value_suffix_strip_only_drops_proper_nouns_the_value_states() -> None:
    assert (
        strip_value_suffix(
            "thomas_arne_birthplace_bengaluru", "Thomas Arne was born in the city of Bengaluru."
        )
        == "thomas_arne_birthplace"
    )
    # An attribute word that appears lower-case in the value stays.
    assert (
        strip_value_suffix(
            "steve_sax_association_sport",
            "Steve Sax is associated with the sport of association football.",
        )
        == "steve_sax_association_sport"
    )
    # Never below three parts; never a token the value does not capitalise.
    assert strip_value_suffix("user_city_paris", "User lives in Paris.") == "user_city_paris"
    assert (
        strip_value_suffix("apple_ceo_tim_cook", "The CEO of Apple Inc. is Tim Cook.")
        == "apple_ceo"
    )
    assert strip_value_suffix("apple_ceo", "Tim Cook") == "apple_ceo"
    facts = [
        ExtractedFact(
            "thomas_arne_birthplace_bengaluru", "Thomas Arne was born in the city of Bengaluru."
        )
    ]
    assert strip_value_suffixes(facts)[0].memory_key == "thomas_arne_birthplace"


def test_bridge_rows_are_read_by_the_subjects_key_slug() -> None:
    import inspect

    from server.memory.app import ask as ma
    from server.memory.app.ask import completion, pool

    assert completion.key_slug("Carlos Gómez") == "carlos_g_mez"
    assert completion.key_slug("The University of the Arts") == "university_of_the_arts"
    assert completion.key_slug("Bernard Arnault") == "bernard_arnault"
    assert completion.key_slug("UK") == "uk"
    # The entity follow is the ask's question-shaped widening phase.
    from server.memory.app.ask import pipeline

    assert "_fill_pool(" in inspect.getsource(ma.ask)
    assert "_widen_by_question(" in inspect.getsource(pipeline._fill_pool)
    src = inspect.getsource(pool._widen_by_question)
    assert "bridge_rows_by_key(search_input, _bridge" in src
    assert 'f"{_bridge}: {question}"' in src  # the semantic follow remains the fallback


def test_bridges_come_from_fact_rows_before_chunks() -> None:
    from types import SimpleNamespace

    from server.memory.app.ask.completion import bridge_entities

    q = "Which country does the current CEO of Twitter hold citizenship of?"
    hits = [
        SimpleNamespace(
            source="chunk",
            content_excerpt="12. The CEO of Twitter is Jack Dorsey. 13. Paris is lovely.",
            entities=[],
        ),
        SimpleNamespace(
            source="fact",
            content_excerpt="The chief executive officer of Twitter is Bernard Arnault.",
            entities=[],
        ),
    ]
    assert bridge_entities(hits, q, limit=2)[0] == "Bernard Arnault"


@pytest.mark.asyncio
async def test_a_bridge_row_the_pool_holds_is_not_added_twice(monkeypatch) -> None:
    # each session's row of a key names its row, and one key cannot fill the read
    from server.memory.app.ask import completion
    from server.memory.app.ask.retrieval_legs import PooledRows
    from server.settings import settings

    asked: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
            assert "LIMIT {per_key:UInt32} BY memory_key" in sql
            asked.update(params)
            return [
                {"agent": "a", "user_id": "u", "session_id": s, "memory_key": "k", "value": "v"}
                for s in ("s1", "s2")
            ]

    monkeypatch.setattr(settings, "memory_search_per_key_cap", 3)
    monkeypatch.setattr("server.core.ch.get_client", lambda: _CH())
    inp = SimpleNamespace(org="o", namespace="n", agent="a", user_id="u", session_id=None)
    hits = await completion.bridge_rows_by_key(inp, "Bernard Arnault")
    assert (asked["per_key"], asked["lim"]) == (3, 6)
    pooled_hit = SimpleNamespace(memory_key="k", session_id="s1")
    pooled_hit._row_identity = ("a", "u", "s1")
    pooled = PooledRows([pooled_hit], "n")
    assert [h.session_id for h in hits if pooled.add(h)] == ["s2"]
