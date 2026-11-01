"""A recording ClickHouse stub and a runner for whole erasure sagas: every count
answers one live row, so every step reaches every mutation it can issue."""

from __future__ import annotations

import contextvars
import json
import re
from typing import Any

import pytest

from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import saga
from server.erasure.store import census as erasure_census
from server.erasure.store import ledger as erasure_ledger

_STEP: contextvars.ContextVar[str] = contextvars.ContextVar("step", default="")
_ALTER = re.compile(r"ALTER TABLE\s+(\S+)\s+(UPDATE|DELETE)\b")
_LIGHT = re.compile(r"DELETE FROM\s+(\S+)")


class _AnyRow(dict[str, Any]):
    """A read row with a value in every column."""

    def __missing__(self, key: str) -> str | int:
        return 1 if key == "h" else "x"  # `h` is a cityHash64


class _TraceCH:
    """One live row per count, one row for each scope read a step mutates by; every
    mutation is done at the first poll."""

    def __init__(self, trace: dict[str, list[tuple[str, str]]]) -> None:
        self.trace = trace
        # (step, table): a step's INSERT ... SELECT rewrite makes its rows stop matching.
        self.rewritten: set[tuple[str, str]] = set()
        self.executed: list[str] = []  # every statement sent to execute, in order

    async def query_one(self, sql: str, params: Any = None, **kw: Any) -> Any:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        if " AS keys" in sql:
            return {"keys": ["k"]}
        if "count()" not in sql:
            return None
        done = any(f"FROM {t} " in sql for step, t in self.rewritten if step == _STEP.get())
        return {"n": 0 if done else 1}

    async def query_all(self, sql: str, params: Any = None, **kw: Any) -> list[dict[str, Any]]:
        if "system.mutations" in sql:
            if "is_done = 0" in sql:
                return []
            return [{"mutation_id": "m", "is_done": 1, "parts_to_do": 0, "latest_fail_reason": ""}]
        if sql.startswith("SELECT group_id FROM loom.principal_groups"):
            return [{"group_id": "g"}]
        if sql.startswith(("SELECT DISTINCT", "SELECT namespace,", "SELECT cityHash64")):
            return [_AnyRow()]
        if "blob_path" in sql and "loom.documents" in sql:
            return [{"source_uri": "doc://a", "blob_path": "o/a/blob"}]
        return []

    async def execute(self, sql: str, params: Any = None, **kw: Any) -> None:
        if m := _ALTER.search(sql):
            entry = (m.group(2), m.group(1))
        elif m := _LIGHT.search(sql):
            entry = ("LIGHTWEIGHT", m.group(1))
        else:  # an INSERT ... SELECT rewrite: its rows stop matching the step's count
            entry = ("OTHER", sql.split()[0])
            if sql.startswith("INSERT INTO "):
                self.rewritten.add((_STEP.get(), sql.split()[2]))
        self.trace.setdefault(_STEP.get(), []).append(entry)
        self.executed.append(sql)


def saga_runner(monkeypatch: pytest.MonkeyPatch) -> Any:
    """`run(scope_type, scope_value, purge=False)`: the saga's trace of (step, statements).
    `run.ch.executed` holds every statement, `run.last["out"]` the saga's result."""
    trace: dict[str, list[tuple[str, str]]] = {}
    order: list[str] = []
    last: dict[str, Any] = {}
    ch = _TraceCH(trace)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)

    async def _noop(*a: Any, **kw: Any) -> None:
        return None

    async def _clean(*a: Any, **kw: Any) -> dict[str, int]:
        return {}

    monkeypatch.setattr(erasure_ledger, "insert_request_checkpoint", _noop)
    monkeypatch.setattr("server.observability.audit.emit", _noop)
    monkeypatch.setattr("server.core.namespace_write_marks.bump", _noop)
    monkeypatch.setattr(erasure_census, "residue_census", _clean)

    real_do_step = saga._do_step

    async def _traced(*, step_name: str, **kw: Any) -> Any:
        order.append(step_name)
        token = _STEP.set(step_name)
        try:
            return await real_do_step(step_name=step_name, **kw)
        finally:
            _STEP.reset(token)

    monkeypatch.setattr(saga, "_do_step", _traced)

    async def _run(
        scope_type: str, scope_value: str, purge: bool = False
    ) -> list[tuple[str, tuple[Any, ...]]]:
        order.clear()
        trace.clear()
        ch.executed.clear()
        inp = ErasureRequestInput(
            org="o", scope_type=scope_type, scope_value=scope_value, requester="ops", purge=purge
        )
        inp, row, erasure_id, now = await saga.begin_erasure(inp)
        out = await saga.run_saga(
            input=inp,
            row=row,
            erasure_id=erasure_id,
            requested_at=now,
            previously_completed=set(),
        )
        assert out.status == "completed", out.failure_reason
        assert [s.step for s in out.steps] == order
        last["out"] = out
        return [(name, tuple(trace.get(name, ()))) for name in order]

    _run.last = last  # type: ignore[attr-defined]
    _run.ch = ch  # type: ignore[attr-defined]
    return _run


def ns_list(purge: bool, external: bool) -> str:
    """A namespace_list scope value."""
    return json.dumps({"namespaces": ["a", "b"], "purge": purge, "external": external})
