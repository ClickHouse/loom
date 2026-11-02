"""memory.apply — the scope grammar, with the repo stubbed.

Validation: exactly one target, limit required for filter/search, the
action block must be present, agent='*' refused. Execution: dry run lists
would_apply rows and writes nothing; a locked row is a refused row; label
and demote write version rows and mirror; filter SQL pins the identity and
detects truncation.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from server.memory.api.models_apply import MemoryApplyInput
from server.memory.app.apply_ops import handle_memory_apply
from server.memory.store import MemoryRow

_NOW = datetime(2026, 9, 12, 12, 0, 0)
_ID = {"org": "test", "namespace": "personal", "agent": "cursor"}


def _row(key: str, **overrides: Any) -> MemoryRow:
    base: dict[str, Any] = {
        "memory_id": f"33333333-3333-3333-3333-{abs(hash(key)) % 10**12:012d}",
        "org": "test",
        "namespace": "personal",
        "agent": "cursor",
        "user_id": "-",
        "session_id": "-",
        "memory_key": key,
        "memory_type": "semantic",
        "value": f"v:{key}",
        "confidence": 0.8,
        "version": 100,
        "pinned": False,
        "created_at": _NOW - timedelta(days=3),
        "expires_at": None,
        "tags": ["q3", "infra"],
    }
    base.update(overrides)
    return MemoryRow(**base)


# ---------------------------------------------------------------- validation


def test_target_must_name_exactly_one() -> None:
    with pytest.raises(ValidationError, match="exactly one"):
        MemoryApplyInput(**_ID, target={}, action="delete", reason="r")
    with pytest.raises(ValidationError, match="exactly one"):
        MemoryApplyInput(
            **_ID,
            target={"keys": ["a"], "filter": {"tags_any": ["x"]}},
            action="delete",
            reason="r",
            limit=5,
        )
    with pytest.raises(ValidationError, match="at least one predicate"):
        MemoryApplyInput(**_ID, target={"filter": {}}, action="delete", reason="r", limit=5)


def test_filter_and_search_need_a_limit_and_keys_do_not() -> None:
    with pytest.raises(ValidationError, match="needs `limit`"):
        MemoryApplyInput(
            **_ID, target={"filter": {"tags_any": ["q3"]}}, action="delete", reason="r"
        )
    with pytest.raises(ValidationError, match="needs `limit`"):
        MemoryApplyInput(
            **_ID, target={"search": {"query": "migration"}}, action="delete", reason="r"
        )
    inp = MemoryApplyInput(**_ID, target={"keys": ["a", "b"]}, action="delete", reason="r")
    assert inp.dry_run is True and inp.target.kind == "keys"


def test_action_block_reason_and_wildcard_agent() -> None:
    with pytest.raises(ValidationError, match="needs its `lock` block"):
        MemoryApplyInput(**_ID, target={"keys": ["a"]}, action="lock", reason="r")
    with pytest.raises(ValidationError, match="exactly one of ttl_seconds"):
        MemoryApplyInput(**_ID, target={"keys": ["a"]}, action="expire", reason="r", expire={})
    with pytest.raises(ValidationError, match="exactly one of confidence_delta"):
        MemoryApplyInput(
            **_ID,
            target={"keys": ["a"]},
            action="demote",
            reason="r",
            demote={"confidence_delta": -0.1, "confidence_factor": 0.5},
        )
    with pytest.raises(ValidationError):
        MemoryApplyInput(**_ID, target={"keys": ["a"]}, action="delete", reason="")
    with pytest.raises(ValidationError, match="memory.clear"):
        MemoryApplyInput(
            org="test",
            namespace="personal",
            agent="*",
            target={"keys": ["a"]},
            action="delete",
            reason="r",
        )


# ---------------------------------------------------------------- execution


class _CH:
    def __init__(self, filter_rows: list[dict[str, Any]] | None = None) -> None:
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executes: list[tuple[str, dict[str, Any]]] = []
        self.query_alls: list[tuple[str, dict[str, Any]]] = []
        self.filter_rows = filter_rows or []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))

    async def execute(self, sql: str, params: dict[str, Any] | None = None, **_: Any) -> None:
        self.executes.append((sql, params or {}))

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None, **_: Any
    ) -> list[dict[str, Any]]:
        self.query_alls.append((sql, params or {}))
        return self.filter_rows

    async def query_one(self, *_: Any, **__: Any) -> dict[str, Any] | None:
        return None


def _stub(monkeypatch: pytest.MonkeyPatch, rows_by_key: dict[str, MemoryRow], ch: _CH) -> None:
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.handlers.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.stack.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.restore.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.write.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.update.get_client", lambda: ch)
    monkeypatch.setattr("server.memory.app.lock.get_client", lambda: ch)

    async def _get_by_key(identity: Any, key: str, **_: Any) -> MemoryRow | None:
        return rows_by_key.get(key)

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)


@pytest.mark.asyncio
async def test_dry_run_lists_rows_and_writes_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH()
    _stub(
        monkeypatch, {"a": _row("a"), "b": _row("b", lock_mode="read_only", lock_reason="hold")}, ch
    )
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID, target={"keys": ["a", "b", "missing", "a"]}, action="delete", reason="stale"
        )
    )
    assert out.matched == 2 and out.would_apply == 1 and out.refused == 1 and out.applied == 0
    by = {r.memory_key: r for r in out.rows}
    assert by["a"].status == "would_apply"
    assert by["b"].status == "refused" and "locked read_only" in by["b"].detail
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_enforcing_delete_goes_through_the_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    ch = _CH()
    _stub(monkeypatch, {"a": _row("a")}, ch)
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID, target={"keys": ["a"]}, action="delete", reason="stale", dry_run=False
        )
    )
    assert out.applied == 1 and out.rows[0].status == "applied" and out.rows[0].version
    table, rows = ch.inserts[0]
    assert table == "loom.memory" and rows[0]["tombstoned_at"] is not None


@pytest.mark.asyncio
async def test_label_and_demote_write_version_rows_and_mirror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reemits: list[tuple[str, dict[str, Any]]] = []

    async def _reemit(org, ns, agent, user_id, session_id, key, *, ch=None, **overrides):
        reemits.append((key, overrides))
        return 1

    monkeypatch.setattr("server.memory.store.reemit_embedding_rows_for_key", _reemit)
    ch = _CH()
    _stub(monkeypatch, {"a": _row("a"), "b": _row("b", tags=["q3", "done"])}, ch)
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID,
            target={"keys": ["a", "b"]},
            action="label",
            reason="close q3",
            dry_run=False,
            label={"tags": ["done"], "mode": "add"},
        )
    )
    assert out.applied == 1 and out.noop == 1  # b already carried "done"
    written = ch.inserts[0][1][0]
    assert written["memory_key"] == "a" and written["tags"] == ["q3", "infra", "done"]
    assert ch.executes == []  # No UPDATE on the vector table
    assert reemits == [("a", {"tags": ["q3", "infra", "done"]})]

    ch2 = _CH()
    _stub(
        monkeypatch,
        {"a": _row("a", confidence=0.8), "c": _row("c", lock_mode="read_only", lock_reason="x")},
        ch2,
    )
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID,
            target={"keys": ["a", "c"]},
            action="demote",
            reason="aging",
            dry_run=False,
            demote={"confidence_factor": 0.5},
        )
    )
    assert out.applied == 1 and out.refused == 1  # read_only refuses a value write
    assert ch2.inserts[0][1][0]["confidence"] == 0.4
    assert ch2.executes == []
    assert reemits[-1] == ("a", {"confidence": 0.4})


@pytest.mark.asyncio
async def test_filter_resolution_pins_the_identity_and_reports_truncation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # three raw rows back for limit=2 → truncated
    raw = [
        {
            **_row(k).model_dump(),
            "memory_id": _row(k).memory_id,
            "value": f'"v:{k}"',
            "value_json": "{}",
        }
        for k in ("x", "y", "z")
    ]
    ch = _CH(filter_rows=raw)
    _stub(monkeypatch, {}, ch)
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID,
            target={"filter": {"tags_any": ["q3"], "created_before": _NOW, "confidence_lte": 0.9}},
            action="delete",
            reason="cleanup",
            limit=2,
        )
    )
    assert out.target == "filter" and out.matched == 2 and out.truncated is True
    sql, params = ch.query_alls[0]
    for col in (
        "org = {org:String}",
        "namespace = {ns:String}",
        "agent = {agent:String}",
        "user_id = {uid:String}",
        "session_id = {sid:String}",
    ):
        assert col in sql, col
    assert "hasAny(tags, {tags_any:Array(String)})" in sql and "created_at < {cb:DateTime}" in sql
    assert "confidence <= {cl:Float32}" in sql and params["lim"] == 3
    assert "ALTER" not in sql and "FINAL" in sql


@pytest.mark.asyncio
async def test_search_target_touches_only_the_callers_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The search reads across agents, users and sessions, and every hit's key
    # also exists under the caller's own identity. Only the hit that IS the
    # caller's row may be acted on: never another owner's row, and never a
    # same-key row of the caller's that the search did not match.
    from server.memory.api.models_search import _MemorySearchHit

    def _hit(key: str, *, agent: str = "cursor", uid: str = "-", sid: str = "-") -> Any:
        h = _MemorySearchHit(
            memory_key=key,
            memory_type="semantic",
            content_excerpt=key,
            distance=0.1,
            similarity=0.9,
            score=0.9,
            user_id=uid,
            session_id=sid,
        )
        h._row_agent = agent
        return h

    hits = [
        _hit("both", agent="other-agent"),  # another owner's row of a key the caller holds too
        _hit("both"),
        _hit("mine"),
        _hit("theirs", agent="other-agent"),
        _hit("other_user", uid="u2"),
        _hit("other_session", sid="s2"),
    ]

    asked: list[Any] = []

    async def _search(inp: Any) -> Any:
        asked.append(inp)
        return SimpleNamespace(results=hits)

    monkeypatch.setattr("server.memory.app.search.handle_memory_search", _search)
    reads: list[tuple[str, str, str, str]] = []

    async def _get_by_key(identity: Any, key: str, **_: Any) -> MemoryRow | None:
        reads.append((identity.agent, identity.user_id, identity.session_id, key))
        return _row(key, agent=identity.agent, user_id=identity.user_id)

    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)
    ch = _CH()
    monkeypatch.setattr("server.core.ch.get_client", lambda: ch)
    out = await handle_memory_apply(
        MemoryApplyInput(
            **_ID, target={"search": {"query": "q3"}}, action="delete", reason="r", limit=10
        )
    )
    assert out.matched == 2 and [r.memory_key for r in out.rows] == ["both", "mine"]
    assert reads == [("cursor", "-", "-", "both"), ("cursor", "-", "-", "mine")]
    assert (asked[0].session_id, asked[0].top_k) == ("-", 10)  # the caller's session only


def test_apply_op_is_a_writer_op_and_audited() -> None:
    from server.operations.base import Role
    from server.operations.registry import ALL_OPERATIONS

    op = {o.name: o for o in ALL_OPERATIONS}["memory.apply"]
    assert op.required_role == Role.WRITER and op.audit
