"""The procedure run ledger sits inside the erasure boundary,
and procedural rows embed their text, not their JSON."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest


class _FakeCH:
    def __init__(self, n: int = 3) -> None:
        self.n = n
        self.queries: list[tuple[str, dict[str, Any]]] = []
        self.executed: list[tuple[str, dict[str, Any]]] = []

    async def query_one(self, sql: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.queries.append((" ".join(sql.split()), dict(params or {})))
        return {"n": self.n}

    async def execute(self, sql: str, params: dict[str, Any] | None = None) -> None:
        self.executed.append((" ".join(sql.split()), dict(params or {})))

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        return [
            {
                "mutation_id": "mutation_1.txt",
                "is_done": 1,
                "parts_to_do": 0,
                "latest_fail_reason": "",
            }
        ]


def test_delete_procedure_runs_by_namespace_is_a_scoped_alter_delete() -> None:
    from server.erasure.store.steps_memory import delete_procedure_runs_by_namespace

    ch = _FakeCH(n=4)
    n = asyncio.run(delete_procedure_runs_by_namespace("o", "ns", ch=ch))
    assert n == 4
    sql, params = ch.executed[0]
    assert sql.startswith("ALTER TABLE loom.procedure_runs DELETE WHERE")
    assert "org = {org:String} AND namespace = {ns:String}" in sql
    assert "mutations_sync = 0" in sql
    assert {k: v for k, v in params.items() if k != "erasure_mutation_tag"} == {
        "org": "o",
        "ns": "ns",
    }


def test_delete_procedure_runs_by_key_scopes_to_one_procedure_and_skips_when_empty() -> None:
    from server.memory.store import delete_procedure_runs_by_key

    ch = _FakeCH(n=0)
    assert asyncio.run(delete_procedure_runs_by_key("o", "ns", "procedure_x", ch=ch)) == 0
    assert ch.executed == []  # nothing to delete, no mutation issued
    ch = _FakeCH(n=2)
    asyncio.run(delete_procedure_runs_by_key("o", "ns", "procedure_x", ch=ch))
    sql, params = ch.executed[0]
    assert "memory_key = {key:String}" in sql
    assert params["key"] == "procedure_x"


def test_namespace_erasure_saga_includes_the_run_ledger(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.erasure.app import saga as erasure

    names: list[str] = []

    async def _fake_step(
        *, step_name: str, step_fn: Any, completed: set[str], row: dict[str, Any], **kw: Any
    ) -> Any:
        names.append(step_name)
        return None

    monkeypatch.setattr(erasure, "_do_step", _fake_step)
    monkeypatch.setattr("server.erasure.app.backends.all_backends", lambda: [])
    asyncio.run(erasure._saga_namespace("o", "ns", completed=set(), row={}))
    assert "procedure_runs.delete" in names
    # after the row-level tombstones, alongside the other hard deletes
    assert names.index("procedure_runs.delete") > names.index("memory.tombstone")


def test_procedural_rows_embed_situation_name_steps_not_graph_json() -> None:
    from server.memory.domain.embed_text import procedural_text_source

    value = {
        "situation": "deploy fails after release",
        "name": "rollback_deploy",
        "steps": ["revert tag", "restart replicas"],
        "graph": {"nodes": [{"id": "n1", "stats": {"runs": 40, "ok": 39}}], "edges": []},
        "run_stats": {"total": 40, "ok": 39},
        "runs": [{"path": ["n1"]}] * 50,
        "revision": 7,
    }
    text = procedural_text_source("procedural", value)
    assert text == "deploy fails after release\nrollback deploy\nrevert tag; restart replicas"
    # stable across statistics churn
    value["run_stats"] = {"total": 41, "ok": 40}
    value["revision"] = 8
    assert procedural_text_source("procedural", value) == text
    # non-procedural or malformed values fall back to the serialized value
    assert procedural_text_source("semantic", value) == ""
    assert procedural_text_source("procedural", {"name": "x"}) == ""
    assert procedural_text_source("procedural", "just a string") == ""


def test_user_erasure_saga_retires_consolidated_dependents_before_summaries() -> None:
    """The user cascade has a step for
    summaries built FROM the erased rows, ordered before the per-user summary
    tombstone so both run whatever the per-user step finds."""
    from server.erasure.app.saga_steps import USER_STEPS
    from server.erasure.store import steps_graph

    steps = dict(USER_STEPS)
    names = [name for name, _ in USER_STEPS]
    assert names.index("consolidated.tombstone") < names.index("summaries.tombstone")
    assert steps["consolidated.tombstone"] is steps_graph.tombstone_consolidated_dependents_by_user
