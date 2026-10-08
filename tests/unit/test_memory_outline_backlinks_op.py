"""`memory.outline` and `memory.backlinks`: a namespace's table
of contents and a memory's inverse links, read from structure alone."""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from server.memory.api.models_temporal import (
    MemoryBacklinksInput,
    MemoryBacklinksOutput,
    MemoryOutlineInput,
    MemoryOutlineOutput,
)
from server.memory.app import curation as cur
from server.operations.registry import ALL_OPERATIONS


class _RoutedCH:
    """Answers each query by the table it names; records the SQL and params."""

    def __init__(self, by_table: dict[str, list[dict[str, Any]]]) -> None:
        self.by_table = by_table
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, dict(params or {})))
        for table, rows in self.by_table.items():
            if table in sql:
                return rows
        return []


def test_ops_are_registered_for_viewers_and_classed_as_reads() -> None:
    from server.insights.app.op_mix import classify

    for name, inp, out in (
        ("memory.outline", MemoryOutlineInput, MemoryOutlineOutput),
        ("memory.backlinks", MemoryBacklinksInput, MemoryBacklinksOutput),
    ):
        op = next(o for o in ALL_OPERATIONS if o.name == name)
        assert op.required_role.name == "VIEWER" and op.idempotent
        assert op.input_model is inp and op.output_model is out
        assert classify(name) == "read"


@pytest.mark.asyncio
async def test_outline_composes_counts_sessions_hubs_communities_and_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _RoutedCH(
        {
            "loom.session_summaries FINAL WHERE org = {o:String} AND namespace = {n:String} AND tombstoned_at IS NULL ORDER BY": [
                {
                    "session_id": "ns:s4",
                    "summary_text": "Discussed the parish workshop. Then planned the retreat.",
                    "kf": 3,
                    "df": 7,
                    "d": datetime(2025, 9, 3),
                },
                {
                    "session_id": "ns:s2",
                    "summary_text": "Asked about knee strain",
                    "kf": 1,
                    "df": 2,
                    "d": datetime(2025, 9, 1),
                },
            ],
            "count() AS c FROM loom.session_summaries": [{"c": 23}],
            "loom.entity_degree": [
                {"entity": "parish", "degree": 40},
                {"entity": "ottawa", "degree": 12},
            ],
            "loom.entity_communities": [
                {
                    "community_id": 0,
                    "label": "parish",
                    "size": 9,
                    "computed_at": "2026-09-12 18:54:48",
                }
            ],
            "memory_type = 'profile'": [{"chars": 512}],
            "GROUP BY memory_type": [
                {"memory_type": "semantic", "c": 120},
                {"memory_type": "preference", "c": 6},
            ],
        }
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_outline(
        MemoryOutlineInput(org="o", namespace="n", agent="a", user_id="u", sessions_limit=1)
    )
    assert out.rows_by_type == {"semantic": 120, "preference": 6}
    assert out.sessions_total == 23 and len(out.sessions) == 1
    s0 = out.sessions[0]
    assert s0.session_id == "ns:s4" and s0.date == "2025-09-03"
    assert s0.headline == "Discussed the parish workshop." and s0.key_facts == 3 and s0.rows == 7
    assert [h.entity for h in out.hubs] == ["parish", "ottawa"]
    assert out.communities[0].label == "parish" and out.communities[0].size == 9
    assert out.profile_chars == 512
    # the digest page asked for one more row than the limit, to know it was cut
    digest_call = next(p for sql, p in ch.calls if "summary_text" in sql)
    assert digest_call["lim"] == 2


@pytest.mark.asyncio
async def test_outline_skips_the_profile_read_for_the_dash_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _RoutedCH({})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_outline(MemoryOutlineInput(org="o", namespace="n", agent="a"))
    assert out.profile_chars == 0 and out.sessions == [] and out.rows_by_type == {}
    assert not any("memory_type = 'profile'" in sql for sql, _ in ch.calls)


def test_headline_takes_the_first_sentence_or_clips() -> None:
    assert cur._headline("One. Two.") == "One."
    assert cur._headline("x" * 300) == "x" * 160
    assert cur._headline("  many   spaces here  ") == "many spaces here"


@pytest.mark.asyncio
async def test_backlinks_reads_the_six_inverse_sources(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _RoutedCH(
        {
            "loom.memory_versions": [
                {"version": 3, "status": "active", "created_at": "2026-09-13 08:00:00"},
                {"version": 2, "status": "superseded", "created_at": "2026-09-12 08:00:00"},
            ],
            "has(derived_from, {k:String})": [{"session_id": "ns:s3", "d": "2025-09-03 00:00:00"}],
            "AND to_key = {k:String}": [
                {
                    "memory_key": "user_new_pref",
                    "relation": "supersedes",
                    "created_at": "2026-09-13",
                }
            ],
            "AND from_key = {k:String}": [
                {
                    "memory_key": "user_old_pref",
                    "relation": "derived_from",
                    "created_at": "2026-09-12",
                }
            ],
            "loom.memory_belief_sources": [{"c": 4}],
            "loom.entity_edge_memories": [
                {"memory_key": "user_parish_role", "shared": ["parish", "ottawa"]}
            ],
        }
    )
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_backlinks(
        MemoryBacklinksInput(org="o", namespace="n", agent="a", memory_key="user_pref", limit=5)
    )
    assert out.found and [v.version for v in out.versions] == [3, 2]
    assert out.digests[0].session_id == "ns:s3" and out.digests[0].date == "2025-09-03"
    assert (
        out.links_in[0].memory_key == "user_new_pref" and out.links_in[0].relation == "supersedes"
    )
    assert out.links_out[0].relation == "derived_from"
    assert out.evidence_count == 4
    assert out.related[0].shared_entities == ["parish", "ottawa"]
    assert all(p.get("k") == "user_pref" for _, p in ch.calls)
    # The link tables are ReplacingMergeTree(created_at): a tombstoned link is a
    # newer version of the same key, so the reads take the latest version only.
    edge_sql = [sql for sql, _ in ch.calls if "loom.operator_edges" in sql]
    assert len(edge_sql) == 2 and all("loom.operator_edges FINAL" in sql for sql in edge_sql)
    [ev_sql] = [sql for sql, _ in ch.calls if "loom.memory_belief_sources" in sql]
    assert "argMax(tombstoned_at, created_at)" in ev_sql and "HAVING tomb_latest IS NULL" in ev_sql


@pytest.mark.asyncio
async def test_backlinks_of_an_unknown_key_is_empty_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _RoutedCH({})
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await cur.handle_memory_backlinks(
        MemoryBacklinksInput(org="o", namespace="n", agent="a", memory_key="nope")
    )
    assert not out.found and out.evidence_count == 0 and out.related == []
