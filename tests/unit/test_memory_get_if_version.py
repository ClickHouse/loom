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
from server.memory.store.rows import MemoryRow
from server.observability.budget_shape import current_budget_shape, note_budget_shape
from tests.unit.memory_store import one_holder


def _row(version: int = 7, sensitivity: str = "") -> MemoryRow:
    now = datetime(2026, 9, 12, tzinfo=UTC)
    return MemoryRow(
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
        org="acme",
        namespace="ns",
        agent="a",
        user_id="-",
        session_id="-",
        memory_key="k",
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
        e.actor = kw.get("actor")

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(audit, "emit", emit)
    monkeypatch.setattr(
        "server.memory.app.query._record_read_exposure",
        lambda op, inp, keys: e.exposures.append(keys),
    )
    return e


def _input(**kw: Any) -> MemoryGetInput:
    return MemoryGetInput(org="o", namespace="n", agent="a", memory_key="deploy_window", **kw)


@pytest.mark.asyncio
async def test_matching_version_returns_unchanged_without_body_audit_or_exposure(env) -> None:
    env.row = _row(version=7, sensitivity="high")
    note_budget_shape("")
    out = await mem.handle_memory_get(_input(if_version=7))
    assert out.found and out.unchanged and out.value is None and out.version == 7
    assert out.version_text == "7"  # exact for a JavaScript caller on this path too
    assert out.memory_id == env.row.memory_id
    assert env.audits == [] and env.exposures == []
    assert current_budget_shape() == ""  # no audit row, so the base budget


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


@pytest.mark.asyncio
async def test_a_get_names_the_rows_user_and_session(env) -> None:
    """A write back to the row (the dashboard's restore and verify) names its identity:
    a key repeats across users and sessions."""
    env.row = env.row.model_copy(update={"agent": "bot", "user_id": "u1", "session_id": "s2"})
    out = await mem.handle_memory_get(_input())
    assert (out.agent, out.user_id, out.session_id) == ("bot", "u1", "s2")
    dumped = out.model_dump()
    assert dumped["user_id"] == "u1" and dumped["session_id"] == "s2"


@pytest.mark.asyncio
async def test_a_high_read_is_audited_as_the_tokens_principal(env) -> None:
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.domain.trust import actor_for

    env.row = _row(sensitivity="high")
    set_current_auth(
        AuthInfo(
            subject="reader", org="acme", namespaces=("ns",), roles=(), is_service_account=True
        )
    )
    try:
        out = await mem._handle_memory_get(_input())
    finally:
        set_current_auth(None)
    assert out.found and env.audits == ["memory.sensitive_read"]
    assert env.actor == actor_for("reader", is_service_account=True)


@pytest.mark.asyncio
async def test_a_high_read_by_id_is_audited_as_the_tokens_principal(
    env, monkeypatch: pytest.MonkeyPatch
) -> None:
    import server.memory.store as memory_repo
    from server.auth.context import set_current_auth
    from server.auth.jwt import AuthInfo
    from server.memory.api.models_crud import MemoryGetByIdInput
    from server.memory.domain.trust import actor_for

    async def by_id(*_a: Any, **_k: Any) -> Any:
        return _row(sensitivity="high")

    async def in_scope(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(memory_repo, "get_by_id", by_id)

    monkeypatch.setattr(memory_repo, "namespaces_of_id", one_holder)
    monkeypatch.setattr("server.auth.authz.row_in_scope", in_scope)
    monkeypatch.setattr("server.auth.authz.row_reach", in_scope)
    set_current_auth(
        AuthInfo(
            subject="reader", org="acme", namespaces=("ns",), roles=(), is_service_account=True
        )
    )
    try:
        await mem.handle_memory_get_by_id(
            MemoryGetByIdInput(org="acme", memory_id="11111111-1111-1111-1111-111111111111")
        )
    finally:
        set_current_auth(None)
    assert env.audits == ["memory.sensitive_read"]
    assert env.actor == actor_for("reader", is_service_account=True)


@pytest.mark.asyncio
@pytest.mark.parametrize(("sensitivity", "shape"), [("high", "audited"), ("", "")])
async def test_a_get_is_held_to_the_audited_budget_only_when_it_audits(
    env, sensitivity: str, shape: str
) -> None:
    env.row = _row(sensitivity=sensitivity)
    note_budget_shape("")
    await mem.handle_memory_get(_input())
    assert current_budget_shape() == shape


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("sensitivity", "omit_high_value", "shape"),
    [("high", False, "audited"), ("", False, ""), ("high", True, "")],
)
async def test_a_get_by_id_is_held_to_the_audited_budget_only_when_it_audits(
    env, monkeypatch: pytest.MonkeyPatch, sensitivity: str, omit_high_value: bool, shape: str
) -> None:
    import server.memory.store as memory_repo
    from server.memory.api.models_crud import MemoryGetByIdInput

    async def by_id(*_a: Any, **_k: Any) -> Any:
        return _row(sensitivity=sensitivity)

    async def in_scope(*_a: Any, **_k: Any) -> bool:
        return True

    monkeypatch.setattr(memory_repo, "get_by_id", by_id)

    monkeypatch.setattr(memory_repo, "namespaces_of_id", one_holder)
    monkeypatch.setattr("server.auth.authz.row_in_scope", in_scope)
    monkeypatch.setattr("server.auth.authz.row_reach", in_scope)
    note_budget_shape("")
    await mem.handle_memory_get_by_id(
        MemoryGetByIdInput(
            org="acme",
            memory_id="11111111-1111-1111-1111-111111111111",
            omit_high_value=omit_high_value,
        )
    )
    assert current_budget_shape() == shape
    # the caller that only names the row reads no high value, so nothing is audited
    assert env.audits == (["memory.sensitive_read"] if shape else [])
