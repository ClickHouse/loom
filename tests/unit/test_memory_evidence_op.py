"""`memory.evidence`: a memory's source turns, spans and link
scope, readable on the wire so integrity can be measured without ClickHouse.

CH-free: the repository lookup and the client are faked."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryEvidenceInput, MemoryEvidenceOutput
from server.memory.app import ops as _memory_ops
from server.memory.app.evidence_reads import evidence_scope, handle_memory_evidence
from server.operations.registry import ALL_OPERATIONS


def test_scope_reads_the_support_type_and_the_legacy_window() -> None:
    assert evidence_scope("extracted_from", 26, 29, 1) == "cited"
    assert evidence_scope("lexical_support", 0, 0, 2) == "lexical"
    assert evidence_scope("window", 0, 0, 20) == "window"
    # legacy whole-window links written under extracted_from: many rows,
    # zero-length spans → not a citation
    assert evidence_scope("extracted_from", 0, 0, 21) == "window"
    # a single zero-span extracted_from row is still a citation (span optional)
    assert evidence_scope("extracted_from", 0, 0, 1) == "cited"
    assert evidence_scope("source", 0, 0, 1) == "other"
    # consolidation lineage: the evidence id is a source memory id
    assert evidence_scope("derived_from", 0, 0, 5) == "derived"


def test_op_is_registered_for_viewers() -> None:
    op = next(o for o in ALL_OPERATIONS if o.name == "memory.evidence")
    assert op.required_role.name == "VIEWER"
    assert op.input_model is MemoryEvidenceInput and op.output_model is MemoryEvidenceOutput


class _FakeCH:
    def __init__(
        self,
        links: list[dict[str, Any]],
        evidence: list[dict[str, Any]],
        memories: list[dict[str, Any]] | None = None,
    ) -> None:
        self.links, self.evidence, self.memories = links, evidence, memories or []
        self.calls: list[dict[str, Any]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append(dict(params or {}))
        if "memory_belief_sources" in sql:
            assert ("tombstoned_at IS NULL" in sql or "tomb_latest IS NULL" in sql) and params
            return self.links
        if "FROM loom.memory FINAL" in sql and "memory_id IN" in sql:
            assert params is not None
            return [m for m in self.memories if m["eid"] in params["ids"]]
        assert "memory_evidence" in sql and params is not None
        return [e for e in self.evidence if e["eid"] in params["ids"]]


class _Row:
    memory_id = "mem-1"
    value = "User's dog Max is awesome at catching frisbees mid-air."


@pytest.mark.asyncio
async def test_handler_pages_links_and_clips_the_span(monkeypatch: pytest.MonkeyPatch) -> None:
    content = "Melanie: Good luck! Also, Max my dog is awesome at catching frisbees mid-air."
    ch = _FakeCH(
        links=[
            {
                "eid": "e1",
                "support_type": "extracted_from",
                "char_start": 26,
                "char_end": 29,
            },
            {
                "eid": "e2",
                "support_type": "lexical_support",
                "char_start": 0,
                "char_end": 0,
            },
        ],
        evidence=[
            {
                "eid": "e1",
                "role": "user",
                "observed_at": datetime(2023, 7, 13, 9, 9),
                "content": content,
            },
            {
                "eid": "e2",
                "role": "user",
                "observed_at": datetime(2023, 7, 13, 9, 5),
                "content": "Melanie: Hey!",
            },
        ],
    )

    async def fake_get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        assert identity.namespace == "ns" and key == "user_dog_max"
        return _Row()

    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await handle_memory_evidence(
        MemoryEvidenceInput(org="o", namespace="ns", agent="a", memory_key="user_dog_max", limit=1)
    )
    assert out.found and out.memory_id == "mem-1" and out.value.startswith("User's dog Max")
    # counts cover all links; the page is one item
    assert (out.cited, out.lexical, out.window) == (1, 1, 0) and out.truncated
    assert len(out.items) == 1
    it = out.items[0]
    assert it.evidence_id == "e1" and it.scope == "cited" and it.span == "Max"
    assert it.role == "user" and it.content == content
    # only the paged ids were fetched from the evidence table
    assert ch.calls[1]["ids"] == ["e1"]


@pytest.mark.asyncio
async def test_missing_key_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return None

    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)
    monkeypatch.setattr(_memory_ops, "__name__", _memory_ops.__name__)  # no-op guard
    out = await handle_memory_evidence(
        MemoryEvidenceInput(org="o", namespace="ns", agent="a", memory_key="nope")
    )
    assert out.found is False and out.items == []


class _DerivedRow:
    memory_id = "mem-d"
    value = "User keeps two dogs, Max and Bowie."


@pytest.mark.asyncio
async def test_derived_links_resolve_to_the_source_memories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _FakeCH(
        links=[
            {"eid": "m1", "support_type": "derived_from", "char_start": 0, "char_end": 0},
            {"eid": "m2", "support_type": "derived_from", "char_start": 0, "char_end": 0},
        ],
        evidence=[],
        memories=[
            {
                "eid": "m1",
                "memory_key": "user_dog_max",
                "value": "User has a dog named Max.",
                "observed_at": datetime(2026, 9, 1),
            },
            {
                "eid": "m2",
                "memory_key": "user_dog_bowie",
                "value": "User has a dog named Bowie.",
                "observed_at": datetime(2026, 9, 2),
            },
        ],
    )

    async def fake_get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        return _DerivedRow()

    monkeypatch.setattr(memory_repo, "get_by_key", fake_get_by_key)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await handle_memory_evidence(
        MemoryEvidenceInput(org="o", namespace="ns", agent="a", memory_key="consolidated:dogs")
    )
    assert out.found and out.derived == 2 and out.cited == 0
    assert [it.scope for it in out.items] == ["derived", "derived"]
    assert out.items[0].role == "memory" and out.items[0].content.startswith("[user_dog_max] ")
    # the second read went to loom.memory for the source ids, not to the evidence table
    assert ch.calls[1]["ids"] == ["m1", "m2"]
