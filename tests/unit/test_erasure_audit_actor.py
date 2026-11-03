"""The erasure audit rows name the principal of the request's token, not the body's
free-text `requester`, which goes in the row's subject. The ledger keeps
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
from server.memory.domain.provenance import pipeline_actor
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


async def test_a_fresh_saga_with_no_actor_at_all_names_no_one_on_every_row(recorded: Any) -> None:
    """Auth off, no pipeline actor, an empty `requester`: every row names `-`, never
    the recovery worker that has not run."""
    _, audits, rows = recorded
    set_current_auth(None)
    inp, row, erasure_id, now = await saga.begin_erasure(
        ErasureRequestInput(org="o", scope_type="namespace", scope_value="A", requester="")
    )
    await saga.run_saga(
        input=inp, row=row, erasure_id=erasure_id, requested_at=now, previously_completed=set()
    )
    assert [(a["event_type"], a["actor"]) for a in audits] == [
        ("erasure.started", "-"),
        ("erasure.completed", "-"),
    ]
    assert {r["actor"] for r in rows} == {"-"}


CLI = "process:cli:erasure-re-verify"


def _resume_args(stored: str) -> dict[str, Any]:
    row = _ledger_row(scope_type="user_id", scope_value="u-42", actor=stored)
    inp = saga.input_from_ledger_row(row)
    return {
        "input": inp,
        "row": saga.request_row_template(
            erasure_id="e1",
            input=inp,
            actor=row["actor"],
            requested_at=row["requested_at"],
            attempts=2,
        ),
        "erasure_id": "e1",
        "requested_at": row["requested_at"],
        "previously_completed": set(),
    }


@pytest.mark.parametrize(
    ("stored", "outer", "revoked_by", "completed_by"),
    [
        ("human:admin@o", "", "human:admin@o", "human:admin@o"),  # the worker
        ("", "", "-", saga.RECOVERY_WORKER_ACTOR),  # the worker, a row an older build wrote
        ("human:admin@o", CLI, "human:admin@o", "human:admin@o"),  # the CLI
        ("", CLI, CLI, CLI),  # the CLI, a row an older build wrote
    ],
)
async def test_a_tokenless_resume_names_one_actor_on_its_grant_revocations_and_rows(
    recorded: Any,
    monkeypatch: pytest.MonkeyPatch,
    stored: str,
    outer: str,
    revoked_by: str,
    completed_by: str,
) -> None:
    """A step's own audit rows (the user's grants revoked) name the ledger's actor
    on a tokenless resume. A row an older build wrote binds nothing, so an outer
    pipeline actor (the CLI's) still names them, and its `erasure.completed` row."""
    from server.identity.app import grants

    _, audits, _ = recorded
    actors: list[str] = []

    async def _revoke(org: str, principal: str, *, actor: str) -> int:
        actors.append(actor)
        return 0

    async def _none(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(grants, "revoke_subject", _revoke)
    monkeypatch.setattr(grants, "forget_granter", _none)
    set_current_auth(None)
    with pipeline_actor(outer):
        await saga.run_saga(**_resume_args(stored))
    assert actors == [revoked_by]
    assert [(a["event_type"], a["actor"]) for a in audits] == [("erasure.completed", completed_by)]


async def test_a_cli_resume_that_fails_names_the_cli_on_a_row_with_no_actor(
    recorded: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`erasure.failed` is written inside the steps' binding: it names the CLI too."""
    _, audits, _ = recorded

    async def _boom(*a: Any, **kw: Any) -> Any:
        raise RuntimeError("step broke")

    monkeypatch.setattr(saga, "_saga_user_id", _boom)
    set_current_auth(None)
    with pipeline_actor(CLI):
        out = await saga.run_saga(**_resume_args(""))
    assert out.status == "failed"
    assert [(a["event_type"], a["actor"]) for a in audits] == [("erasure.failed", CLI)]


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


@pytest.mark.parametrize("stored", ["human:admin@o", ""])
async def test_a_resume_carries_the_ledgers_actor_forward_unchanged(
    monkeypatch: pytest.MonkeyPatch, stored: str
) -> None:
    """No token in the worker: the resumed run and the abandonment's terminal row
    carry the ledger's actor as it is, never `requester` and never the worker's
    own name, so a later resume by another process still names that process."""
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
    assert seen == [stored, stored]  # the resumed run, then the abandonment's terminal row


@pytest.mark.parametrize(
    ("stored", "named"), [("human:admin@o", "human:admin@o"), ("", saga.RECOVERY_WORKER_ACTOR)]
)
async def test_a_resumed_saga_that_completes_names_the_ledgers_actor(
    recorded: Any, stored: str, named: str
) -> None:
    """The real saga, resumed by the worker with no token: its `erasure.completed`
    row names the ledger's actor (the worker when the row has none), the worker's
    own `erasure.recovered` row the worker. The ledger keeps what it held."""
    _, audits, rows = recorded
    set_current_auth(None)
    await recovery._recover_one(
        row=_ledger_row(actor=stored),  # type: ignore[arg-type]
        cfg=recovery.RecoveryConfig(max_attempts=5),
        result=recovery.RecoveryResult(),
    )
    assert [(a["event_type"], a["actor"]) for a in audits] == [
        ("erasure.completed", named),
        ("erasure.recovered", saga.RECOVERY_WORKER_ACTOR),
    ]
    assert audits[0]["subject"]["requester"] == "dpo@y"
    assert rows and {r["actor"] for r in rows} == {stored}


def test_audit_actor_falls_back_to_the_pipeline_actor_when_the_row_has_none() -> None:
    """Under a bound pipeline actor (what every CLI command runs under): a ledger
    row with an actor keeps it, one without names the pipeline, never `requester`."""
    with pipeline_actor("process:cli:erasure-re-verify"):
        assert saga.audit_actor(_ledger_row()) == "human:admin@o"
        assert saga.audit_actor(_ledger_row(actor="")) == "process:cli:erasure-re-verify"
