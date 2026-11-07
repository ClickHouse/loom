"""memory.link carries its justification and canonicalizes the verb;
`contradicts` between two live memories opens a conflict."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest

from server.memory.api.models_graph import MemoryLinkInput
from server.memory.domain.edge_relations import CANONICAL_RELATIONS, canonical_link_relation


@pytest.mark.parametrize(
    ("raw", "verb", "flip"),
    [
        ("contradicts", "contradicts", False),
        ("Conflicts With", "contradicts", False),
        ("contradicted_by", "contradicts", True),
        ("blocked_by", "blocks", True),
        ("replaces", "supersedes", False),
        ("driven_by", "derived_from", False),
        ("caused", "caused", False),
        ("causes", "caused", False),
        ("related", "related_to", False),
        ("supports", "supports", False),
        ("some odd verb!", "some_odd_verb", False),  # unknown verbs pass through normalized
        ("", "", False),
    ],
)
def test_canonical_link_relation(raw: str, verb: str, flip: bool) -> None:
    assert canonical_link_relation(raw) == (verb, flip)
    if verb in CANONICAL_RELATIONS:
        # idempotent on its own output
        assert canonical_link_relation(verb) == (verb, False)


class _FakeCH:
    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []

    async def insert(self, table, rows, settings=None):
        assert table == "loom.operator_edges"
        self.rows.extend(rows)


def _link(fake: _FakeCH, **kw: Any):
    from server.memory.app import ops as h

    base: dict[str, Any] = {"org": "o", "namespace": "n", "from_key": "a", "to_key": "b"}
    base.update(kw)
    with patch("server.core.ch.get_client", return_value=fake):
        return asyncio.run(h.handle_memory_link(MemoryLinkInput(**base)))


def test_edge_row_carries_explanation_evidence_confidence_and_run() -> None:
    fake = _FakeCH()
    out = _link(
        fake,
        relation="Replaces",
        label="v2",
        explanation="The pinned image made the retry loop unnecessary.",
        evidence=["  CI green for 40 runs ", "", "x" * 600],
        confidence=0.85,
        run_id="r1",
    )
    assert out.linked is True and out.relation == "supersedes" and out.confidence == 0.85
    assert out.conflict_opened is False
    row = fake.rows[0]
    assert (row["from_key"], row["to_key"], row["relation"]) == ("a", "b", "supersedes")
    assert row["explanation"] == "The pinned image made the retry loop unnecessary."
    assert row["evidence"] == [
        "CI green for 40 runs",
        "x" * 500,
    ]  # trimmed, empties dropped, capped
    assert row["confidence"] == 0.85 and row["run_id"] == "r1" and row["weight"] == 1.0
    assert row["label"] == "v2" and row["tombstoned_at"] is None


def test_reversed_verb_flips_the_edge_direction() -> None:
    fake = _FakeCH()
    out = _link(fake, relation="blocked_by")  # "a is blocked by b" == "b blocks a"
    assert (out.from_key, out.to_key, out.relation) == ("b", "a", "blocks")
    assert (fake.rows[0]["from_key"], fake.rows[0]["to_key"]) == ("b", "a")


def test_defaults_read_as_before() -> None:
    fake = _FakeCH()
    out = _link(fake, relation="driven_by", label="GDPR")
    row = fake.rows[0]
    assert row["explanation"] == "" and row["evidence"] == [] and row["confidence"] == 1.0
    assert row["run_id"] == "-" and out.relation == "derived_from"


def _memory(mid: str):
    return SimpleNamespace(memory_id=mid)


def test_contradicts_between_two_memories_opens_a_conflict() -> None:
    from server.memory import store as memory_repo
    from server.memory.app import conflicts

    fake = _FakeCH()
    calls: list[dict[str, Any]] = []

    async def get_by_key(ident, key, **kw):
        return {"a": _memory("id-a"), "b": _memory("id-b")}.get(key)

    async def mark(**kw):
        calls.append(kw)
        return "conflict"

    with (
        patch.object(memory_repo, "get_by_key", get_by_key),
        patch.object(conflicts, "mark_peers_conflicted", mark),
    ):
        out = _link(
            fake,
            relation="conflicts with",
            explanation="They name different default ports.",
            evidence=["8123 vs 9000"],
        )
    assert out.relation == "contradicts" and out.conflict_opened is True
    assert calls == [{"org": "o", "new_memory_id": "id-a", "peer_ids": ["id-b"], "namespace": "n"}]
    assert fake.rows[0]["relation"] == "contradicts"  # the edge is written regardless


def test_contradicts_with_an_entity_endpoint_or_tombstone_opens_nothing() -> None:
    from server.memory import store as memory_repo
    from server.memory.app import conflicts

    calls: list[dict[str, Any]] = []

    async def get_by_key(ident, key, **kw):
        return _memory("id-a") if key == "a" else None  # "b" is a bare entity string

    async def mark(**kw):
        calls.append(kw)

    with (
        patch.object(memory_repo, "get_by_key", get_by_key),
        patch.object(conflicts, "mark_peers_conflicted", mark),
    ):
        out = _link(_FakeCH(), relation="contradicts")
        assert out.conflict_opened is False
        tomb = _link(_FakeCH(), relation="contradicts", tombstone=True)
        assert tomb.linked is False and tomb.conflict_opened is False
    assert calls == []


def test_contradicts_against_a_superseded_memory_opens_nothing() -> None:
    # "b" was superseded: the mark skips it and flags nothing, so no conflict opened.
    from server.memory import store as memory_repo

    fake = _FakeCH()

    async def get_by_key(ident, key, **kw):
        return _memory("id-" + key)

    async def get_by_id(memory_id, **kw):
        return SimpleNamespace(memory_id=memory_id, status="superseded")

    with (
        patch.object(memory_repo, "get_by_key", get_by_key),
        patch.object(memory_repo, "get_by_id", get_by_id),
    ):
        out = _link(fake, relation="contradicts")
    assert out.linked is True and out.conflict_opened is False and len(fake.rows) == 1


def test_conflict_marking_failure_keeps_the_edge() -> None:
    from server.memory import store as memory_repo
    from server.memory.app import conflicts

    fake = _FakeCH()

    async def get_by_key(ident, key, **kw):
        return _memory("id-" + key)

    async def mark(**kw):
        raise RuntimeError("ch hiccup")

    with (
        patch.object(memory_repo, "get_by_key", get_by_key),
        patch.object(conflicts, "mark_peers_conflicted", mark),
    ):
        out = _link(fake, relation="contradicts")
    assert out.linked is True and out.conflict_opened is False and len(fake.rows) == 1


def test_provenance_link_model_carries_the_justification() -> None:
    from server.memory.api.models_lifecycle import MemoryProvenanceLink

    link = MemoryProvenanceLink(direction="out", memory_key="b")
    assert (link.explanation, link.evidence, link.confidence, link.author, link.run_id) == (
        "",
        [],
        1.0,
        "",
        "",
    )


def test_empty_relation_is_stored_as_related_to_and_flagged() -> None:
    """An edge without a verb is a co-occurrence; loom stores
    the honest fallback and says so, so the ledger can count the habit."""
    fake = _FakeCH()
    out = _link(fake, relation="")
    assert out.relation == "related_to" and out.relation_defaulted is True
    assert fake.rows[0]["relation"] == "related_to"
    sharp = _link(_FakeCH(), relation="supports")
    assert sharp.relation_defaulted is False


@pytest.mark.parametrize(
    ("raw", "verb", "flip"),
    [
        ("precedes", "precedes", False),
        ("before", "precedes", False),
        ("followed_by", "precedes", False),
        ("after", "precedes", True),
        ("follows", "precedes", True),
        ("preceded_by", "precedes", True),
    ],
)
def test_temporal_order_verbs_canonicalize_to_precedes(raw: str, verb: str, flip: bool) -> None:
    assert canonical_link_relation(raw) == (verb, flip)
    assert "precedes" in CANONICAL_RELATIONS
