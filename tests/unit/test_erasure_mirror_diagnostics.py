"""Erasure diagnostics reveal no mirror row counts to a namespace's administrator."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.auth import context
from server.auth.jwt import AuthInfo
from server.erasure.api.erasure import ErasureListInput, ErasureRequestInput, ErasureRequestOutput
from server.erasure.app import handlers
from server.knowledge.app import mirrors
from server.operations.base import Role
from tests.unit.test_erasure_audit_actor import recorded as recorded
from tests.unit.test_erasure_receipt import _status, _status_row

MIRROR = "acl-" + "a" * 24


@pytest.fixture
def registry(no_mirrors: None, monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    reads: list[list[str]] = []

    async def found(org: str, namespaces: list[str], **kw: Any) -> dict[str, str]:
        reads.append(namespaces)
        return {MIRROR: "team"} if org == "o" and "team" in namespaces else {}

    monkeypatch.setattr(mirrors, "mirrors_of", found)
    return reads


def _reader(*, platform: bool = False, scoped: bool = False) -> AuthInfo:
    return AuthInfo(
        subject="reader",
        org="o",
        namespaces=("*",),
        roles=(Role.PLATFORM_ADMIN if platform else Role.ORG_ADMIN,),
        is_service_account=False,
        ns_roles=(("team", "admin"),) if scoped else (),
    )


@pytest.mark.parametrize(
    "scope_type,scope_value",
    [
        ("namespace", "team"),
        ("namespace_list", '["team", "other"]'),
        ("namespace_list", json.dumps({"namespaces": ["team", MIRROR], "purge": True})),
    ],
)
@pytest.mark.parametrize("reader", [None, _reader(), _reader(platform=True, scoped=True)])
async def test_a_mirrored_scope_s_status_keeps_codes_only(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    scope_type: str,
    scope_value: str,
    reader: AuthInfo | None,
) -> None:
    monkeypatch.setattr(context, "current_auth", lambda: reader)
    out = await _status(
        monkeypatch,
        _status_row(
            scope_type=scope_type,
            scope_value=scope_value,
            status="failed",
            mirror_marked=True,
            residue_count=47,
            residue='{"loom.documents": 47}',
            census='{"loom.documents": 47}',
            failure_reason=f"verify.residue: ErasureResidueError: residue after erasure: {{{MIRROR!r}: 47}}",
        ),
    )
    assert out.found and out.status == "failed"
    assert (out.residue_count, out.residue, out.census) == (0, {}, None)
    assert out.verified_at is None
    assert out.failure_reason == "ERASURE_FAILED"
    assert MIRROR not in out.model_dump_json()


@pytest.mark.parametrize("namespace,platform", [("ordinary", False), ("team", True)])
async def test_an_ordinary_scope_or_platform_reader_keeps_the_receipt(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
    namespace: str,
    platform: bool,
) -> None:
    monkeypatch.setattr(context, "current_auth", lambda: _reader(platform=platform))
    out = await _status(
        monkeypatch,
        _status_row(
            scope_value=namespace,
            mirror_marked=namespace == "team",
            residue_count=47,
            residue='{"loom.documents": 47}',
            census='{"loom.documents": 47}',
        ),
    )
    assert (out.residue_count, out.residue, out.census) == (
        47,
        {"loom.documents": 47},
        {"loom.documents": 47},
    )


async def test_the_list_uses_the_persisted_mark_without_a_registry_read(
    registry: list[list[str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "current_auth", lambda: _reader())

    async def scope(*a: Any, **kw: Any) -> Any:
        return "1", {}

    async def rows(*a: Any, **kw: Any) -> Any:
        return [
            _status_row(scope_value=ns, residue_count=n, mirror_marked=ns == "team")
            for ns, n in [("team", 47), ("ordinary", 3)]
        ]

    monkeypatch.setattr(handlers, "_list_scope", scope)
    monkeypatch.setattr(handlers.erasure_ledger, "list_requests", rows)
    out = await handlers.handle_erasure_list(ErasureListInput(org="o"))
    assert [r.residue_count for r in out.rows] == [0, 3]
    assert registry == []


async def test_a_mirrored_request_keeps_no_counts_in_an_idempotent_answer(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "current_auth", lambda: _reader(platform=True))
    row = _status_row()

    async def begin(value: Any) -> Any:
        return value, {"mirror_marked": True}, row["id"], row["requested_at"]

    async def run(**kw: Any) -> ErasureRequestOutput:
        return ErasureRequestOutput(
            erasure_id=row["id"],
            status="failed",
            requested_at=row["requested_at"],
            steps=[{"step": "documents.tombstone", "rows_affected": 47}],
            residue_count=47,
            residue={"loom.documents": 47},
            census={"loom.documents": 47},
            failure_reason="verify.residue: ErasureResidueError: residue after erasure: 47",
        )

    monkeypatch.setattr(handlers, "begin_erasure", begin)
    monkeypatch.setattr(handlers, "run_saga", run)
    out = await handlers.handle_erasure_request(
        ErasureRequestInput(org="o", scope_type="namespace", scope_value="team", requester="reader")
    )
    assert (out.residue_count, out.residue, out.census) == (0, {}, None)
    assert out.steps == []
    assert out.verified_at is None
    assert out.failure_reason == "ERASURE_FAILED"


async def test_a_mirrored_saga_audits_no_step_counts_but_keeps_internal_evidence(
    recorded: Any,
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run, audits, _ = recorded

    async def forget(*a: Any, **kw: Any) -> None:
        return None

    monkeypatch.setattr(mirrors, "forget_mirrors", forget)
    await run("namespace", "team")
    raw = run.last["out"]
    assert any(step.rows_affected for step in raw.steps)
    completed = next(a["subject"] for a in audits if a["event_type"] == "erasure.completed")
    assert completed["census"] is None
    assert completed["steps"] == []


async def test_a_marked_receipt_stays_private_when_the_registry_is_lost(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unavailable(*a: Any, **kw: Any) -> Any:
        raise AssertionError("receipts must not depend on the current registry")

    monkeypatch.setattr(mirrors, "mirrors_of", unavailable)
    monkeypatch.setattr(context, "current_auth", lambda: _reader())
    out = await _status(
        monkeypatch,
        _status_row(
            mirror_marked=True,
            residue_count=47,
            census='{"loom.documents":47}',
        ),
    )
    assert out.residue_count == 0 and out.census is None


async def test_a_failed_mark_stops_the_failure_tail_before_it_counts(
    registry: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.erasure.app import saga

    events: list[str] = []

    async def unavailable(**kw: Any) -> None:
        events.append("mark")
        raise RuntimeError("store unavailable")

    async def count(*a: Any, **kw: Any) -> Any:
        events.append("count")
        raise AssertionError("no mirror count before a durable mark")

    monkeypatch.setattr(saga, "checkpoint", unavailable)
    monkeypatch.setattr(saga.erasure_census, "evidence_census", count)
    verify: dict[str, Any] = {}
    await saga._census_tail(
        "o",
        "namespace",
        "team",
        verify,
        purge=False,
        row={},
        completed=set(),
    )
    assert events == ["mark"]
    assert verify == {}


@pytest.mark.parametrize("crash", [False, True])
async def test_a_last_attempt_keeps_its_new_mark_when_it_is_abandoned(
    monkeypatch: pytest.MonkeyPatch,
    crash: bool,
) -> None:
    from server.erasure.app import saga
    from server.erasure.workers import recovery
    from tests.unit.test_erasure_audit_actor import _ledger_row

    rows: list[Any] = []
    audits: list[Any] = []

    async def run(*, row: Any, **kw: Any) -> ErasureRequestOutput:
        row["mirror_marked"] = True
        if crash:
            raise RuntimeError("stopped after marking")
        return ErasureRequestOutput(
            erasure_id=str(row["id"]),
            steps=[],
            status="failed",
            requested_at=row["requested_at"],
            residue_count=47,
            residue={"loom.documents": 47},
            census={"loom.documents": 47},
            failure_reason="verify.residue: ErasureResidueError: residue after erasure: 47",
        )

    async def checkpoint(**kw: Any) -> None:
        rows.append(kw)

    async def emit(**kw: Any) -> None:
        audits.append(kw)

    monkeypatch.setattr(saga, "run_saga", run)
    monkeypatch.setattr(saga, "checkpoint", checkpoint)
    monkeypatch.setattr(recovery.audit, "emit", emit)
    await recovery._recover_one(
        row=_ledger_row(),
        cfg=recovery.RecoveryConfig(max_attempts=2),
        result=recovery.RecoveryResult(),
    )
    assert rows[-1]["row"]["mirror_marked"]
    assert audits[-1]["subject"]["residue_count"] == 0
    if not crash:
        assert rows[-1]["extra"]["residue_count"] == 47
        assert audits[-1]["subject"]["last_error"] == "ERASURE_FAILED"
        assert "47" not in audits[-1]["subject"]["last_error"]


def test_marked_versions_survive_later_unmarked_writers_and_keep_timestamp_order() -> None:
    from server.erasure.domain.receipts import receipt_version

    marked = receipt_version(41, mirror_marked=True)
    assert marked > receipt_version(42, mirror_marked=False)
    assert receipt_version(42, mirror_marked=True) > marked
    assert receipt_version(41, mirror_marked=False) == 41


async def test_two_marked_failure_causes_have_the_same_public_receipt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(context, "current_auth", lambda: _reader())
    base = _status_row(mirror_marked=True, status="failed")
    early = await _status(
        monkeypatch,
        {
            **base,
            "failure_reason": "documents.tombstone: RuntimeError: unavailable",
            "current_step": 2,
            "residue_count": 3,
            "residue": '{"loom.documents":3}',
            "census": '{"loom.documents":3}',
        },
    )
    late = await _status(
        monkeypatch,
        {
            **base,
            "failure_reason": "verify.residue: ErasureResidueError: residue after erasure: 47",
            "current_step": 25,
            "residue_count": 47,
            "residue": '{"loom.chunks":47}',
            "census": '{"loom.chunks":47}',
        },
    )
    assert early.model_dump() == late.model_dump()
    assert late.failure_reason == "ERASURE_FAILED" and late.current_step == 0
