"""Concept phrases are graph nodes too: the extractor's
`topics` parse, become co-occurrence nodes beside entities, and write
`about` triples — never code-classified."""

from __future__ import annotations

from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry, _BulkSetResult
from server.memory.app.extract import ExtractedFact
from server.memory.app.extract.parse import _parse_fact_list, _parse_topics


def test_parse_fact_list_carries_topics() -> None:
    raw = (
        '{"facts": [{"key": "k", "value": "v", "entities": ["alice"], '
        '"topics": ["Retry storms after deploys", "retry storms after deploys", '
        '"Alice", "x", 7, "vegetarian cooking", "one too many"]}]}'
    )
    facts = _parse_fact_list(raw)
    assert facts == [
        ExtractedFact(
            memory_key="k",
            value="v",
            tags=[],
            entities=["alice"],
            topics=["retry storms after deploys", "vegetarian cooking", "one too many"],
        )
    ]


def test_parse_topics_rules() -> None:
    assert _parse_topics(None, []) == []
    assert _parse_topics("not a list", []) == []
    # bare entity copies are not topics; short tokens dropped; whitespace folded
    assert _parse_topics(["Bob", "  pleasant   weather in bangalore. "], ["bob"]) == [
        "pleasant weather in bangalore"
    ]
    assert len(_parse_topics(["a" * 200], [])[0]) == 80


class _StubCH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql, params=None):
        return []

    async def insert(self, table, rows, settings=None):
        self.inserts.append((table, rows))


def _input(**kw: Any) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="dev",
        namespace="ns",
        agent="pilot",
        user_id="alice",
        session_id="s1",
        entries=[_BulkSetEntry(memory_key="k1", value="v", **kw)],
    )


@pytest.mark.asyncio
async def test_topics_become_cooccurrence_nodes_beside_entities() -> None:
    from server.memory.app.projections import write_entity_edges

    ch = _StubCH()
    await write_entity_edges(
        _input(entities=["alice"], topics=["retry storms after deploys"]),
        [_BulkSetResult(memory_key="k1", version=0)],
        ch=ch,
        entity_map={},
    )
    edges = {t: rows for t, rows in ch.inserts}.get("loom.entity_edges", [])
    pairs = {(e["entity_a"], e["entity_b"]) for e in edges}
    # canonical form (head singularized, as every entity node is), one edge
    assert pairs == {("alice", "retry storms after deploy")}


@pytest.mark.asyncio
async def test_topics_write_about_triples_not_code_kinds() -> None:
    from server.memory.app.projections import write_entity_triples

    ch = _StubCH()
    await write_entity_triples(
        _input(topics=["vegetarian cooking"]),
        [_BulkSetResult(memory_key="k1", version=0)],
        ch=ch,
        entity_map={},
    )
    triples = {t: rows for t, rows in ch.inserts}.get("loom.entity_triples", [])
    # subject = the fact's actor (the writing agent), as the code-facet edges do;
    # a fact with topics but no relations / files / concepts still writes.
    assert [(t["subject"], t["relation"], t["object"]) for t in triples] == [
        ("pilot", "about", "vegetarian cooking")
    ]
    assert triples[0]["confidence"] == 1.0


def test_bulk_entry_caps_topics_at_three() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        _BulkSetEntry(memory_key="k", value="v", topics=["a", "b", "c", "d"])
