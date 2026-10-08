from __future__ import annotations

from typing import Any

import pytest

from server.memory.app.entity_resolution import (
    choose_canonical_candidate,
    lookup_canonical_entities,
    resolve_entities_for_write,
)


class StubCH:
    def __init__(self) -> None:
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.query_all_returns: list[list[dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.query_all_calls.append((sql, params or {}))
        return self.query_all_returns.pop(0) if self.query_all_returns else []

    async def insert(
        self,
        table: str,
        rows: list[dict[str, Any]],
        settings: dict[str, Any] | None = None,
    ):
        self.insert_calls.append((table, rows, settings or {}))


def test_choose_canonical_candidate_merges_high_similarity_low_entropy() -> None:
    decision = choose_canonical_candidate(
        "Northwind AI",
        ["northwindai", "unrelated product"],
    )

    assert decision.canonical_text == "northwindai"
    assert decision.strategy == "fuzzy"
    assert decision.confidence >= 0.95


def test_choose_canonical_candidate_keeps_ambiguous_short_name_separate() -> None:
    decision = choose_canonical_candidate("acme", ["acme labs", "acme corp"])

    assert decision.canonical_text == "acme"
    assert decision.strategy == "self"


@pytest.mark.asyncio
async def test_resolve_entities_for_write_is_namespace_scoped_and_persists() -> None:
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "mention_text": "northwindai",
                "canonical_text": "northwindai",
                "confidence": 1.0,
                "evidence_count": 2,
            }
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Northwind AI"],
    )

    assert resolved["northwind ai"] == "northwindai"
    sql, params = ch.query_all_calls[0]
    assert "namespace = {ns:String}" in sql
    assert params["ns"] == "private-agent"
    table, rows, settings = ch.insert_calls[0]
    assert table == "loom.entity_canonical_map"
    assert rows[0]["mention_text"] == "northwind ai"
    assert rows[0]["canonical_text"] == "northwindai"
    assert rows[0]["strategy"] == "fuzzy"
    # Synchronous insert: later sessions of the same multi-session ingest
    # must be able to read this resolution back immediately.
    assert settings["async_insert"] == 0
    assert settings["wait_for_async_insert"] == 1


@pytest.mark.asyncio
async def test_lookup_canonical_entities_read_only_expands_aliases() -> None:
    ch = StubCH()
    ch.query_all_returns.append([{"canonical_text": "northwindai"}])

    out = await lookup_canonical_entities(
        ch,
        org="clickhouse",
        namespaces=["private-agent"],
        entity_texts=["Northwind AI"],
    )

    assert out == ["northwind ai", "northwindai"]
    assert ch.insert_calls == []
    sql, params = ch.query_all_calls[0]
    assert "namespace IN {namespaces:Array(String)}" in sql
    assert params["namespaces"] == ["private-agent"]


# ----------------------------------------------------------------------
# Short-name over-merge guard
# ----------------------------------------------------------------------


def test_short_name_mismatch_blocks_near_identical_short_heads() -> None:
    """High string similarity on short tokens joins genuinely distinct
    names (a one-char edit flips the referent): the fuzzy gate must
    refuse, leaving the mention self-canonical."""
    decision = choose_canonical_candidate("qin dynasty", ["qing dynasty"])

    assert decision.canonical_text == "qin dynasty"
    assert decision.strategy == "self"


def test_short_name_guard_allows_separator_variants() -> None:
    """Pure spacing/punctuation variants compact to the same surface and
    must keep merging."""
    decision = choose_canonical_candidate("white water rafting", ["whitewater rafting"])

    assert decision.canonical_text == "whitewater rafting"
    assert decision.strategy == "fuzzy"


def test_short_name_guard_allows_long_token_typos() -> None:
    """Long-token near-duplicates (typos) still merge; direction is
    handled separately by evidence weighting."""
    decision = choose_canonical_candidate("naomi alderman", ["naomi alderdman"])

    assert decision.strategy == "fuzzy"


# ----------------------------------------------------------------------
# Evidence-weighted canonical direction
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeat_observation_flips_canonical_away_from_typo() -> None:
    """A correct spelling repeatedly observed must overtake a typo'd
    canonical that was merely written first: the cluster re-points to the
    better-attested variant."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {  # the typo's self-row: directly observed once
                "mention_text": "naomi alderdman",
                "canonical_text": "naomi alderdman",
                "confidence": 1.0,
                "evidence_count": 1,
                "created_at": None,
            },
            {  # the correct spelling, folded INTO the typo
                "mention_text": "naomi alderman",
                "canonical_text": "naomi alderdman",
                "confidence": 0.96,
                "evidence_count": 1,
                "created_at": None,
            },
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Naomi Alderman"],
    )

    # This observation makes the correct spelling's attestation (2) exceed
    # the typo's (1) -> direction flips.
    assert resolved["naomi alderman"] == "naomi alderman"
    _, rows, _ = ch.insert_calls[0]
    by_mention = {r["mention_text"]: r for r in rows}
    assert by_mention["naomi alderman"]["canonical_text"] == "naomi alderman"
    assert by_mention["naomi alderman"]["strategy"] == "self"
    assert by_mention["naomi alderman"]["evidence_count"] == 2
    # The old canonical's cluster (its own self-row) is re-pointed.
    assert by_mention["naomi alderdman"]["canonical_text"] == "naomi alderman"
    assert by_mention["naomi alderdman"]["evidence_count"] == 1


@pytest.mark.asyncio
async def test_better_attested_canonical_keeps_direction() -> None:
    """When the existing canonical's direct attestation still dominates,
    the mapping is reused unchanged (no flip, no re-point)."""
    ch = StubCH()
    ch.query_all_returns.append(
        [
            {
                "mention_text": "naomi alderdman",
                "canonical_text": "naomi alderdman",
                "confidence": 1.0,
                "evidence_count": 5,
                "created_at": None,
            },
            {
                "mention_text": "naomi alderman",
                "canonical_text": "naomi alderdman",
                "confidence": 0.96,
                "evidence_count": 1,
                "created_at": None,
            },
        ]
    )

    resolved = await resolve_entities_for_write(
        ch,
        org="clickhouse",
        namespace="private-agent",
        entity_texts=["Naomi Alderman"],
    )

    assert resolved["naomi alderman"] == "naomi alderdman"
    _, rows, _ = ch.insert_calls[0]
    assert len(rows) == 1
    assert rows[0]["strategy"] == "existing"
    assert rows[0]["canonical_text"] == "naomi alderdman"
