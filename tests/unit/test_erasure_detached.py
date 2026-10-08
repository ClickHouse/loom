"""A saga can be registered and run detached — the caller gets
the erasure id at once, the saga checkpoints as usual, and a task nothing
else holds is kept referenced until it ends. `erasure.request` keeps its
inline shape and its admin gate. CH-free."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import server.erasure.app.handlers as handlers
import server.erasure.app.saga as h
from server.erasure.api.erasure import ErasureRequestInput


@pytest.fixture
def ledger(monkeypatch):
    rows: list[dict[str, Any]] = []
    audits: list[dict[str, Any]] = []

    async def _insert(row):
        rows.append(dict(row))

    async def _emit(**kw):
        audits.append(kw)

    monkeypatch.setattr(h.erasure_ledger, "insert_request_checkpoint", _insert)
    monkeypatch.setattr(h.audit, "emit", _emit)
    return rows, audits


def _req(**over: Any) -> ErasureRequestInput:
    base: dict[str, Any] = {
        "org": "o",
        "scope_type": "namespace_list",
        "scope_value": '{"namespaces": ["code-widgets"], "purge": true, "external": false}',
        "requester": "alice@x",
        "purge": True,
        "external": False,
    }
    base.update(over)
    return ErasureRequestInput(**base)


@pytest.mark.asyncio
async def test_begin_registers_the_saga_and_keeps_the_flags_in_the_scope(ledger) -> None:
    rows, audits = ledger
    inp, row, erasure_id, now = await h.begin_erasure(_req())
    assert len(erasure_id) == 36 and row["id"] == erasure_id and row["attempts"] == 1
    assert rows and rows[0]["status"] == "in_progress" and rows[0]["steps_completed"] == []
    assert inp.purge and not inp.external
    assert '"purge": true' in inp.scope_value and '"external": false' in inp.scope_value
    (a,) = audits
    assert a["event_type"] == "erasure.started" and a["request_id"] == erasure_id
    assert a["actor"] == "alice@x"


@pytest.mark.asyncio
async def test_detached_returns_at_once_and_runs_the_saga_in_the_background(
    ledger, monkeypatch
) -> None:
    ran: list[tuple[str, str, set[str]]] = []
    gate = asyncio.Event()

    async def _saga(*, input, row, erasure_id, requested_at, previously_completed):
        await gate.wait()
        ran.append((erasure_id, input.scope_type, set(previously_completed)))
        return "done"

    monkeypatch.setattr(h, "run_saga", _saga)
    erasure_id = await h.run_erasure_detached(_req())
    assert ran == []  # the caller did not wait
    assert any(t.get_name() == f"erasure-{erasure_id[:8]}" for t in h._DETACHED)
    gate.set()
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ran == [(erasure_id, "namespace_list", set())]
    assert not any(t.get_name() == f"erasure-{erasure_id[:8]}" for t in h._DETACHED)


@pytest.mark.asyncio
async def test_a_detached_saga_that_raises_is_logged_not_lost(ledger, monkeypatch, caplog) -> None:
    async def _boom(**kw):
        raise RuntimeError("mutation refused")

    monkeypatch.setattr(h, "run_saga", _boom)
    erasure_id = await h.run_erasure_detached(_req())
    for _ in range(3):
        await asyncio.sleep(0)
    assert not any(t.get_name() == f"erasure-{erasure_id[:8]}" for t in h._DETACHED)
    assert any("detached saga ended with RuntimeError" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_erasure_request_still_runs_inline_through_begin(ledger, monkeypatch) -> None:
    seen: list[str] = []

    async def _saga(*, input, row, erasure_id, requested_at, previously_completed):
        seen.append(erasure_id)
        return "inline"

    monkeypatch.setattr(handlers, "run_saga", _saga)
    monkeypatch.setattr(handlers, "current_auth", lambda: None, raising=False)
    from server.auth.context import set_current_auth

    set_current_auth(None)  # auth-off: the admin gate is skipped, the saga runs inline
    out = await handlers.handle_erasure_request(_req())
    assert out == "inline" and len(seen) == 1
