"""Run.grade writes a verdict onto closed runs, carries every
column, never writes usage events, and hands the relied-on keys to the
owner's proposal policy."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pydantic import ValidationError

from server.tasks.api.models_run import RunGradeInput, RunListInput
from server.tasks.app import run as run_handlers
from server.tasks.store import runs as runs_store


def _run(
    rid: str, status: str = "completed", used: list[str] | None = None, **kw: Any
) -> dict[str, Any]:
    row = {c: "" for c in runs_store.RUN_COLUMNS}
    row.update(
        org="o",
        namespace="n",
        run_id=rid,
        status=status,
        memories_used=used or [],
        graded_at=None,
        completed_at=None,
        started_at=None,
    )
    row.update(kw)
    return row


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows, self.inserts, self.sql = rows, [], []

    async def query_all(self, sql: str, params: Any = None) -> list[dict[str, Any]]:
        self.sql.append((sql, params))
        return self.rows

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        assert table == "loom.agent_runs"
        self.inserts.append(rows)


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    from server.memory.app import proposals

    seen: dict[str, Any] = {"policy": []}

    async def _apply(org: str, ns: str, keys: list[str], *, ch: Any = None) -> list[Any]:
        seen["policy"].append(keys)
        return [("lesson_a", "accept", proposals.GradedCounts(passes=2))]

    monkeypatch.setattr(proposals, "apply_policy", _apply)
    return seen


def _grade(ch: _CH, monkeypatch: pytest.MonkeyPatch, **kw: Any) -> Any:
    monkeypatch.setattr(run_handlers, "get_client", lambda: ch)
    return asyncio.run(run_handlers.handle_run_grade(RunGradeInput(org="o", namespace="n", **kw)))


def test_session_grade_skips_active_runs_and_carries_every_column(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = _CH([_run("r1", used=["lesson_a", "fact"], goal="g1"), _run("r2", status="active")])
    out = _grade(ch, monkeypatch, session_id="pr_1", grade="pass", source="maintainer", ref="f1")
    assert (out.graded, out.skipped_active, out.memories_counted) == (1, 1, 2)
    (written,) = ch.inserts[0]
    assert set(written) == set(runs_store.RUN_COLUMNS)  # nothing dropped under RMT(version)
    assert (written["grade"], written["grade_source"], written["grade_ref"], written["goal"]) == (
        "pass",
        "maintainer",
        "f1",
        "g1",
    )
    assert written["graded_at"] is not None
    assert env["policy"] == [["fact", "lesson_a"]]
    assert [(d.memory_key, d.decision, d.passes) for d in out.proposals_decided] == [
        ("lesson_a", "accept", 2)
    ]
    sql, params = ch.sql[0]
    assert "session_id = {sid:String}" in sql and params["sid"] == "pr_1"


def test_the_same_verdict_writes_nothing_but_still_applies_the_policy(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    ch = _CH(
        [
            _run(
                "r1",
                used=["lesson_a"],
                grade="pass",
                grade_source="ci",
                grade_reason="",
                grade_ref="",
            )
        ]
    )
    out = _grade(ch, monkeypatch, run_id="r1", grade="pass", source="ci")
    assert (out.graded, out.unchanged) == (0, 1) and ch.inserts == []
    assert env["policy"] == [["lesson_a"]]  # an owner who turned auto on gets the decision


def test_grade_writes_no_usage_events(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo

    def _no(*a: Any, **k: Any) -> Any:
        raise AssertionError("run.grade must not write usage events")

    monkeypatch.setattr(memory_repo, "record_usage_event", _no, raising=False)
    _grade(_CH([_run("r1", used=["k"])]), monkeypatch, run_id="r1", grade="fail")


def test_unknown_target_is_not_found(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
    from server.core.errors import NotFoundError

    with pytest.raises(NotFoundError):
        _grade(_CH([]), monkeypatch, run_id="nope", grade="pass")


def test_exactly_one_target_and_a_verdict() -> None:
    with pytest.raises(ValidationError):
        RunGradeInput(org="o", namespace="n", grade="pass")
    with pytest.raises(ValidationError):
        RunGradeInput(org="o", namespace="n", run_id="r", session_id="s", grade="pass")
    with pytest.raises(ValidationError):
        RunGradeInput(org="o", namespace="n", run_id="r", grade="maybe")  # type: ignore[arg-type]


def test_every_run_write_carries_the_grade_columns() -> None:
    row = runs_store.run_row({c: "x" for c in runs_store.RUN_COLUMNS if not c.startswith("grade")})
    assert row["grade"] == "" and row["graded_at"] is None
    kept = run_handlers._grade_columns({"grade": "pass", "grade_source": "ci", "graded_at": "t"})
    assert (kept["grade"], kept["grade_source"], kept["graded_at"]) == ("pass", "ci", "t")
    assert RunListInput(org="o", namespace="n", grade="ungraded").grade == "ungraded"
