"""An op that declares `audit` with no `audit_subject` gets an audit row whose subject
names what the call acted on and hashes every text value: `loom.audit_log` is not
covered by erasure, so it must never hold memory content."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, date, datetime
from enum import StrEnum
from typing import Any, Literal

import pytest
from pydantic import BaseModel

import server.observability.budgets as budgets
import server.transport._common as common
from server.core.hashing import served_content_sha256
from server.identity.api.models import (
    NamespaceRestoreInput,
    NamespaceSnapshotOutput,
    _SnapshotMemoryRow,
)
from server.identity.api.models_grants import GrantsShareAgentInput
from server.memory.api.models_apply import MemoryApplyInput
from server.memory.api.models_bulk import MemoryImportCuratedInput
from server.memory.api.models_crud import MemoryDeleteVersionInput, MemoryVerifyInput
from server.memory.api.models_decision import DecisionRecordInput
from server.memory.api.models_move import MemoryMoveInput
from server.memory.api.models_records import MemoryRecordWriteInput
from server.memory.api.models_webhook import WebhookRegisterInput
from server.observability.audit import input_subject
from server.operations.registry import OPERATIONS_BY_NAME
from server.transport import post_rows

SECRET = "the merger closes on 12 March at 40 dollars a share"


def _digest(value: Any) -> dict[str, Any]:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    size = (
        len(value)
        if isinstance(value, str)
        else len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    )
    return {"sha256": hashlib.sha256(text.encode()).hexdigest(), "chars": size}


def _audited_subject(monkeypatch: pytest.MonkeyPatch, op_name: str, body: Any) -> dict[str, Any]:
    subjects: list[dict[str, Any]] = []

    async def _audit(**kw: Any) -> None:
        subjects.append(kw["subject"])

    monkeypatch.setattr(budgets, "effective_budget_ms", lambda op, b: b)
    monkeypatch.setattr(common, "emit_op_audit", _audit)
    asyncio.run(
        post_rows.post_handler_rows(
            OPERATIONS_BY_NAME[op_name],
            transport="http",
            request_id="r",
            org="o",
            namespace="n",
            agent="a",
            actor="human:x",
            input_obj=body,
            elapsed_ms=1,
            response_body={},
            idem=(None, None),
            replay_headers={},
        )
    )
    assert len(subjects) == 1
    return subjects[0]


def test_decision_record_audit_row_holds_no_decision_text(monkeypatch) -> None:
    body = DecisionRecordInput(
        org="o",
        namespace="n",
        agent="a",
        category="finance",
        scenario=SECRET,
        reasoning=SECRET,
        outcome=SECRET,
        alternatives=[SECRET],
        decision_maker="cfo",
        confidence=0.75,
    )
    subject = _audited_subject(monkeypatch, "decision.record", body)
    assert SECRET not in json.dumps(subject)
    assert subject["scenario"] == _digest(SECRET)
    assert subject["alternatives"] == _digest([SECRET])
    # Who decided names a principal, as `grantee` or `approver` do.
    assert subject["decision_maker"] == "cfo"
    assert (subject["org"], subject["namespace"], subject["agent"]) == ("o", "n", "a")
    assert subject["confidence"] == 0.75
    # A Literal field is a vocabulary word, not content.
    assert subject["stage"] == "decided"


def test_memory_move_audit_row_keeps_the_keys_and_hashes_the_value(monkeypatch) -> None:
    body = MemoryMoveInput(
        org="o", namespace="n", agent="a", from_key="k1", to_key="k2", value=SECRET
    )
    subject = _audited_subject(monkeypatch, "memory.move", body)
    assert SECRET not in json.dumps(subject)
    assert (subject["from_key"], subject["to_key"]) == ("k1", "k2")
    assert subject["value"] == _digest(SECRET)
    # The hash is the one memory.get serves as the moved row's content_sha256.
    assert subject["value"]["sha256"] == served_content_sha256(SECRET)


@pytest.mark.parametrize("value", [185_000, 0.25, True])
def test_a_number_in_a_field_typed_any_is_hashed(monkeypatch, value: Any) -> None:
    body = MemoryMoveInput(
        org="o", namespace="n", agent="a", from_key="k1", to_key="pay", value=value
    )
    subject = _audited_subject(monkeypatch, "memory.move", body)
    assert subject["value"] == _digest(value)


def test_import_curated_audit_row_holds_no_document(monkeypatch) -> None:
    body = MemoryImportCuratedInput(
        org="o", namespace="n", content=f"## Deal\n{SECRET}\n", source_uri="file://a.md"
    )
    subject = _audited_subject(monkeypatch, "memory.import_curated", body)
    assert SECRET not in json.dumps(subject)
    assert subject["source_uri"] == "file://a.md"
    # Empty values carry nothing to hide and stay as they are.
    assert subject["extra_tags"] == []


def test_an_admin_op_audit_row_names_its_principals(monkeypatch) -> None:
    body = GrantsShareAgentInput(org="o", namespace="n", grantee="ci-bot", role="writer")
    subject = _audited_subject(monkeypatch, "grants.share_agent", body)
    assert (subject["grantee"], subject["role"]) == ("ci-bot", "writer")


def test_memory_apply_audit_row_keeps_the_keys_it_named(monkeypatch) -> None:
    body = MemoryApplyInput(
        org="o",
        namespace="n",
        agent="a",
        target={"keys": ["k1", "k2"]},
        action="delete",
        reason=SECRET,
        dry_run=False,
    )
    subject = _audited_subject(monkeypatch, "memory.apply", body)
    assert subject["target"]["keys"] == ["k1", "k2"]
    assert (subject["action"], subject["dry_run"]) == ("delete", False)
    assert subject["reason"] == _digest(SECRET)


def test_a_free_form_dict_is_hashed_whole(monkeypatch) -> None:
    body = MemoryRecordWriteInput(
        org="o",
        namespace="n",
        schema_name="patient",
        memory_key="p1",
        fields={"patient_id": {"value": "P-123"}, "diagnosis": {"value": SECRET}},
    )
    subject = _audited_subject(monkeypatch, "memory.record.write", body)
    dumped = json.dumps(subject)
    assert SECRET not in dumped and "P-123" not in dumped
    assert (subject["schema_name"], subject["memory_key"]) == ("patient", "p1")


_SNAPSHOT_ID = "00000000-0000-4000-8000-0000000000aa"


def test_a_restored_snapshot_keeps_its_ids_and_counts_and_hashes_its_rows(monkeypatch) -> None:
    row = _SnapshotMemoryRow(
        memory_id="00000000-0000-4000-8000-000000000001",
        agent="a",
        user_id="-",
        session_id="-",
        run_id="-",
        memory_key="k",
        memory_type="fact",
        value=SECRET,
        confidence=1.0,
        source_trace="",
        pinned=False,
    )
    payload = NamespaceSnapshotOutput(
        snapshot_id=_SNAPSHOT_ID,
        org="o",
        source_namespace="src",
        created_at=datetime(2026, 1, 1, tzinfo=UTC),
        memory_count=1,
        type_count=0,
        grant_count=0,
        audit_tail_count=0,
        include_embeddings=False,
        memories=[row],
        types=[],
        grants=[],
        audit_tail=[],
    )
    body = NamespaceRestoreInput(org="o", target_namespace="dst", payload=payload)
    subject = _audited_subject(monkeypatch, "namespace.restore", body)
    assert SECRET not in json.dumps(subject)
    shaped = subject["payload"]
    assert (shaped["snapshot_id"], shaped["source_namespace"], shaped["memory_count"]) == (
        _SNAPSHOT_ID,
        "src",
        1,
    )
    assert shaped["memories"] == _digest([row.model_dump(mode="json")])


def test_a_verify_audit_row_names_its_attester(monkeypatch) -> None:
    body = MemoryVerifyInput(org="o", memory_id="m1", actor="human:alice")
    subject = _audited_subject(monkeypatch, "memory.verify", body)
    assert (subject["memory_id"], subject["actor"]) == ("m1", "human:alice")


def test_a_credential_named_field_is_hashed_whatever_its_suffix() -> None:
    class Body(BaseModel):
        api_key: str
        signing_key: str
        db_password_id: str
        secret_name: str
        refresh_token_id: str
        session_token_key: str
        item_key: str

    credentials = ("api_key", "signing_key", "db_password_id", "secret_name")
    credentials += ("refresh_token_id", "session_token_key")
    body = Body(item_key="k", **dict.fromkeys(credentials, SECRET))
    subject = input_subject(body)
    assert SECRET not in json.dumps(subject)
    assert {name: subject[name] for name in credentials} == dict.fromkeys(
        credentials, _digest(SECRET)
    )
    assert subject["item_key"] == "k"


def test_an_enum_and_an_optional_nested_model_are_shaped_by_their_type() -> None:
    class Colour(StrEnum):
        RED = "red"

    class Inner(BaseModel):
        item_key: str
        note: str

    class Body(BaseModel):
        colour: Colour
        inner: Inner | None = None
        stages: list[Literal["a", "b"]] = []
        until: datetime | None = None
        on: date

    body = Body(
        colour=Colour.RED,
        inner=Inner(item_key="k", note=SECRET),
        stages=["a"],
        until=datetime(2026, 9, 12, 8, 30, tzinfo=UTC),
        on=date(2026, 9, 12),
    )
    assert input_subject(body) == {
        "colour": "red",
        "inner": {"item_key": "k", "note": _digest(SECRET)},
        "stages": ["a"],
        "until": "2026-09-12T08:30:00Z",
        "on": "2026-09-12",
    }


@pytest.mark.parametrize(
    ("url", "host"),
    [
        ("https://hooks.example.com:8443/h/the-token", "https://hooks.example.com:8443"),
        ("http://[2001:db8::1]:9000/h/the-token", "http://[2001:db8::1]:9000"),
    ],
)
def test_a_webhook_host_keeps_its_port_and_ipv6_brackets(monkeypatch, url: str, host: str) -> None:
    body = WebhookRegisterInput(
        org="o", namespace="n", url=url, secret_ref="LOOM_WEBHOOK_SECRET_O__CI"
    )
    subject = _audited_subject(monkeypatch, "webhook.register", body)
    assert subject["url_host"] == host
    assert "the-token" not in json.dumps(subject)


def test_a_deferred_delete_version_writes_the_ops_audit_row(monkeypatch) -> None:
    from server.memory.app import handlers
    from server.observability import audit

    rows: list[tuple[str, dict[str, Any]]] = []

    async def _emit(event_type: str, **kw: Any) -> None:
        rows.append((event_type, kw))

    monkeypatch.setattr(audit, "emit", _emit)
    body = MemoryDeleteVersionInput(
        org="o", namespace="n", agent="a", memory_key="k", version="1700000000000000001"
    )
    asyncio.run(handlers._audit_deferred(body))
    [(event_type, kw)] = rows
    assert event_type == "memory.delete_version"
    assert (kw["org"], kw["namespace"], kw["actor"]) == ("o", "n", "anonymous")
    assert kw["subject"]["memory_key"] == "k"
    assert kw["subject"]["version"] == 1700000000000000001
    assert kw["subject"]["outcome"] == "deferred"


def test_a_webhook_registration_names_where_events_go_and_hashes_the_url(monkeypatch) -> None:
    url = "https://hooks.example.com/services/T1/B2/the-token"
    body = WebhookRegisterInput(
        org="o",
        namespace="n",
        url=url,
        secret_ref="LOOM_WEBHOOK_SECRET_O__CI",
        events=["memory.written"],
    )
    subject = _audited_subject(monkeypatch, "webhook.register", body)
    assert "the-token" not in json.dumps(subject)
    assert subject["url_host"] == "https://hooks.example.com"
    assert subject["url"] == _digest(url)
    assert (subject["secret_ref"], subject["events"]) == (
        "LOOM_WEBHOOK_SECRET_O__CI",
        ["memory.written"],
    )
