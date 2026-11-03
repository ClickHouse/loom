"""Every erasure audit row names the principal of the request's token, never the
body's free-text `requester`, which stays in the row's subject. The ledger keeps
that actor, so a saga the recovery worker resumes without a token names it too.
CH-free."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.erasure.api.erasure import ErasureRequestInput
from server.erasure.app import saga
from server.erasure.store import ledger as erasure_ledger
from server.erasure.workers import recovery
from server.operations.base import Role
from tests.unit.erasure_saga_stub import saga_runner

ADMIN = AuthInfo(
    subject="admin@o", org="o", namespaces=("A",), roles=(Role.ADMIN,), is_service_account=False
)


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> Any:
    run = saga_runner(monkeypatch)
    audits: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    async def _emit(**kw: Any) -> None:
        audits.append(kw)

    async def _insert(row: Any) -> None:
        rows.append(dict(row))

    monkeypatch.setattr(saga.audit, "emit", _emit)
    monkeypatch.setattr(erasure_ledger, "insert_request_checkpoint", _insert)
    yield run, audits, rows
    set_current_auth(None)


async def test_a_completed_saga_names_the_token_on_every_row(recorded: Any) -> None:
    run, audits, rows = recorded
    set_current_auth(ADMIN)
    await run("namespace", "A")  # the stub's request declares requester="ops"
    assert [a["event_type"] for a in audits] == ["erasure.started", "erasure.completed"]
    assert {a["actor"] for a in audits} == {"human:admin@o"}
    assert {a["subject"]["requester"] for a in audits} == {"ops"}
    assert rows and {r["actor"] for r in rows} == {"human:admin@o"}
    assert {r["requester"] for r in rows} == {"ops"}


async def test_a_failed_saga_names_the_token(
    recorded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, audits, _ = recorded

    async def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("step broke")

    monkeypatch.setattr(saga, "_saga_namespace", _boom)
    set_current_auth(ADMIN)
    inp, row, erasure_id, now = await saga.begin_erasure(
        ErasureRequestInput(org="o", scope_type="namespace", scope_value="A", requester="dpo@y")
    )
    out = await saga.run_saga(
        input=inp, row=row, erasure_id=erasure_id, requested_at=now, previously_completed=set()
    )
    assert out.status == "failed"
    assert [(a["event_type"], a["actor"]) for a in audits] == [
        ("erasure.started", "human:admin@o"),
        ("erasure.failed", "human:admin@o"),
    ]
    assert audits[1]["subject"]["requester"] == "dpo@y"


def _ledger_row(**over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "id": "e1",
        "org": "o",
        "scope_type": "namespace",
        "scope_value": "A",
        "requester": "dpo@y",
        "actor": "human:admin@o",
        "urgency": "normal",
        "purge": False,
        "attempts": 1,
        "steps_completed": [],
        "requested_at": datetime.now(UTC).replace(tzinfo=None),
        "failure_reason": "",
        "residue": "",
    }
    row.update(over)
    return row


@pytest.mark.parametrize(
    ("stored", "named"), [("human:admin@o", "human:admin@o"), ("", saga.RESUME_ACTOR)]
)
async def test_a_resumed_saga_keeps_the_ledgers_actor(
    monkeypatch: pytest.MonkeyPatch, stored: str, named: str
) -> None:
    """No token in the worker: the actor comes from the ledger row, never from
    `requester`; a row from before the ledger kept it names the worker."""
    seen: list[str] = []

    async def _run_saga(*, row: Any, **kw: Any) -> Any:
        seen.append(row["actor"])
        raise RuntimeError("still broken")

    async def _noop(**kw: Any) -> None:
        return None

    async def _checkpoint(*, row: Any, **kw: Any) -> None:
        seen.append(row["actor"])

    monkeypatch.setattr(saga, "run_saga", _run_saga)
    monkeypatch.setattr(saga, "checkpoint", _checkpoint)
    monkeypatch.setattr(recovery.audit, "emit", _noop)
    set_current_auth(None)
    await recovery._recover_one(
        row=_ledger_row(actor=stored),  # type: ignore[arg-type]
        cfg=recovery.RecoveryConfig(max_attempts=2),
        result=recovery.RecoveryResult(),
    )
    assert seen == [named, named]  # the resumed run, then the abandonment's terminal row
