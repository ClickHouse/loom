"""Every member of a repository's code namespace may add team
file rules there, and revise or delete the rules they wrote — nothing more.

The rule is one function (`code_member_writes.code_member_may_write`), asked
by the transports' gate (`authorize_op`) only after the ordinary gate refused;
the handlers' second check (`assert_own_row`) refuses another principal's row
with a hint naming its author, key and what a member can do instead.
"""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.code.app import member_writes as cmw
from server.memory import store as memory_repo
from server.memory.app import update as update_mod
from server.operations.base import Role
from server.operations.registry import OPERATIONS_BY_NAME

ORG = "clickhouse"
CODE = {"code-clickhouse", "code-example-private"}
# (namespace, subject) → granted role; the ordinary gate reads it, and the
# member rule reads it as membership.
GRANTS = {
    ("code-clickhouse", "bob"): Role.VIEWER,
    ("code-clickhouse", "alice"): Role.VIEWER,
    ("code-clickhouse", "maint"): Role.ADMIN,
    ("code-example-private", "maint"): Role.ADMIN,
    ("team-notes", "bob"): Role.VIEWER,  # a memory namespace bob only views
}


def _auth(subject: str) -> AuthInfo:
    return AuthInfo(
        subject=subject, org=ORG, namespaces=(), roles=(Role.VIEWER,), is_service_account=False
    )


@pytest.fixture(autouse=True)
def gate(monkeypatch: pytest.MonkeyPatch):
    """The ordinary gate as the grants table says, plus the code registry."""

    async def check_authorized(auth, required_role, org=None, namespace=None, **_kw):
        role = GRANTS.get((namespace, auth.subject))
        if role is None or role.value < required_role.value:
            raise PermissionError(f"{required_role.name} required on {org}/{namespace}")

    async def lookup_grant(org, namespace, subject):
        return GRANTS.get((namespace, subject))

    monkeypatch.setattr(authz, "check_authorized", check_authorized)
    monkeypatch.setattr(authz, "_lookup_grant", lookup_grant)
    monkeypatch.setattr(authz, "_is_code_namespace", lambda org, ns: ns in CODE)
    yield
    set_current_auth(None)
    cmw._member_write.set(False)


async def _authorize(subject: str, op: str, ns: str) -> None:
    auth = _auth(subject)
    set_current_auth(auth)
    await cmw.authorize_op(auth, OPERATIONS_BY_NAME[op], org=ORG, namespace=ns)


# --- the transport gate -----------------------------------------------------------


@pytest.mark.asyncio
async def test_a_viewer_member_may_memory_set_into_the_code_namespace() -> None:
    assert OPERATIONS_BY_NAME["memory.set"].required_role == Role.WRITER
    await _authorize("bob", "memory.set", "code-clickhouse")
    assert cmw.admitted_as_code_member() is True


@pytest.mark.asyncio
async def test_a_viewer_cannot_write_a_non_code_namespace_they_only_view() -> None:
    with pytest.raises(PermissionError):
        await _authorize("bob", "memory.set", "team-notes")
    assert cmw.admitted_as_code_member() is False


@pytest.mark.asyncio
async def test_a_non_member_cannot_write_the_private_repositorys_namespace() -> None:
    with pytest.raises(PermissionError):
        await _authorize("bob", "memory.set", "code-example-private")
    with pytest.raises(PermissionError):
        await _authorize("carol", "memory.set", "code-clickhouse")  # no grant at all


@pytest.mark.asyncio
async def test_an_admin_passes_the_ordinary_gate_unchanged() -> None:
    await _authorize("maint", "memory.set", "code-example-private")
    assert cmw.admitted_as_code_member() is False  # not the member rule: full rights
    await _authorize("maint", "memory.pin", "code-clickhouse")
    await _authorize("maint", "memory.lock", "code-clickhouse")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "op",
    [
        "memory.pin",
        "memory.lock",
        "memory.redact",
        "memory.bulk_set",
        "memory.set_from_messages",
        "memory.clear",
    ],
)
async def test_other_memory_writes_stay_refused_for_a_viewer(op: str) -> None:
    # a renamed or re-roled op must fail here, not skip silently
    assert op in OPERATIONS_BY_NAME
    assert OPERATIONS_BY_NAME[op].required_role.value > Role.VIEWER.value
    with pytest.raises(PermissionError):
        await _authorize("bob", op, "code-clickhouse")


@pytest.mark.asyncio
async def test_code_admin_ops_stay_refused_for_a_viewer() -> None:
    admin_ops = [
        n
        for n, op in OPERATIONS_BY_NAME.items()
        if n.startswith("code.") and op.required_role.value > Role.VIEWER.value
    ]
    assert admin_ops, "the registry has code.* ops above viewer"
    for name in admin_ops:
        assert not await cmw.code_member_may_write(_auth("bob"), name, ORG, "code-clickhouse")
        with pytest.raises(PermissionError):
            await _authorize("bob", name, "code-clickhouse")


@pytest.mark.asyncio
async def test_the_rule_never_widens_a_read() -> None:
    # asked only after a refusal, and only for the three write ops
    for name in OPERATIONS_BY_NAME:
        if name not in cmw.CODE_MEMBER_WRITE_OPS:
            assert not await cmw.code_member_may_write(_auth("bob"), name, ORG, "code-clickhouse")
    assert cmw.CODE_MEMBER_WRITE_OPS == {"memory.set", "memory.update", "memory.delete"}


# --- the handlers' own-row check -----------------------------------------------------


def _row(actor: str, **over: Any) -> memory_repo.MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "51c3cdf5-7ea2-4b79-9d34-a6b5d7a9a2b2",
        "org": ORG,
        "namespace": "code-clickhouse",
        "agent": "claude-code",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "rules/join/bucket_count",
        "memory_type": "semantic",
        "value": "Keep 32 buckets.",
        "confidence": 1.0,
        "version": 100,
        "pinned": False,
        "created_at": datetime(2026, 9, 22),
        "expires_at": None,
        "entities": [],
        "actor": actor,
        "files": ["src/Interpreters/GraceHashJoin.cpp"],
        "tags": ["constraint"],
    }
    base.update(over)
    return memory_repo.MemoryRow(**base)


class _CH:
    def __init__(self) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None

    async def query_all(self, *_: Any, **__: Any) -> list[dict[str, Any]]:
        return []

    async def execute(self, *_: Any, **__: Any) -> None:
        return None

    def tombstoned(self) -> bool:
        return any(
            r.get("tombstoned_at") for t, rs in self.inserts if t == "loom.memory" for r in rs
        )

    def memory_rows(self) -> list[dict[str, Any]]:
        return [r for t, rs in self.inserts if t == "loom.memory" for r in rs]


def _wire(monkeypatch: pytest.MonkeyPatch, existing: memory_repo.MemoryRow | None) -> _CH:
    import server.memory.app.blacklist_guard as _bl
    import server.memory.app.conflicts as _cf
    from server.memory.app import handlers as mem_handlers
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod

    ch = _CH()
    monkeypatch.setattr(mem_handlers, "get_client", lambda: ch)
    monkeypatch.setattr(_stack_mod, "get_client", lambda: ch)
    monkeypatch.setattr(_restore_mod, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _existing(*_: Any, **__: Any) -> memory_repo.MemoryRow | None:
        return existing

    monkeypatch.setattr("server.memory.store.get_by_key", _existing)
    monkeypatch.setattr("server.memory.store.get_by_id", _existing)
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr(_bl, "get_client", lambda: ch, raising=False)
    monkeypatch.setattr(_cf, "get_client", lambda: ch, raising=False)
    return ch


def _delete_input() -> Any:
    from server.memory.api.models_crud import MemoryDeleteInput

    return MemoryDeleteInput(
        org=ORG,
        namespace="code-clickhouse",
        agent="claude-code",
        memory_key="rules/join/bucket_count",
    )


@pytest.mark.asyncio
async def test_the_author_deletes_their_own_team_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import handlers as mem_handlers

    ch = _wire(monkeypatch, _row("human:alice"))
    await _authorize("alice", "memory.delete", "code-clickhouse")
    out = await mem_handlers.handle_memory_delete(_delete_input())
    assert out.tombstoned is True and ch.tombstoned()


@pytest.mark.asyncio
async def test_an_admin_deletes_anyones_team_rule(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import handlers as mem_handlers

    ch = _wire(monkeypatch, _row("human:alice"))
    await _authorize("maint", "memory.delete", "code-clickhouse")
    out = await mem_handlers.handle_memory_delete(_delete_input())
    assert out.tombstoned is True and ch.tombstoned()


@pytest.mark.asyncio
async def test_another_member_is_refused_with_a_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.app import handlers as mem_handlers

    ch = _wire(monkeypatch, _row("human:alice"))
    await _authorize("bob", "memory.delete", "code-clickhouse")
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_delete(_delete_input())
    assert e.value.status_code == 403 and not ch.inserts
    detail = e.value.detail
    assert detail["code"] == "AUTH_INSUFFICIENT"
    hint = detail["hint"]
    assert hint["author"] == "alice" and hint["memory_key"] == "rules/join/bucket_count"
    assert hint["namespace"] == "code-clickhouse" and len(hint["next"]) == 2
    assert "corrected rule" in hint["next"][0] and "alice" in hint["next"][1]
    assert "written by alice" in detail["message"]  # a client showing only the message sees it


@pytest.mark.asyncio
async def test_the_author_revises_their_rule_and_another_member_cannot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import write as write_mod

    def _set() -> MemorySetInput:
        return MemorySetInput(
            org=ORG,
            namespace="code-clickhouse",
            agent="claude-code",
            memory_key="rules/join/bucket_count",
            classify=False,
            value="Keep 64 buckets since the 26.8 join benchmark.",
        )

    ch = _wire(monkeypatch, _row("human:alice"))
    await _authorize("alice", "memory.set", "code-clickhouse")
    await write_mod.handle_memory_set(_set())
    rows = ch.memory_rows()
    assert rows and rows[0]["actor"] == "human:alice"
    assert rows[0]["files"] == ["src/Interpreters/GraceHashJoin.cpp"]  # kept on revision

    ch = _wire(monkeypatch, _row("human:alice"))
    await _authorize("bob", "memory.set", "code-clickhouse")
    with pytest.raises(HTTPException) as e:
        await write_mod.handle_memory_set(_set())
    assert e.value.status_code == 403 and e.value.detail["hint"]["author"] == "alice"
    assert not ch.memory_rows()


@pytest.mark.asyncio
async def test_memory_update_by_key_checks_the_author(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.memory.api.models_crud import MemoryUpdateInput
    from server.memory.app import handlers as mem_handlers
    from server.memory.app import restore as _restore_mod
    from server.memory.app import stack as _stack_mod

    row = _row("human:alice")

    async def _candidates(_inp: Any, _ch: Any) -> list[dict[str, Any]]:
        return [{"memory_id": row.memory_id}]

    async def _by_id(*_: Any, **__: Any) -> memory_repo.MemoryRow:
        return row

    async def _update(inp: Any, existing: Any, ch: Any, *, op: str) -> Any:
        return SimpleNamespace(
            updated=True,
            new_version=2,
            content_sha256="x",
            conflict_verdict="",
            conflict_peers=0,
            duplicate_of="",
        )

    monkeypatch.setattr(mem_handlers, "get_client", lambda: _CH())
    monkeypatch.setattr(_stack_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(_restore_mod, "get_client", lambda: _CH())
    monkeypatch.setattr("server.memory.app.write.get_client", mem_handlers.get_client)
    monkeypatch.setattr("server.memory.app.update.get_client", mem_handlers.get_client)
    monkeypatch.setattr(update_mod, "_live_rows_for_key", _candidates)
    monkeypatch.setattr("server.memory.store.get_by_id", _by_id)
    monkeypatch.setattr(update_mod, "_update_live_row", _update)
    inp = MemoryUpdateInput(
        org=ORG,
        namespace="code-clickhouse",
        memory_key="rules/join/bucket_count",
        value="Keep 64 buckets.",
    )
    await _authorize("bob", "memory.update", "code-clickhouse")
    with pytest.raises(HTTPException) as e:
        await update_mod.handle_memory_update(inp)
    assert e.value.status_code == 403
    await _authorize("alice", "memory.update", "code-clickhouse")
    out = await update_mod.handle_memory_update(inp)
    assert out.updated is True


def test_outside_the_member_rule_the_check_is_a_no_op() -> None:
    cmw._member_write.set(False)
    cmw.assert_own_row(_row("human:alice"), op="memory.delete")  # admin / ordinary writer
    cmw._member_write.set(True)
    cmw.assert_own_row(None, op="memory.set")  # a new rule: nothing to own yet


@pytest.mark.asyncio
async def test_a_refused_member_is_sent_to_the_decisions_author() -> None:
    # A member cannot call decision.record, so the hint names who can.
    row = _row("human:alice", memory_type="decision", memory_key="decision:k")
    (step,) = cmw.refusal_hint(row, op="memory.delete")["next"]
    assert step.startswith("ask alice or a maintainer of 'code-clickhouse'")
    assert "decision.record" in step and "memory.set" not in step
    assert not await cmw.code_member_may_write(
        _auth("bob"), "decision.record", ORG, "code-clickhouse"
    )


@pytest.mark.asyncio
async def test_the_refusal_names_a_decision_and_its_one_step(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import handlers as mem_handlers

    _wire(monkeypatch, _row("human:alice", memory_type="decision"))
    await _authorize("bob", "memory.delete", "code-clickhouse")
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_delete(_delete_input())
    message = e.value.detail["message"]
    assert "the team decision 'rules/join/bucket_count' was written by alice" in message
    assert message.endswith(e.value.detail["hint"]["next"][0] + ".")


@pytest.mark.asyncio
async def test_a_row_tagged_decision_is_named_a_decision_with_the_rule_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The pre-edit block calls it a decision; a newer rule supersedes it, so the steps stay.
    from server.memory.app import handlers as mem_handlers

    _wire(monkeypatch, _row("human:alice", tags=["decision"]))
    await _authorize("bob", "memory.delete", "code-clickhouse")
    with pytest.raises(HTTPException) as e:
        await mem_handlers.handle_memory_delete(_delete_input())
    hint = e.value.detail["hint"]
    assert hint["kind"] == "decision" and len(hint["next"]) == 2
    assert "corrected rule" in hint["next"][0]
    assert e.value.detail["message"].startswith(
        "memory.delete: the team decision 'rules/join/bucket_count' was written by alice"
    )
