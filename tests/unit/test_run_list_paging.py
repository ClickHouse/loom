"""run.list pages, sorts and searches every run of a namespace, and counts the matches.

The Runs page reads `total` to say "1–50 of 1,234" and pages with `offset`; the
order is deterministic (run_id breaks ties) so a page boundary never repeats or
skips a run. The count query carries the list query's WHERE, filter for filter.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from server.tasks.api.models_run import RunListInput
from server.tasks.store import runs as runs_store


class StubCH:
    """Answers query_all with `rows` and query_one with `count`; records every call."""

    def __init__(self, rows: list[dict[str, Any]] | None = None, count: int = 0) -> None:
        self.rows = rows or []
        self.count = count
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.calls.append(("all", sql, params or {}))
        return self.rows

    async def query_one(self, sql, params=None):
        self.calls.append(("one", sql, params or {}))
        return {"n": self.count}


def _row(run_id: str) -> dict[str, Any]:
    return {
        "org": "o",
        "namespace": "n",
        "agent": "claude",
        "run_id": run_id,
        "goal": "fix CI",
        "status": "completed",
        "metadata": "{}",
    }


def _where(sql: str) -> str:
    body = sql.split("WHERE", 1)[1]
    for end in ("ORDER BY", "LIMIT"):
        body = body.split(end, 1)[0]
    return " ".join(body.split())


def _list(ch: StubCH, **kw: Any):
    from server.tasks.app import run as h

    with patch.object(h, "get_client", return_value=ch):
        return asyncio.run(h.handle_run_list(RunListInput(org="o", namespace="n", **kw)))


def test_defaults_keep_todays_page_and_add_the_total() -> None:
    ch = StubCH(rows=[_row("r2"), _row("r1")], count=1234)
    out = _list(ch)
    assert [r.run_id for r in out.runs] == ["r2", "r1"]
    assert out.total == 1234
    (_, list_sql, list_params), (_, count_sql, count_params) = ch.calls
    assert "ORDER BY started_at DESC, run_id DESC" in list_sql
    assert "OFFSET {off:UInt32}" in list_sql
    assert list_params == {"org": "o", "ns": "n", "lim": 50, "off": 0}
    assert "count()" in count_sql and "FROM loom.agent_runs FINAL" in count_sql
    assert "ORDER BY" not in count_sql and "LIMIT" not in count_sql
    assert count_params == {"org": "o", "ns": "n"}


@pytest.mark.parametrize(
    ("order_by", "clause"),
    [
        ("started_desc", "ORDER BY started_at DESC, run_id DESC"),
        ("started_asc", "ORDER BY started_at ASC, run_id ASC"),
        ("duration_desc", "ORDER BY duration_ms DESC, started_at DESC, run_id DESC"),
        ("duration_asc", "ORDER BY duration_ms ASC, started_at ASC, run_id ASC"),
        ("relied_on_desc", "ORDER BY length(memories_used) DESC, started_at DESC, run_id DESC"),
        ("relied_on_asc", "ORDER BY length(memories_used) ASC, started_at ASC, run_id ASC"),
    ],
)
def test_every_order_ends_on_the_run_id(order_by: str, clause: str) -> None:
    ch = StubCH()
    _list(ch, order_by=order_by, offset=100, limit=50)
    _, sql, params = ch.calls[0]
    assert clause in sql
    assert params["off"] == 100 and params["lim"] == 50


def test_q_matches_goal_run_id_and_agent_in_any_case_and_the_count_agrees() -> None:
    ch = StubCH(count=3)
    out = _list(
        ch,
        q="Deploy",
        agent="claude",
        session_id="s1",
        status="failed",
        memory_activity="with",
        grade="fail",
    )
    assert out.total == 3
    (_, list_sql, list_params), (_, count_sql, count_params) = ch.calls
    where = _where(list_sql)
    assert "positionCaseInsensitiveUTF8(goal, {q:String}) > 0" in where
    assert "positionCaseInsensitiveUTF8(run_id, {q:String}) > 0" in where
    assert "positionCaseInsensitiveUTF8(agent, {q:String}) > 0" in where
    assert list_params["q"] == "Deploy"
    # Filter for filter, the count reads the rows the list pages through.
    assert _where(count_sql) == where
    assert count_params == {k: v for k, v in list_params.items() if k not in ("lim", "off")}


def test_a_blank_q_is_no_filter() -> None:
    ch = StubCH()
    _list(ch, q="   ")
    _, sql, params = ch.calls[0]
    assert "q" not in params and "positionCaseInsensitive" not in sql


def test_input_bounds() -> None:
    for bad in (
        {"offset": -1},
        {"offset": 100_001},
        {"order_by": "goal_asc"},
        {"q": "x" * 201},
    ):
        with pytest.raises(ValidationError):
            RunListInput(org="o", namespace="n", **bad)


def test_store_count_reads_zero_when_no_row_answers() -> None:
    class NoRow(StubCH):
        async def query_one(self, sql, params=None):
            return None

    n = asyncio.run(runs_store.count_runs(NoRow(), "o", "n", where=[], params={}))
    assert n == 0


def test_store_refuses_an_unknown_order() -> None:
    """The order is written into the statement, so only a LIST_ORDERS key gets that far."""
    with pytest.raises(KeyError):
        asyncio.run(
            runs_store.list_runs(
                StubCH(), "o", "n", where=[], params={}, limit=1, order_by="goal_asc"
            )
        )


def test_an_n_minus_1_replica_serves_the_new_inputs_and_names_them() -> None:
    """A replica a release behind has no offset / order_by / q: it serves its
    default page (run.list is a read, so no close match is refused) and names the
    keys in `ignored_args`. This build reads all three."""
    from types import SimpleNamespace

    from pydantic import create_model

    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport.arg_check import ignored_block, refusal

    new = ("offset", "order_by", "q")
    op = OPERATIONS_BY_NAME["run.list"]
    assert op.read_only
    body = {"org": "o", "namespace": "n", "offset": 50, "order_by": "started_asc", "q": "ci"}
    assert ignored_block(op, body) == {}

    old_fields = {
        name: (info.annotation, info)
        for name, info in RunListInput.model_fields.items()
        if name not in new
    }
    old_model = create_model("RunListInputN1", **old_fields)  # type: ignore[call-overload]
    old_op = SimpleNamespace(name="run.list", input_model=old_model, read_only=True)
    block = ignored_block(old_op, body)
    assert block["ignored_args"] == sorted(new)
    assert refusal(old_op, block) is None
