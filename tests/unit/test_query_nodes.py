"""Past queries as nodes: the node row builder, the write hook and the
erasure step. Stub ClickHouse throughout."""

from __future__ import annotations

import asyncio
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory.domain import query_nodes as qn


class StubCH:
    def __init__(self, returns: list[dict[str, Any]] | None = None, raise_on_query: bool = False):
        self.returns = returns or []
        self.raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def query_all(self, sql: str, params: dict[str, Any] | None = None):
        self.calls.append((" ".join(sql.split()), params or {}))
        if self.raise_on_query:
            raise RuntimeError("cold table")
        return self.returns

    async def insert(self, table, rows, settings=None):
        self.inserted.append((table, rows))


# --- identity -----------------------------------------------------------------


def test_normalise_collapses_case_whitespace_and_trailing_punctuation() -> None:
    assert qn.normalise_query("  How to   X?  ") == "how to x"
    assert qn.normalise_query("how to x") == qn.normalise_query("HOW TO X!!")
    assert qn.normalise_query("how to x") != qn.normalise_query("how to y")
    assert qn.normalise_query("   ") == ""


def test_query_hash_is_stable_uint64_over_the_normalised_text() -> None:
    h = qn.query_hash("How to X?")
    assert h == qn.query_hash("how to x") and 0 < h < 2**64
    assert qn.query_hash("how to y") != h


# --- write side ---------------------------------------------------------------


def test_build_row_carries_text_vector_answer_and_counts() -> None:
    row = qn.build_query_node_row(
        org="o",
        namespace="ns",
        query="Where is the RRF weight set?",
        query_vec=[0.1, 0.2],
        embedding_model="m",
        returned_keys=["k1", "k2"],
        abstain=False,
        now=datetime(2026, 9, 12, 7, 0, 0, 123456),
    )
    assert row is not None
    assert row["query"] == "where is the rrf weight set" and row["query_hash"] == qn.query_hash(
        row["query"]
    )
    assert (
        row["embedding"] == [0.1, 0.2]
        and row["embedding_dim"] == 2
        and row["embedding_model"] == "m"
    )
    assert row["returned_keys"] == ["k1", "k2"] and row["hits"] == 1 and row["abstains"] == 0
    assert row["first_seen"] == row["last_seen"] == datetime(2026, 9, 12, 7, 0, 0)


def test_build_row_is_none_without_text_vector_or_answer() -> None:
    kw = dict(org="o", namespace="ns", embedding_model="m", abstain=False)
    assert qn.build_query_node_row(query="", query_vec=[0.1], returned_keys=["k"], **kw) is None
    assert qn.build_query_node_row(query="q", query_vec=[], returned_keys=["k"], **kw) is None
    assert qn.build_query_node_row(query="q", query_vec=[0.1], returned_keys=[], **kw) is None


def test_build_row_counts_an_abstained_answer_and_caps_the_key_list() -> None:
    row = qn.build_query_node_row(
        org="o",
        namespace="ns",
        query="q",
        query_vec=[0.1],
        embedding_model="m",
        returned_keys=[f"k{i}" for i in range(80)],
        abstain=True,
    )
    assert row is not None and row["abstains"] == 1 and len(row["returned_keys"]) == 50


def test_handler_builder_gates_on_capture_consent(monkeypatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search.telemetry import _build_query_node_row
    from server.observability import genai

    inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="how to X", top_k=3)
    out = SimpleNamespace(results=[SimpleNamespace(memory_key="k1")], abstain=False)
    emb = SimpleNamespace(model_name="m")
    genai.set_identity(org="o", namespace="ns", capture_level="off")
    try:
        assert _build_query_node_row(inp, out, emb, [0.1]) is None
        genai.set_identity(org="o", namespace="ns", capture_level="all")
        row = _build_query_node_row(inp, out, emb, [0.1])
        assert row is not None and row["query"] == "how to x" and row["returned_keys"] == ["k1"]
        assert _build_query_node_row(inp, out, emb, None) is None
    finally:
        genai.set_identity()


@pytest.mark.asyncio
async def test_schedule_writes_trace_row_and_query_node_under_their_own_flags(monkeypatch) -> None:
    from server.memory.api.models_search import MemorySearchInput
    from server.memory.app.search import telemetry as h
    from server.observability import genai
    from server.settings import settings

    fake = StubCH()
    monkeypatch.setattr(h, "get_client", lambda: fake)
    monkeypatch.setattr(settings, "search_trace_log_enabled", True)
    monkeypatch.setattr(settings, "memory_query_nodes_enabled", True)
    genai.set_identity(org="o", namespace="ns", capture_level="all")
    try:
        inp = MemorySearchInput(org="o", namespace="ns", agent="a", query="how to X", top_k=3)
        out = SimpleNamespace(
            results=[SimpleNamespace(memory_key="k1", rank_score=0.9)],
            abstain=False,
            retrieval_risk="low",
        )
        h._schedule_trace_log(inp, out, SimpleNamespace(model_name="m"), 12, query_vec=[0.1, 0.2])
        for _ in range(5):
            await asyncio.sleep(0)
        tables = [t for t, _ in fake.inserted]
        assert tables == ["loom.search_trace_log", "loom.query_nodes"]
        assert fake.inserted[1][1][0]["embedding"] == [0.1, 0.2]

        # node flag alone → only the node; neither flag → nothing scheduled
        fake.inserted.clear()
        monkeypatch.setattr(settings, "search_trace_log_enabled", False)
        h._schedule_trace_log(inp, out, SimpleNamespace(model_name="m"), 12, query_vec=[0.1])
        for _ in range(5):
            await asyncio.sleep(0)
        assert [t for t, _ in fake.inserted] == ["loom.query_nodes"]
        fake.inserted.clear()
        monkeypatch.setattr(settings, "memory_query_nodes_enabled", False)
        h._schedule_trace_log(inp, out, SimpleNamespace(model_name="m"), 12, query_vec=[0.1])
        for _ in range(5):
            await asyncio.sleep(0)
        assert fake.inserted == []
    finally:
        genai.set_identity()


# --- erasure -------------------------------------------------------------------


def test_erasure_deletes_query_nodes_per_namespace_and_the_saga_includes_it(monkeypatch) -> None:
    from server.erasure.app import saga as erasure
    from server.erasure.store.steps_memory import delete_query_nodes_by_namespace

    class _CH:
        def __init__(self) -> None:
            self.executed: list[tuple[str, dict[str, Any]]] = []

        async def query_one(self, sql, params=None):
            return {"n": 2}

        async def execute(self, sql, params=None):
            self.executed.append((" ".join(sql.split()), dict(params or {})))

        async def query_all(self, sql, params=None):
            return [
                {
                    "mutation_id": "mutation_1.txt",
                    "is_done": 1,
                    "parts_to_do": 0,
                    "latest_fail_reason": "",
                }
            ]

    ch = _CH()
    assert asyncio.run(delete_query_nodes_by_namespace("o", "ns", ch=ch)) == 2
    sql, params = ch.executed[0]
    assert sql.startswith("ALTER TABLE loom.query_nodes DELETE WHERE")
    assert "mutations_sync = 0" in sql and {
        k: v for k, v in params.items() if k != "erasure_mutation_tag"
    } == {"org": "o", "ns": "ns"}

    names: list[str] = []

    async def _fake_step(
        *, step_name: str, step_fn: Any, completed: set[str], row: dict[str, Any], **kw: Any
    ) -> Any:
        names.append(step_name)
        return None

    monkeypatch.setattr(erasure, "_do_step", _fake_step)
    monkeypatch.setattr("server.erasure.app.backends.all_backends", lambda: [])
    asyncio.run(erasure._saga_namespace("o", "ns", completed=set(), row={}))
    assert "query_nodes.delete" in names
    assert names.index("query_nodes.delete") > names.index("memory.tombstone")


def test_query_nodes_are_off_by_default() -> None:
    from server.settings import Settings

    assert Settings().memory_query_nodes_enabled is False
