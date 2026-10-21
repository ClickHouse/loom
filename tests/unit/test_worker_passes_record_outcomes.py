"""Every worker pass reaches the registry through `record_pass` with its counts.

The supervisor writes the pass's `loom.worker_status` row from the outcome
`record_pass` left in the registry; a worker that records none writes a row with
zero counts and no pass metric or span, and its work (TTL alters issued, leases
expired, webhooks delivered, threads titled, profiles written) is invisible.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.workers.status import get_registry


@pytest.fixture(autouse=True)
def _fresh_registry():
    get_registry().reset()
    yield
    get_registry().reset()


def _outcome(name: str) -> dict[str, Any]:
    return dict(get_registry().snapshot()[name]["last_outcome"])


@pytest.mark.asyncio
async def test_retention_reconcile_records_the_alters_it_issued(monkeypatch) -> None:
    from server.memory.workers import retention_reconcile as w
    from server.storage import retention_overrides

    async def _apply(*_: Any, **__: Any) -> int:
        return 3

    monkeypatch.setattr(retention_overrides, "apply", _apply)
    assert (await w.run_pass()).alters_issued == 3
    assert _outcome("retention_reconcile") == {"alters_issued": 3}


@pytest.mark.asyncio
async def test_task_lease_sweep_records_its_counts(monkeypatch) -> None:
    from server.tasks.app import sweep as task
    from server.tasks.workers import lease_sweep as w

    counts = {"expired": 2, "blocked": 1, "resumed": 0, "escalated": 0, "leases": 0}

    async def _sweep(**_: Any) -> dict[str, int]:
        return dict(counts)

    monkeypatch.setattr(task, "sweep_once", _sweep)
    assert (await w.run_pass()).total == 3
    assert _outcome("task_lease_sweep") == counts


@pytest.mark.asyncio
async def test_task_webhook_deliver_records_its_counts(monkeypatch) -> None:
    from server.tasks.workers import webhook_deliver as w

    async def _deliver(**_: Any) -> w.DeliverResult:
        return w.DeliverResult(delivered=1, failed=1, skipped=0, deferred=2)

    monkeypatch.setattr(w, "deliver_once", _deliver)
    await w.run_pass()
    assert _outcome("task_webhook_deliver") == {
        "delivered": 1,
        "failed": 1,
        "skipped": 0,
        "deferred": 2,
    }


@pytest.mark.asyncio
async def test_a_raising_sweep_is_recorded_as_a_failure(monkeypatch) -> None:
    from server.tasks.app import sweep as task
    from server.tasks.workers import lease_sweep as w

    async def _down(**_: Any) -> dict[str, int]:
        raise RuntimeError("ch down")

    monkeypatch.setattr(task, "sweep_once", _down)
    with pytest.raises(RuntimeError):
        await w.run_pass()
    assert get_registry().snapshot()["task_lease_sweep"]["last_error"] == "RuntimeError: ch down"


@pytest.mark.asyncio
async def test_the_titler_records_an_outcome_even_on_the_fake_llm(monkeypatch) -> None:
    from types import SimpleNamespace

    import server.llm.client as llm_client
    from server.knowledge.workers import conversation_titler as w

    monkeypatch.setattr(llm_client, "get_llm_client", lambda: SimpleNamespace(model="fake"))
    await w.run_pass()
    assert _outcome("conversation_titler") == {"candidates": 0, "titled": 0}
