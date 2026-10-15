"""A poll that holds the version gets the version back."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from server.auth.context import current_auth_var
from server.auth.jwt import AuthInfo
from server.core.errors import AuthInsufficientError
from server.memory.api.models_crud import MemoryGetInput
from server.memory.app import handlers as mem


def _row(version: int = 7, sensitivity: str = "") -> SimpleNamespace:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return SimpleNamespace(
        memory_id="11111111-1111-1111-1111-111111111111",
        value={"text": "deploys on Tuesday"},
        memory_type="semantic",
        confidence=1.0,
        version=version,
        created_at=now,
        updated_at=now,
        expires_at=None,
        on_expire="",
        expire_reason="",
        valid_at=None,
        valid_to=None,
        temporal_anchor=None,
        temporal_confidence=0.0,
        pinned=False,
        actor="",
        source_class="",
        sensitivity=sensitivity,
        recall_count=3,
        last_recalled_at=None,
        evidence_count=1,
        reinforcement_count=1,
        last_reinforced_at=None,
        tags=[],
        entities=[],
        status="active",
        source_trace="",
        run_id="-",
        verified_by="",
        verified_at=None,
        verified_sha256="",
        grounding="",
        evidence="",
        char_limit=None,
        mentioned_at=None,
    )


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    from server.memory import store as memory_repo
    from server.observability import audit

    e = SimpleNamespace(row=_row(), audits=[], exposures=[], identity=None)

    async def get_by_key(identity: Any, key: str, **kw: Any) -> Any:
        e.identity = identity
        return e.row

    async def emit(kind: str, **kw: Any) -> None:
        e.audits.append(kind)

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(audit, "emit", emit)
    monkeypatch.setattr(
        "server.memory.app.query._record_read_exposure",
        lambda op, inp, keys, receipt=None: e.exposures.append(keys),
    )
    return e


def _input(**kw: Any) -> MemoryGetInput:
    return MemoryGetInput(org="o", namespace="n", agent="a", memory_key="deploy_window", **kw)


@pytest.mark.asyncio
async def test_matching_version_returns_unchanged_without_body_audit_or_exposure(env) -> None:
    env.row = _row(version=7, sensitivity="high")
    out = await mem.handle_memory_get(_input(if_version=7))
    assert out.found and out.unchanged and out.value is None and out.version == 7
    assert out.memory_id == env.row.memory_id
    assert env.audits == [] and env.exposures == []


@pytest.mark.asyncio
async def test_stale_or_absent_precondition_returns_the_body(env) -> None:
    out = await mem.handle_memory_get(_input(if_version=6))
    assert out.found and not out.unchanged and out.value == {"text": "deploys on Tuesday"}
    assert env.exposures == [["deploy_window"]]
    out = await mem.handle_memory_get(_input())
    assert out.found and not out.unchanged and out.value is not None


@pytest.mark.asyncio
async def test_missing_row_is_still_not_found(env, monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory import store as memory_repo

    async def none(identity: Any, key: str, **kw: Any) -> Any:
        return None

    monkeypatch.setattr(memory_repo, "get_by_key", none)
    out = await mem.handle_memory_get(_input(if_version=7))
    assert not out.found and not out.unchanged


@pytest.mark.asyncio
async def test_exact_get_obeys_the_tokens_pinned_user(env) -> None:
    auth = AuthInfo(
        subject="shared-b",
        org="o",
        namespaces=("n",),
        roles=(),
        is_service_account=True,
        read_scope="own_user",
        read_user_id="user-b",
    )
    token = current_auth_var.set(auth)
    try:
        with pytest.raises(AuthInsufficientError):
            await mem.handle_memory_get(_input(user_id="user-a"))
        await mem.handle_memory_get(_input())
        assert env.identity.user_id == "user-b"
    finally:
        current_auth_var.reset(token)
