"""Namespace graph evidence cannot shape a read bounded to one identity."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from server.core import ch as ch_module
from server.core.ch import CHClient
from server.memory.app import search_llm
from server.memory.app.retrieval import graph as graph_reads
from server.memory.app.search.graph import _run_graph_stages
from tests.integration._resources import unique_org


def _context(org: str, user: str | None) -> Any:
    return SimpleNamespace(
        input=SimpleNamespace(
            org=org,
            namespace="graph",
            search_mode="graph",
            graph_seed="query",
            user_id=user,
            session_id=None,
            session_ids=[],
            run_id=None,
            agent="*",
            scope="private",
            as_of=None,
            query="topic likes outsider",
        ),
        actual_work={},
        effective_entity_filter=["topic"],
        namespaces=["graph"],
        query_anchor_iso="",
        obs_date="",
        graph_paths_by_ns={},
        graph_expanded_by_ns={},
        triple_matched_by_ns={},
        query_triples=[],
        llm_free=False,
        search_budget=SimpleNamespace(note_llm_call=lambda: None),
    )


async def test_outside_graph_evidence_changes_only_the_namespace_control(
    monkeypatch: Any, ch_client: Any
) -> None:
    org = unique_org("graph-read-scope")
    ch = CHClient()
    await ch.connect()
    monkeypatch.setattr(ch_module, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.search.graph.get_client", lambda: ch)
    triples = AsyncMock(return_value=[("topic", "likes", "outsider")])
    monkeypatch.setattr(search_llm, "extract_query_triples_llm", triples)
    monkeypatch.setattr(graph_reads, "expand_verb_synonyms", AsyncMock(return_value=["likes"]))
    try:
        before = _context(org, "reader")
        await _run_graph_stages(before)
        ch_client.insert(
            "loom.entity_edges",
            [[org, "graph", "outsider", "topic", 10]],
            column_names=["org", "namespace", "entity_a", "entity_b", "co_occur"],
        )
        columns = ["org", "namespace", "subject", "relation", "object"]
        ch_client.insert(
            "loom.entity_triples",
            [[org, "graph", "topic", "likes", "outsider"]],
            column_names=columns,
        )
        ch_client.insert(
            "loom.entity_triple_memories",
            [[org, "graph", "topic", "likes", "outsider", "shared-key"]],
            column_names=[*columns, "memory_key"],
        )
        after = _context(org, "reader")
        await _run_graph_stages(after)
        assert before.graph_expanded_by_ns == after.graph_expanded_by_ns == {}
        assert before.triple_matched_by_ns == after.triple_matched_by_ns == {}
        assert before.graph_paths_by_ns == after.graph_paths_by_ns == {}
        triples.assert_not_awaited()

        control = _context(org, None)
        await _run_graph_stages(control)
        assert control.graph_expanded_by_ns == {"graph": ["outsider"]}
        assert control.triple_matched_by_ns == {"graph": ["shared-key"]}
        assert control.graph_paths_by_ns["graph"]["outsider"] == ["topic", "outsider"]
        triples.assert_awaited_once()
    finally:
        await ch.disconnect()
