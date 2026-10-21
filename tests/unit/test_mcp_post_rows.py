"""An MCP call's post-handler rows (budget violation, audit,
idempotency) go out in one concurrent round, and a failing one never fails
the call."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from typing import Any

import pytest

import server.observability.budgets as budgets
import server.transport._common as common
from server.core import idempotency
from server.transport import post_rows


def _op(**kw: Any) -> SimpleNamespace:
    fields = {"audit_subject": None, **kw}
    return SimpleNamespace(
        name="memory.set", budget_p99_ms=10, audit=True, idempotent=True, **fields
    )


def _call(**kw: Any) -> None:
    args = dict(
        request_id="r",
        org="o",
        namespace="n",
        agent="a",
        actor="human:x",
        input_obj=SimpleNamespace(model_dump=lambda mode: {"org": "o"}),
        elapsed_ms=500,
        response_body={"ok": True},
        idem=(idempotency.Scope(key="k", org="o", subject="x"), "h"),
        replay_headers={"X-Loom-Transport": "mcp"},
    )
    args.update(kw)
    asyncio.run(post_rows.post_handler_rows(_op(), transport="mcp", **args))


@pytest.fixture()
def _slow_rows(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    done: list[str] = []

    def _row(name: str, fail: bool = False) -> Any:
        async def _write(**_: Any) -> None:
            await asyncio.sleep(0.2)
            if fail:
                raise RuntimeError(f"{name} down")
            done.append(name)

        return _write

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_budget_violation", _row("budget"))
    monkeypatch.setattr(common, "emit_op_audit", _row("audit"))
    monkeypatch.setattr(idempotency, "store", _row("idem"))
    return done


def test_the_three_rows_go_out_in_one_round(_slow_rows: list[str]) -> None:
    t = time.perf_counter()
    _call()
    assert sorted(_slow_rows) == ["audit", "budget", "idem"]
    assert time.perf_counter() - t < 0.45, "three 0.2 s writes took one round, not three"


def test_under_budget_and_without_a_key_only_the_audit_row(_slow_rows: list[str]) -> None:
    _call(elapsed_ms=1, idem=(None, None))
    assert _slow_rows == ["audit"]


def test_a_failing_row_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    async def _boom(**_: Any) -> None:
        raise RuntimeError("audit sink down")

    async def _ok(**_: Any) -> None:
        return None

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_budget_violation", _ok)
    monkeypatch.setattr(common, "emit_op_audit", _boom)
    monkeypatch.setattr(idempotency, "store", _ok)
    _call()
    assert "audit emit failed for memory.set" in caplog.text


def test_the_shared_gather_never_raises_for_either_transport(caplog) -> None:
    """A post-handler row that raises never fails a request whose handler
    succeeded, on HTTP or MCP."""

    async def _boom() -> None:
        raise RuntimeError("sink down")

    async def _ok() -> None:
        return None

    asyncio.run(
        post_rows.gather_best_effort(
            [("audit emit", _boom()), ("idempotency store", _ok())],
            op_name="memory.set",
        )
    )
    assert "http audit emit failed for memory.set" in caplog.text


def test_an_op_s_audit_subject_replaces_the_input_in_its_audit_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    subjects: list[Any] = []

    async def _audit(**kw: Any) -> None:
        subjects.append(kw["subject"])

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_op_audit", _audit)
    op = _op(audit_subject=lambda body: {"chars": 3})
    body = SimpleNamespace(model_dump=lambda mode: {"content": "secret"})
    asyncio.run(
        post_rows.post_handler_rows(
            op,
            transport="mcp",
            request_id="r",
            org="o",
            namespace="n",
            agent="a",
            actor="human:x",
            input_obj=body,
            elapsed_ms=1,
            response_body={"ok": True},
            idem=(None, None),
            replay_headers={},
        )
    )
    assert subjects == [{"chars": 3}]
