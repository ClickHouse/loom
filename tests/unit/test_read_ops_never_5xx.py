"""A read op answers 4xx or an empty result, never 5xx, to a
well-formed request.

Six read ops that could answer 500 to a synthesised body are reproduced here
at the handler with a fake client and pinned to the answer each must give: a
mapped row set, a 404
RESOURCE_NOT_FOUND or a 422 VALIDATION_FAILED.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace as NS
from typing import Any

import pytest

from server.core.errors import NotFoundError, ValidationError


class _FakeCH:
    """Records every statement; answers query_all / query_one from queues."""

    def __init__(
        self,
        *,
        all_returns: list[list[dict[str, Any]]] | None = None,
        one_returns: list[dict[str, Any] | None] | None = None,
    ) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self._all = list(all_returns or [])
        self._one = list(one_returns or [])

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        self.calls.append((sql, params or {}))
        return self._all.pop(0) if self._all else []

    async def query_one(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any] | None:
        self.calls.append((sql, params or {}))
        return self._one.pop(0) if self._one else None


def _use(monkeypatch: pytest.MonkeyPatch, ch: _FakeCH, *modules: str) -> None:
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    for mod in modules:
        monkeypatch.setattr(f"{mod}.get_client", lambda: ch, raising=False)


def test_list_ontology_answers_the_rows_it_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    """namespace.list_ontology: the entry model lives in namespace_models;
    importing it from the operations module would be an ImportError (a 500) on
    every call."""
    from server.identity.api.models_namespace_catalog import (
        NamespaceListOntologyInput,
        _OntologyEntry,
    )
    from server.identity.app.namespace_ontology import handle_namespace_list_ontology

    ch = _FakeCH(
        all_returns=[
            [
                {
                    "kind": "entity",
                    "name": "customer",
                    "status": "accepted",
                    "description": None,
                    "cardinality": None,
                    "created_by": "alice",
                    "updated_at": datetime(2026, 9, 15, 6, 0),
                }
            ]
        ]
    )
    _use(monkeypatch, ch, "server.identity.app.namespace_ontology")
    out = asyncio.run(
        handle_namespace_list_ontology(NamespaceListOntologyInput(org="o", namespace="purged"))
    )
    assert len(out.entries) == 1 and isinstance(out.entries[0], _OntologyEntry)
    assert out.entries[0].name == "customer" and out.entries[0].description == ""
    assert ch.calls[0][1] == {"org": "o", "ns": "purged", "status": "accepted"}

    empty = asyncio.run(
        handle_namespace_list_ontology(
            NamespaceListOntologyInput(org="o", namespace="purged", status=None)
        )
    )
    assert empty.entries == [] and "status" not in ch.calls[1][1]


def test_summary_composes_from_the_models_module(monkeypatch: pytest.MonkeyPatch) -> None:
    """namespace.summary: like list_ontology — the two private row models
    come from the module that defines them, not one that does not re-export
    them."""
    from server.identity.api.models_namespace_catalog import NamespaceSummaryInput
    from server.identity.app.namespace_summary import handle_namespace_summary

    ch = _FakeCH(
        all_returns=[
            [{"memory_type": "semantic", "n": 3}, {"memory_type": "decision", "n": 1}],
            [{"entity": "alice", "degree": 7}],
        ],
        one_returns=[{"writes": 2, "recalls": 5, "agents": 1, "last_at": datetime(2026, 9, 15)}],
    )
    _use(monkeypatch, ch, "server.identity.app.namespace_summary")
    out = asyncio.run(handle_namespace_summary(NamespaceSummaryInput(org="o", namespace="ns")))
    assert out.total_memories == 4 and [t.n for t in out.type_counts] == [3, 1]
    assert out.top_entities[0].entity == "alice" and out.recent_recalls == 5

    # A purged namespace: no rows anywhere, the activity read answers None.
    ch = _FakeCH(one_returns=[None])
    _use(monkeypatch, ch, "server.identity.app.namespace_summary")
    out = asyncio.run(handle_namespace_summary(NamespaceSummaryInput(org="o", namespace="purged")))
    assert out.total_memories == 0 and out.top_entities == [] and out.last_activity_at is None


def test_list_categories_is_two_plain_reads_ordered_here(monkeypatch: pytest.MonkeyPatch) -> None:
    """ClickHouse 26.8 rejects a correlated subquery named in ORDER BY (code
    48); the count is one grouped read and the ordering is done in Python."""
    from server.identity.api.models_namespace_catalog import NamespaceListCategoriesInput
    from server.identity.app.namespace_catalog import handle_namespace_list_categories

    ch = _FakeCH(
        all_returns=[
            [
                {"category_id": "c-b", "name": "beta", "description": "b"},
                {"category_id": "c-a", "name": "alpha", "description": "a"},
                {"category_id": "c-z", "name": "zeta", "description": "z"},
            ],
            [{"category_id": "c-a", "item_count": 2}, {"category_id": "c-z", "item_count": 2}],
        ]
    )
    _use(monkeypatch, ch, "server.identity.app.namespace_catalog")
    out = asyncio.run(
        handle_namespace_list_categories(NamespaceListCategoriesInput(org="o", namespace="ns"))
    )
    assert [(c.name, c.item_count) for c in out.categories] == [
        ("alpha", 2),
        ("zeta", 2),
        ("beta", 0),
    ]
    assert len(ch.calls) == 2
    for sql, _ in ch.calls:
        assert "(SELECT" not in sql and "ORDER BY" not in sql
    assert "GROUP BY category_id" in ch.calls[1][0]

    # A purged namespace: one read, no count read, an empty result.
    ch = _FakeCH()
    _use(monkeypatch, ch, "server.identity.app.namespace_catalog")
    out = asyncio.run(
        handle_namespace_list_categories(NamespaceListCategoriesInput(org="o", namespace="purged"))
    )
    assert out.categories == [] and len(ch.calls) == 1


def test_threads_aggregate_alias_does_not_shadow_the_agent_filter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`argMax(agent, ts) AS agent` next to `WHERE agent = {agent}` is code
    184 (ILLEGAL_AGGREGATION) whenever a body names an agent, so the alias
    differs."""
    from server.knowledge.api.models_conversations import ConversationThreadsInput
    from server.knowledge.app.conversations import handle_conversation_threads

    ts = datetime(2026, 9, 15, 6, 0)
    ch = _FakeCH(
        all_returns=[
            [{"thread_id": "t1", "msg_count": 2, "first_seen": ts, "last_seen": ts}],
            [{"thread_id": "t1", "n": 2, "last_turn": ("a", "user", "hi")}],
            [{"thread_id": "t1", "title": "Titled"}],
        ]
    )
    _use(monkeypatch, ch)
    out = asyncio.run(
        handle_conversation_threads(ConversationThreadsInput(org="o", namespace="ns", agent="a"))
    )
    sql, params = ch.calls[0]
    keyed = ch.calls[1][0]
    # The last turn's agent rides one aliased tuple, never an `agent` alias.
    assert "AS last_turn" in keyed and " AS agent" not in sql + keyed
    assert "AND agent = {agent:String}" in sql and params["agent"] == "a"
    assert out.threads[0].agent == "a" and out.threads[0].title == "Titled"

    ch = _FakeCH()
    _use(monkeypatch, ch)
    out = asyncio.run(
        handle_conversation_threads(
            ConversationThreadsInput(org="o", namespace="purged", agent="*")
        )
    )
    assert out.threads == [] and "agent = {agent" not in ch.calls[0][0]


def test_locate_on_a_key_with_no_procedure_is_not_found(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_procedure import ProcedureLocateInput
    from server.memory.app.procedure import handle_procedure_locate

    async def _get(inp: Any) -> Any:
        return NS(value=None, content_sha256="", found=False)

    monkeypatch.setattr("server.memory.app.handlers.handle_memory_get", _get)
    with pytest.raises(NotFoundError) as ei:
        asyncio.run(
            handle_procedure_locate(
                ProcedureLocateInput(org="o", namespace="purged", memory_key="canary_nope")
            )
        )
    assert ei.value.status_code == 404 and ei.value.code == "RESOURCE_NOT_FOUND"
    assert "canary_nope" in str(ei.value)


def test_scip_status_rejects_a_batch_id_that_is_not_a_uuid(monkeypatch: pytest.MonkeyPatch) -> None:
    """The batch column is a UUID; a body the column cannot parse answers 422
    before any read, never a ClickHouse 500 (code 376)."""
    from server.code.api.models_ingest import CodeScipStatusInput
    from server.code.app import ingest as code_ingest

    monkeypatch.setattr(code_ingest, "require_code_intel", lambda: None)

    class _NoRepo:
        def __init__(self) -> None:
            raise AssertionError("the repository must not be touched for a malformed batch_id")

    monkeypatch.setattr(code_ingest, "CodeRepo", _NoRepo)
    with pytest.raises(ValidationError) as ei:
        asyncio.run(
            code_ingest.handle_code_scip_status(
                CodeScipStatusInput(org="o", namespace="ns", batch_id="canary-not-a-uuid")
            )
        )
    assert ei.value.status_code == 422 and ei.value.code == "VALIDATION_FAILED"
