"""memory.ask facet completion: a count question is answered
from the whole set of one attribute, not the top-k slice of it.

A namespace can hold all eight `*_age` facts while a top-k window fills with
unrelated facts, so the reader would answer "no ages in memories". CH-free:
the repository lookup is stubbed.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.app import ask as ma
from server.memory.app.ask import completion
from server.memory.app.ask.completion import derive_key_facets


def test_facets_from_shared_suffixes_and_query_cues() -> None:
    keys = [
        "user_maya_age",
        "user_miles_age",
        "chunk:s0:2",
        "summary:s0",
        "user_boss_email",
        "event_x_location",
    ]
    # the stem names the age facet → only it, even though email/location keys exist
    assert derive_key_facets(keys, "How many individuals are 28 years old or older?") == ["age"]
    # a cue in the stem wins over the suffix two hits happen to share
    assert derive_key_facets(keys, "How many events are happening in Atlanta?") == ["location"]
    # no cue: the suffix >=2 hits share is the facet
    assert derive_key_facets(keys, "How many of them qualify?") == ["age"]
    # projection keys and bare keys never contribute
    assert derive_key_facets(["chunk:s0:1", "summary:s0", "nounderscore"], "how many") == []


def test_facets_ignore_stopwords_and_cap_at_four() -> None:
    keys = [
        f"user_{i}_{f}"
        for i in range(2)
        for f in ("age", "height", "birthday", "location", "duration", "notes")
    ]
    out = derive_key_facets(keys, "how many")
    assert "notes" not in out and len(out) == 4


@pytest.mark.asyncio
async def test_repo_lookup_uses_key_suffix_and_identity_eligibility() -> None:
    class _CH:
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict[str, Any]]] = []

        async def query_all(self, sql, params=None):
            self.calls.append((sql, params or {}))
            return [
                {
                    "memory_key": "user_maya_age",
                    "memory_type": "semantic",
                    "content_excerpt": "Maya is 28",
                    "created_at": "2026-09-01 00:00:00",
                }
            ]

    ch = _CH()
    rows = await memory_repo.find_by_key_facet(
        org="o",
        namespace="n",
        facets=["age", "Bad Facet!", "location"],
        limit=60,
        scope="private",
        agent="a",
        user_id="u",
        ch=ch,
    )
    assert rows and rows[0]["memory_key"] == "user_maya_age"
    sql, params = ch.calls[0]
    assert "endsWith(memory_key, {fx0:String})" in sql and params["fx0"] == "_age"
    # malformed facet dropped; location expands to its synonym suffixes for older rows
    assert params["fx1"] == "_location" and params["fx2"] == "_venue" and params["fx3"] == "_city"
    assert "scope = {scope:String}" in sql and "agent = {agent:String}" in sql
    assert "user_id = {user_id:String}" in sql and "tombstoned_at IS NULL" in sql
    assert "ORDER BY memory_key ASC" in sql


@pytest.mark.asyncio
async def test_empty_facets_skip_the_query() -> None:
    class _CH:
        async def query_all(self, sql, params=None):
            raise AssertionError("must not query")

    assert (
        await memory_repo.find_by_key_facet(org="o", namespace="n", facets=["!!"], ch=_CH()) == []
    )


class _Hit:
    def __init__(self, key: str, text: str, sim: float = 0.5) -> None:
        self.memory_key = key
        self.memory_type = "semantic"
        self.content_excerpt = text
        self.similarity = sim
        self.rank_score = sim
        self.valid_at = "2026-09-01"
        self.created_at = "2026-09-01"


class _Retrieval:
    def __init__(self, hits: list[Any]) -> None:
        self.results = hits
        self.abstain = False
        self.abstain_reason = ""
        self.abstain_code = ""
        self.embedding_model = "fake"


class _SearchInput:
    org = "o"
    namespace = "n"
    agent = "*"
    user_id = None
    scope = "private"
    top_k = 3
    query = "How many individuals are 28 years old or older?"
    observation_date = None
    as_of = None
    run_id = None


def test_count_question_completes_the_facet_and_widens_the_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    hits = [
        _Hit("user_maya_age", "Maya is 28"),
        _Hit("user_miles_age", "Miles is 28"),
        _Hit("user_boss_email", "boss email x@y"),
    ]

    async def _search(_inp):
        return _Retrieval(hits)

    seen: dict[str, Any] = {}

    async def _facet_rows(**kw):
        seen.update(kw)
        return [
            {
                "memory_key": f"user_p{i}_age",
                "memory_type": "semantic",
                "content_excerpt": f"P{i} is {20 + i}",
                "created_at": "2026-09-01 00:00:00",
            }
            for i in range(6)
        ] + [
            {
                "memory_key": "user_maya_age",
                "memory_type": "semantic",
                "content_excerpt": "dup",
                "created_at": "",
            }
        ]

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr(memory_repo, "find_by_key_facet", _facet_rows)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.0)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)
    monkeypatch.setattr(settings, "memory_ask_code_enrichment", False, raising=False)

    res = asyncio.run(ma.ask(_SearchInput(), llm=None, allow_synthesis=False))
    assert res.source == "retrieval_only"
    assert res.facets == ["age"]
    assert res.facet_memories_added == 6  # the duplicate key was not added twice
    assert seen["facets"] == ["age"] and seen["limit"] == 60
    assert seen["scope"] == "private" and seen["agent"] == "*"
    # caller still gets its top_k; the completion rows are in the pool behind them
    assert [h.memory_key for h in res.results] == [
        "user_maya_age",
        "user_miles_age",
        "user_boss_email",
    ]


def test_non_count_question_never_completes(monkeypatch: pytest.MonkeyPatch) -> None:
    hits = [_Hit("user_maya_age", "Maya is 28"), _Hit("user_miles_age", "Miles is 28")]

    async def _search(_inp):
        return _Retrieval(hits)

    async def _boom(**kw):
        raise AssertionError("facet lookup must not run for a single-fact question")

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    monkeypatch.setattr(memory_repo, "find_by_key_facet", _boom)
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_ask_recency_weight", 0.0)
    monkeypatch.setattr(settings, "memory_ask_identifier_injection", False)

    class _SI(_SearchInput):
        query = "How old is Maya?"

    res = asyncio.run(ma.ask(_SI(), llm=None, allow_synthesis=False))
    assert res.facets == [] and res.facet_memories_added == 0


def test_facet_lookup_retries_the_fresh_part_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """ClickHouse code 10 on a part written seconds earlier is retried like the
    vector leg does; a fail-open [] would count a slice of the set."""
    calls = {"n": 0}

    async def _flaky(**kw):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError(
                "Code: 10. DB::Exception: Not found column org: in block memory_key String(size = 0)"
            )
        return [
            {
                "memory_key": "user_x_age",
                "memory_type": "semantic",
                "content_excerpt": "x is 30",
                "created_at": "",
            }
        ]

    monkeypatch.setattr(memory_repo, "find_by_key_facet", _flaky)
    out = asyncio.run(completion._facet_complete_memories(_SearchInput(), ["age"], [], limit=60))
    assert calls["n"] == 3 and [h.memory_key for h in out] == ["user_x_age"]

    async def _other(**kw):
        raise RuntimeError("some other failure")

    monkeypatch.setattr(memory_repo, "find_by_key_facet", _other)
    assert (
        asyncio.run(completion._facet_complete_memories(_SearchInput(), ["age"], [], limit=60))
        == []
    )


def test_facet_lookup_reads_with_sequential_consistency(monkeypatch: pytest.MonkeyPatch) -> None:
    """A replica that has SOME of a fresh namespace's parts returns a shorter
    list and nothing looks wrong; the one read
    where a missing row changes the number pays the metadata round-trip."""
    from server.core.ch import sequential_reads_active

    seen: list[bool] = []

    async def _rows(**kw):
        seen.append(sequential_reads_active())
        return [
            {
                "memory_key": "user_aunt_birthday",
                "memory_type": "semantic",
                "content_excerpt": "July 25",
                "created_at": "",
            }
        ]

    monkeypatch.setattr(memory_repo, "find_by_key_facet", _rows)
    out = asyncio.run(
        completion._facet_complete_memories(_SearchInput(), ["birthday"], [], limit=60)
    )
    assert seen == [True] and [h.memory_key for h in out] == ["user_aunt_birthday"]
    assert not sequential_reads_active()

    seen.clear()
    monkeypatch.setattr(memory_repo, "read_your_writes_enabled", lambda: False)
    asyncio.run(completion._facet_complete_memories(_SearchInput(), ["birthday"], [], limit=60))
    assert seen == [False]
