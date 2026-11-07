"""The audit rows a memory handler writes itself name ids, keys and hashes, never the
caller's free text or a stored fact's text: `loom.audit_log` holds no memory content
and erasure never reaches it. The actor comes from the token."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pytest

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.hashing import sha256_value
from server.memory import store as memory_repo
from server.memory.api.models_lifecycle import MemoryCopyToInput, MemoryCrossPromoteInput
from server.memory.app import copy_ops, triple_projections
from server.memory.app.provenance import writer_actor
from server.memory.domain import triples_follow_rows as tfr
from server.memory.store import MemoryRow
from server.operations.base import Role

_MID = "11111111-1111-1111-1111-111111111111"
_RATIONALE = "Promoted because Dana Reyes said the vault code is 4417"


def _row(namespace: str) -> MemoryRow:
    return MemoryRow(
        memory_id=_MID,
        org="o",
        namespace=namespace,
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
        memory_type="semantic",
        value="the vault code is 4417",
        confidence=1.0,
        version=1,
        pinned=False,
        created_at=datetime(2026, 9, 12, tzinfo=UTC),
        expires_at=None,
    )


@pytest.fixture
def emitted(monkeypatch: pytest.MonkeyPatch) -> Any:
    rows: list[dict[str, Any]] = []

    async def _emit(event_type: str, **kw: Any) -> str:
        rows.append({"event_type": event_type, **kw})
        return ""

    async def _noop(*a: Any, **kw: Any) -> None:
        return None

    async def _plain(org: str, namespace: str) -> bool:
        return False

    monkeypatch.setattr("server.observability.audit.emit", _emit)
    monkeypatch.setattr(memory_repo, "insert_memory_versions", _noop)
    monkeypatch.setattr("server.memory.app.blacklist_guard.enforce_on_insert", _noop)
    monkeypatch.setattr("server.core.ch.get_client", lambda: object())
    monkeypatch.setattr("server.auth.authz.may_be_restricted", _plain)
    set_current_auth(
        AuthInfo(
            subject="alice",
            org="o",
            namespaces=("A", "B"),
            roles=(Role.ADMIN,),
            is_service_account=False,
        )
    )
    yield rows
    set_current_auth(None)


def _no_text(subject: Any, *texts: str) -> None:
    blob = json.dumps(subject)
    for text in texts:
        assert text not in blob


@pytest.mark.asyncio
async def test_cross_promote_audits_the_rationale_as_a_hash(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    async def _by_id(memory_id: str, **kw: Any) -> MemoryRow:
        return _row("A")

    monkeypatch.setattr(memory_repo, "get_by_id", _by_id)
    body = {"from_org": "o", "from_namespace": "A", "from_memory_id": _MID, "to_namespace": "B"}
    await copy_ops.handle_memory_cross_promote(
        MemoryCrossPromoteInput(**body, rationale=_RATIONALE)
    )
    (row,) = [r for r in emitted if r["event_type"] == "memory.cross_promote"]
    _no_text(row["subject"], _RATIONALE, "4417")
    assert row["subject"]["rationale_sha256"] == sha256_value(_RATIONALE)
    assert row["subject"]["rationale_chars"] == len(_RATIONALE)


@pytest.mark.asyncio
async def test_copy_to_audits_the_rationale_as_a_hash(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    async def _by_key(identity: Any, key: str, **kw: Any) -> MemoryRow:
        return _row("A")

    monkeypatch.setattr(memory_repo, "get_by_key", _by_key)
    body = {"from_org": "o", "from_namespace": "A", "memory_key": "k", "to_namespace": "B"}
    await copy_ops.handle_memory_copy_to(MemoryCopyToInput(**body, rationale=_RATIONALE))
    (row,) = [r for r in emitted if r["event_type"] == "memory.copy_to"]
    _no_text(row["subject"], _RATIONALE, "4417")
    assert row["subject"]["rationale_sha256"] == sha256_value(_RATIONALE)


@pytest.mark.asyncio
async def test_a_closed_triple_is_audited_by_hash_with_the_token_actor(
    monkeypatch: pytest.MonkeyPatch, emitted: list[dict[str, Any]]
) -> None:
    async def _close(*a: Any, **kw: Any) -> None:
        return None

    async def _held(**kw: Any) -> frozenset[Any]:
        return frozenset()

    monkeypatch.setattr(triple_projections, "close_triples", _close)
    monkeypatch.setattr(triple_projections, "reopen_held", _held)

    class _Input:
        org, namespace = "o", "A"
        agent = "agent-the-request-named"

    triple = ("dana reyes", "lives_in", "berlin")
    plan = tfr.FollowPlan(closes=[triple], inserts=[])
    closed = await triple_projections._close_planned(
        _Input(), plan, datetime(2026, 9, 12), ch=object()
    )
    assert closed == 1
    (row,) = [r for r in emitted if r["event_type"] == "triple.follows_row_supersession"]
    _no_text(row["subject"], "dana reyes", "berlin")
    assert row["subject"] == {"closed_sha256": sha256_value("dana reyes lives_in berlin")}
    assert row["actor"] == writer_actor() != "agent-the-request-named"
