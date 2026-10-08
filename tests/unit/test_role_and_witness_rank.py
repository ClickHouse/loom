"""A lesson remembers who learned it."""

from __future__ import annotations

import asyncio
from typing import Any

from server.memory.api.models_search import MemorySearchInput
from server.memory.domain import query_nodes as qn


class StubCH:
    def __init__(self, returns=None, raise_on_query=False):
        self.returns = returns or []
        self.raise_on_query = raise_on_query
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), params or {}))
        if self.raise_on_query:
            raise RuntimeError("down")
        return self.returns


# --- role → soft tag ---------------------------------------------------------


def _inp(**kw):
    return MemorySearchInput(org="o", namespace="n", agent="a", query="q", **kw)


def test_role_becomes_a_soft_tag_and_never_narrows() -> None:
    from server.memory.app.search.request import _apply_role_tags

    assert _apply_role_tags(_inp()).tags == []
    out = _apply_role_tags(_inp(role=" Critic "))
    assert out.tags == ["role:critic"] and out.tags_soft is True
    # soft tags already present: the role joins them
    out = _apply_role_tags(_inp(role="planner", tags=["ci"], tags_soft=True))
    assert out.tags == ["ci", "role:planner"] and out.tags_soft is True
    # hard tag filter: the caller's scope is left alone (a role never filters)
    out = _apply_role_tags(_inp(role="planner", tags=["ci"]))
    assert out.tags == ["ci"] and out.tags_soft is False


def test_run_begin_stores_the_role_and_the_reflection_carries_it(monkeypatch) -> None:
    from datetime import datetime
    from unittest.mock import patch

    from server.memory import store as memory_repo
    from server.tasks.api.models_run import RunBeginInput, RunCompleteInput, RunReflection
    from server.tasks.app import run as h

    class _CH:
        def __init__(self) -> None:
            self.one: list[Any] = []
            self.all: list[Any] = []
            self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

        async def query_one(self, sql, params=None):
            return self.one.pop(0) if self.one else None

        async def query_all(self, sql, params=None):
            return self.all.pop(0) if self.all else []

        async def insert(self, table, rows, settings=None):
            self.inserted.append((table, rows))

    fake = _CH()

    async def record(**kw):
        pass

    monkeypatch.setattr(memory_repo, "record_usage_event", record)
    captured: list[Any] = []

    async def fake_set(inp):
        captured.append(inp)

    from server.memory.app import write as write_mod

    monkeypatch.setattr(write_mod, "handle_memory_set", fake_set)
    monkeypatch.setattr("server.memory.app.write.handle_memory_set", write_mod.handle_memory_set)
    with patch.object(h, "get_client", return_value=fake):
        fake.one.append(None)  # no existing run
        asyncio.run(
            h.handle_run_begin(
                RunBeginInput(
                    org="o",
                    namespace="n",
                    agent="critic-1",
                    goal="review the parser change",
                    run_id="r1",
                    role="Critic",
                    metadata={"k": 1},
                )
            )
        )
        _, rows = fake.inserted[-1]
        assert '"role":"critic"' in rows[0]["metadata"] and '"k":1' in rows[0]["metadata"]

        existing = dict(rows[0])
        existing["started_at"] = datetime(2026, 9, 12, 8, 0, 0)
        fake.one.append(existing)  # _fetch_run
        fake.all.append([])  # written
        fake.one.append({"n": 0})  # observations
        asyncio.run(
            h.handle_run_complete(
                RunCompleteInput(
                    org="o",
                    namespace="n",
                    run_id="r1",
                    status="completed",
                    reflection=RunReflection(
                        lessons=["Read the diff before the tests."],
                        confidence=0.7,
                        evidence=["role_witness"],
                    ),  # A lesson names its evidence,
                )
            )
        )
    inp = captured[0]
    assert "role:critic" in inp.tags and inp.value["role"] == "critic"
    assert inp.memory_type == "reflection" and inp.memory_key == "reflection:r1"


# --- witnesses ----------------------------------------------------------------


def test_node_row_records_who_asked() -> None:
    row = qn.build_query_node_row(
        org="o",
        namespace="n",
        query="q",
        query_vec=[0.1],
        embedding_model="m",
        returned_keys=["k"],
        abstain=False,
        session_id="s9",
    )
    assert row is not None and row["sessions"] == ["s9"]
    row = qn.build_query_node_row(
        org="o",
        namespace="n",
        query="q",
        query_vec=[0.1],
        embedding_model="m",
        returned_keys=["k"],
        abstain=False,
    )
    assert row is not None and row["sessions"] == []
