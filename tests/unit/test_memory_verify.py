"""memory.verify — attestation bound to content, actor decided by the token.

The state is derived, never stored: human/process while the attested hash
equals the live hash, stale once the content moves, none when never attested.
The handler writes one full version row (every column carried forward) and
is a no-op when nothing would change.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth import authz
from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.core.hashing import sha256_value_json
from server.memory.api.models_crud import MemoryVerifyInput, MemoryVerifyOutput
from server.memory.app import after_write
from server.memory.app import verify as mv
from server.memory.domain import trust
from server.memory.store import MEMORY_PERSISTED_COLS, MemoryRow
from server.operations.base import Role

_NOW = datetime(2026, 9, 11, 12, 0, 0)


def _sha(value: Any) -> str:
    return sha256_value_json(json.dumps(value, default=str))


def _row(**over: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": "11111111-1111-1111-1111-111111111111",
        "org": "o",
        "namespace": "ns",
        "agent": "claude-code",
        "user_id": "-",
        "session_id": "-",
        "memory_key": "deploy_window",
        "memory_type": "semantic",
        "value": "Tue and Thu only; never Friday.",
        "confidence": 1.0,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=2),
        "expires_at": None,
    }
    base.update(over)
    return MemoryRow(**base)


def _token(subject: str, sa: bool = False, allowed: bool = True) -> AuthInfo:
    """A flat WRITER token of org "o" whose claim names `ns` when allowed."""
    return AuthInfo(
        subject=subject,
        org="o",
        namespaces=("ns",) if allowed else ("elsewhere",),
        roles=(Role.WRITER,),
        is_service_account=sa,
    )


class _CH:
    def __init__(self) -> None:
        self.inserted: list[tuple[str, list[dict[str, Any]]]] = []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.inserted.append((table, rows))


_GRANTS: dict[tuple[str, str, str], Role] = {}


@pytest.fixture(autouse=True)
def _clear_auth(monkeypatch: pytest.MonkeyPatch):
    """No namespace is restricted; grants come from `_GRANTS`."""

    async def _plain(org: str, ns: str) -> bool:
        return False

    async def _grant(org: str, ns: str, subject: str) -> Role | None:
        return _GRANTS.get((org, ns, subject))

    monkeypatch.setattr(authz, "may_be_restricted", _plain)
    monkeypatch.setattr(authz, "_lookup_grant", _grant)
    _GRANTS.clear()
    set_current_auth(None)
    yield
    set_current_auth(None)
    _GRANTS.clear()


def _wire(monkeypatch: pytest.MonkeyPatch, row: MemoryRow | None) -> _CH:
    from server.memory import store as memory_repo

    async def _by_id(memory_id: str, *, org: str | None = None, **kw: Any) -> MemoryRow | None:
        assert kw.get("strong")  # the attestation is built from this row
        return row if row is not None and memory_id == row.memory_id else None

    async def _by_key(identity: Any, key: str, **kw: Any) -> MemoryRow | None:
        assert kw.get("strong")
        return row if row is not None and key == row.memory_key else None

    async def _holders(org: str, memory_id: str, *, strong: bool) -> list[str]:
        assert strong  # an attestation acts on the holder it finds
        return [row.namespace] if row is not None and memory_id == row.memory_id else []

    monkeypatch.setattr(memory_repo, "get_by_id", _by_id)
    monkeypatch.setattr(memory_repo, "namespaces_of_id", _holders)
    monkeypatch.setattr(memory_repo, "get_by_key", _by_key)
    ch = _CH()
    monkeypatch.setattr(mv, "get_client", lambda: ch)
    return ch


# ---------------- state derivation ----------------


def test_state_is_derived_against_the_live_hash() -> None:
    sha = _sha("v1")
    assert trust.verification_state("", "", sha) == "none"
    assert trust.verification_state("human:alice", sha, sha) == "human"
    assert trust.verification_state("process:ci", sha, sha) == "process"
    # content moved after the attestation → stale, not silently still verified
    assert trust.verification_state("human:alice", sha, _sha("v2")) == "stale"
    # an attestation that cannot be confirmed confers nothing
    assert trust.verification_state("human:alice", sha, "") == "stale"
    assert trust.verification_state("human:alice", "", sha) == "stale"
    assert trust.verification_state("human:alice", "0", "0") == "stale"


def test_actor_kind_comes_from_the_token_kind() -> None:
    assert trust.actor_for("alice", is_service_account=False) == "human:alice"
    assert trust.actor_for("ci-bot", is_service_account=True) == "process:ci-bot"
    assert trust.is_valid_actor("human:x") and trust.is_valid_actor("process:y")
    assert not trust.is_valid_actor("agent/claude") and not trust.is_valid_actor("human:")


# ---------------- handler ----------------


@pytest.mark.asyncio
async def test_verify_writes_one_full_version_row_bound_to_content(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    set_current_auth(_token("alice"))

    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=row.memory_id, actor="process:spoof")
    )

    assert out.found and out.changed
    assert out.verified_by == "human:alice"  # body's actor ignored under auth
    assert out.verification == "human"
    assert out.content_sha256 == _sha(row.value)
    assert out.version > row.version

    ((table, rows),) = ch.inserted
    assert table == "loom.memory" and len(rows) == 1
    new = rows[0]
    # every persisted column present — no silent zeroing under FINAL
    assert set(MEMORY_PERSISTED_COLS) <= set(new)
    assert new["verified_by"] == "human:alice"
    assert new["verified_sha256"] == _sha(row.value) == new["content_sha256"]
    assert new["verified_at"] is not None and new["version"] == out.version
    # untouched columns carried forward, not reset
    # A hand-built row has no stored text, so the version row
    # carries the bytes memory.set writes for the value — quoted, not raw
    assert new["memory_key"] == "deploy_window" and new["value"] == json.dumps(row.value)
    assert json.loads(new["value"]) == row.value
    assert new["pinned"] == 0 and new["confidence"] == 1.0


@pytest.mark.asyncio
async def test_service_account_attests_as_process(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    _wire(monkeypatch, row)
    set_current_auth(_token("ci-bot", sa=True))
    out = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=row.memory_id))
    assert out.verified_by == "process:ci-bot" and out.verification == "process"


@pytest.mark.asyncio
async def test_dev_mode_requires_a_well_formed_actor(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    with pytest.raises(HTTPException) as ei:
        await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=row.memory_id))
    assert ei.value.status_code == 422 and ch.inserted == []
    with pytest.raises(HTTPException):
        await mv.handle_memory_verify(
            MemoryVerifyInput(org="o", memory_id=row.memory_id, actor="claude")
        )
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=row.memory_id, actor="human:dev")
    )
    assert out.verified_by == "human:dev"


@pytest.mark.asyncio
async def test_addressing_by_identity_and_key(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    _wire(monkeypatch, row)
    set_current_auth(_token("alice"))
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", namespace="ns", agent="claude-code", memory_key="deploy_window")
    )
    assert out.found and out.verification == "human"
    with pytest.raises(HTTPException) as ei:
        await mv.handle_memory_verify(MemoryVerifyInput(org="o"))
    assert ei.value.status_code == 422


@pytest.mark.asyncio
async def test_not_found_and_forbidden(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    set_current_auth(_token("alice"))
    assert (
        await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id="nope"))
    ).found is False
    set_current_auth(_token("intruder", allowed=False))
    # By org-wide id: a row the caller cannot reach answers exactly as a missing id
    # does — a 403 naming its namespace would confirm the id exists.
    foreign = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=row.memory_id))
    assert foreign == MemoryVerifyOutput(found=False) and ch.inserted == []
    # By (namespace, key): refused before the lookup, whether or not the key exists.
    for key in (row.memory_key, "no_such_key"):
        with pytest.raises(HTTPException) as ei:
            await mv.handle_memory_verify(
                MemoryVerifyInput(org="o", namespace="ns", agent="claude-code", memory_key=key)
            )
        assert ei.value.status_code == 403
    assert ch.inserted == []


_REFUSED = {
    # Named in the claim, but a viewer: reading is not attesting.
    "viewer": AuthInfo(
        subject="alice", org="o", namespaces=("ns",), roles=(Role.VIEWER,), is_service_account=False
    ),
    # Flat WRITER, but its authoritative map says viewer on ns.
    "scoped-viewer": AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("ns",),
        roles=(Role.WRITER,),
        is_service_account=True,
        ns_roles=(("ns", "viewer"),),
    ),
    # Another tenant, whose subject shares a name with a writer here.
    "other-tenant": AuthInfo(
        subject="bob", org="other", namespaces=("*",), roles=(Role.ADMIN,), is_service_account=False
    ),
}


@pytest.mark.asyncio
@pytest.mark.parametrize("who", sorted(_REFUSED))
async def test_attesting_needs_writer_on_the_rows_namespace(
    monkeypatch: pytest.MonkeyPatch, who: str
) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    _GRANTS[("o", "ns", "bob")] = Role.WRITER
    set_current_auth(_REFUSED[who])
    by_id = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=row.memory_id))
    assert by_id == MemoryVerifyOutput(found=False)
    with pytest.raises(HTTPException) as ei:
        await mv.handle_memory_verify(
            MemoryVerifyInput(
                org="o", namespace="ns", agent="claude-code", memory_key="deploy_window"
            )
        )
    assert ei.value.status_code == 403
    assert ch.inserted == []


@pytest.mark.asyncio
async def test_a_writer_grant_admits_a_token_that_does_not_name_the_namespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    _GRANTS[("o", "ns", "carol")] = Role.WRITER
    set_current_auth(_token("carol", allowed=False))
    out = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=row.memory_id))
    assert out.found and out.changed and len(ch.inserted) == 1


@pytest.mark.asyncio
async def test_reverify_is_a_no_op_and_edit_makes_it_stale(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _sha("Tue and Thu only; never Friday.")
    live = _row(
        verified_by="human:alice", verified_sha256=sha, verified_at=_NOW, content_sha256=sha
    )
    ch = _wire(monkeypatch, live)
    set_current_auth(_token("alice"))
    out = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=live.memory_id))
    assert out.found and out.changed is False and out.verification == "human" and ch.inserted == []

    # the same attestation on a row whose content moved reads stale, and a
    # re-verify writes a fresh binding
    edited = _row(
        value="Fridays are fine now.",
        verified_by="human:alice",
        verified_sha256=sha,
        verified_at=_NOW,
    )
    ch = _wire(monkeypatch, edited)
    assert (
        trust.verification_state(edited.verified_by, edited.verified_sha256, _sha(edited.value))
        == "stale"
    )
    out = await mv.handle_memory_verify(MemoryVerifyInput(org="o", memory_id=edited.memory_id))
    assert out.changed and out.verification == "human" and out.content_sha256 == _sha(edited.value)
    assert ch.inserted[0][1][0]["verified_sha256"] == _sha(edited.value)


@pytest.mark.asyncio
async def test_revoke_clears_and_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    sha = _sha("Tue and Thu only; never Friday.")
    live = _row(verified_by="human:alice", verified_sha256=sha, verified_at=_NOW)
    ch = _wire(monkeypatch, live)
    set_current_auth(_token("someone-else"))
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=live.memory_id, revoke=True)
    )
    assert out.changed and out.verification == "none" and out.verified_by == ""
    new = ch.inserted[0][1][0]
    assert new["verified_by"] == "" and new["verified_at"] is None and new["verified_sha256"] == ""

    ch = _wire(monkeypatch, _row())
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=_row().memory_id, revoke=True)
    )
    assert out.found and out.changed is False and ch.inserted == []


# ---------------- surfaces ----------------


def test_registered_audited_and_callable_by_name_over_mcp() -> None:
    from server.operations import toolsets
    from server.operations.registry import OPERATIONS_BY_NAME
    from server.transport import mcp_descriptions

    op = OPERATIONS_BY_NAME["memory.verify"]
    assert op.required_role.name == "WRITER" and op.audit is True and op.idempotent is True
    assert op.skip_namespace_authz is True  # memory_id addressing; handler re-checks
    assert "memory.verify" not in toolsets._MCP_HIDDEN_OPS
    assert "memory.verify" not in toolsets._MCP_CORE_OPS  # a human act, not a chat-loop verb
    assert "stale" in mcp_descriptions.TOOL_DESCRIPTIONS["memory.verify"]


def test_get_outputs_carry_the_derived_state() -> None:
    from server.memory.api.models_crud import MemoryGetByIdOutput, MemoryGetOutput, _MemoryListEntry

    for model in (MemoryGetOutput, MemoryGetByIdOutput):
        fields = model.model_fields
        assert {"verified_by", "verified_at", "verification"} <= set(fields)
        assert fields["verification"].default == "none"
    assert {"verified_by", "verification"} <= set(_MemoryListEntry.model_fields)


def test_persisted_columns_include_attestation_and_projection_matches() -> None:
    from server.memory import store as memory_repo

    for col in ("verified_by", "verified_at", "verified_sha256"):
        assert col in MEMORY_PERSISTED_COLS
        assert col in memory_repo._MEMORY_FINAL_PROJECTION
    r = _row(
        verified_by="human:x",
        verified_sha256="abc",
        verified_at=datetime.now(UTC).replace(tzinfo=None),
    )
    built = memory_repo.build_memory_version_row(r, version=1)
    assert built["verified_by"] == "human:x" and built["verified_sha256"] == "abc"


# ---------------- the answer waits for the durable insert only ----------------


@pytest.mark.asyncio
async def test_verify_answers_before_the_embeddings_mirror_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attestation row is committed and the answer returns; the mirror
    into memory_embeddings (a FINAL read and a second committed insert) runs
    afterwards as an `after_write` task."""
    import asyncio

    row = _row()
    ch = _wire(monkeypatch, row)
    set_current_auth(_token("alice"))
    started = asyncio.Event()
    release = asyncio.Event()
    mirrored: list[tuple[str, bool]] = []

    async def slow_mirror(r: MemoryRow, *, verified: bool) -> None:
        started.set()
        await release.wait()
        mirrored.append((r.memory_key, verified))

    monkeypatch.setattr(mv, "_mirror_verified_to_embeddings", slow_mirror)
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=row.memory_id, actor="")
    )
    assert out.found and out.changed
    assert [t for t, _ in ch.inserted] == [
        "loom.memory"
    ]  # the durable insert, and the answer is back
    await asyncio.wait_for(started.wait(), 1.0)  # the mirror was scheduled…
    assert mirrored == []  # …and has not finished: the answer did not wait for it
    release.set()
    await after_write.drain()
    assert mirrored == [("deploy_window", True)]


@pytest.mark.asyncio
async def test_a_failing_mirror_never_fails_the_verify(monkeypatch: pytest.MonkeyPatch) -> None:
    row = _row()
    ch = _wire(monkeypatch, row)
    set_current_auth(_token("alice"))

    async def boom(r: MemoryRow, *, verified: bool) -> None:
        raise RuntimeError("embeddings unavailable")

    monkeypatch.setattr(mv, "_mirror_verified_to_embeddings", boom)
    out = await mv.handle_memory_verify(
        MemoryVerifyInput(org="o", memory_id=row.memory_id, actor="")
    )
    assert out.changed and len(ch.inserted) == 1
    await after_write.drain()  # the failure is logged, not raised


async def test_the_mirror_re_emits_every_live_row_of_the_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One row per (scope, embedding model): each is re-emitted whole with `verified` set,
    keeping the columns the verify input never carried (reinforcement, mention times)."""
    from server.memory.store import EMBEDDING_PERSISTED_COLS

    row = _row()
    stored = [
        {
            **{c: v for c, v in EMBEDDING_PERSISTED_COLS.items()},
            "org": row.org,
            "namespace": row.namespace,
            "agent": row.agent,
            "user_id": row.user_id,
            "session_id": row.session_id,
            "memory_key": row.memory_key,
            "scope": scope,
            "embedding_model": model,
            "embedding_dim": 2,
            "embedding": [0.5, 0.5],
            "last_reinforced_at": datetime(2026, 1, 2),
            "mentioned_at": datetime(2026, 1, 3),
            "version": 7,
        }
        for scope, model in (("private", "m1"), ("shared", "m2"))
    ]
    inserted: list[dict[str, Any]] = []

    class _CH:
        async def query_all(self, sql: str, params: dict[str, Any] | None = None) -> Any:
            assert "FROM loom.memory_embeddings FINAL" in sql
            return [dict(r) for r in stored]

        async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
            assert table == "loom.memory_embeddings"
            inserted.extend(rows)

    monkeypatch.setattr(mv, "get_client", lambda: _CH())
    await mv._mirror_verified_to_embeddings(row, verified=True)
    assert [(r["scope"], r["embedding_model"]) for r in inserted] == [
        ("private", "m1"),
        ("shared", "m2"),
    ]
    assert all(r["verified"] == 1 and r["version"] > 7 for r in inserted)
    assert all(r["mentioned_at"] == datetime(2026, 1, 3) for r in inserted)
