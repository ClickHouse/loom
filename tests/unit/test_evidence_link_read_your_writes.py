"""The evidence-link step reads the rows it just wrote with the write's
consistency, and counts the ones it still cannot resolve.

A plain FINAL read right after the insert can land on a replica that has not
seen the rows; `links` would come back empty and the step would return 0 with
no warning and no ledger row, leaving whole sessions with evidence rows and
no links."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from server.core import ch as ch_mod
from server.memory.app import drops, evidence
from server.memory.app.extract import evidence as ev


class _FakeCH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.sequential_seen: bool | None = None

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        assert "FROM loom.memory FINAL" in sql
        self.sequential_seen = ch_mod.sequential_reads_active()
        return self.rows


@pytest.mark.asyncio
async def test_lookup_runs_under_sequential_reads_and_counts_unresolved(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeCH([{"memory_key": "a", "memory_id": "id-a"}])  # "b" was written but not readable
    monkeypatch.setattr(ev, "get_client", lambda: fake)
    linked_calls: list[dict[str, Any]] = []

    async def fake_link(**kw: Any) -> None:
        linked_calls.append(kw)

    drops_seen: list[tuple[str, str, int]] = []
    monkeypatch.setattr(evidence, "link_belief_sources", fake_link)
    monkeypatch.setattr(
        drops,
        "record_drop",
        lambda org, ns, stage, reason, **kw: drops_seen.append(
            (stage, reason, int(kw.get("n", 0)))
        ),
    )
    inp = SimpleNamespace(org="o", namespace="n", agent="ag", user_id="-", session_id="s1")
    n = await ev._link_written_memories_to_evidence(
        inp,
        ["a", "b"],
        ["e1"],
        evidence_refs_by_memory_key={"a": [("e1", 0, 4)], "b": [("e1", 0, 4)]},
        evidence_scope_by_memory_key={"a": ev.SCOPE_CITED, "b": ev.SCOPE_CITED},
    )
    assert fake.sequential_seen is True, "the read-after-write must carry sequential consistency"
    assert not ch_mod.sequential_reads_active(), "the scope must not leak"
    # the readable memory is linked, the unreadable one is counted
    assert n == 1 and linked_calls and linked_calls[0]["memory_links"] == [("a", "id-a")]
    assert drops_seen == [("evidence.link", "unresolved_after_write", 1)]


@pytest.mark.asyncio
async def test_all_resolved_records_no_drop(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeCH([{"memory_key": "a", "memory_id": "id-a"}])
    monkeypatch.setattr(ev, "get_client", lambda: fake)

    async def fake_link(**kw: Any) -> None:
        return None

    monkeypatch.setattr(evidence, "link_belief_sources", fake_link)
    monkeypatch.setattr(drops, "record_drop", lambda *a, **kw: pytest.fail("no drop expected"))
    inp = SimpleNamespace(org="o", namespace="n", agent="ag", user_id="-", session_id="s1")
    assert await ev._link_written_memories_to_evidence(inp, ["a"], ["e1"]) == 1
