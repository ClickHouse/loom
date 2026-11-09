"""Writes resolve the row they act on under the request's read scope.

A token pinned to user U that names `agent="*"` (or addresses a row by id)
must not delete, lock, touch, outcome or hash-probe user V's row: V's row is
simply not found. The store is faked here; it honours the scope it is given
the way `get_by_key` / `get_by_id` do, so each test shows the handler passed
the request's scope and not the whole namespace.
"""

from __future__ import annotations

import asyncio
import contextvars
import re
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi import HTTPException

from server.auth.context import set_current_auth
from server.auth.jwt import AuthInfo
from server.identity.domain.read_scope import (
    bind_request_read_filters,
    resolve_read_filters,
    row_in_read_scope,
)

_V_ROW = SimpleNamespace(
    memory_id="00000000-0000-0000-0000-00000000000v",
    memory_key="k",
    namespace="n",
    agent="bot",
    user_id="V",
    session_id="-",
    value="V's secret",
    pinned=False,
    lock_mode="",
    lock_until=None,
    status="active",
)


def _pinned(user: str = "U") -> AuthInfo:
    return AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_user_id=user,
    )


def _run_as(auth: AuthInfo | None, coro_fn: Any) -> Any:
    """Run `coro_fn()` with `auth` bound the way the transport binds it, in a
    fresh context so nothing leaks into other tests."""

    def go() -> Any:
        set_current_auth(auth)
        if auth is not None:
            bind_request_read_filters(
                auth,
                resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth),
            )
        return asyncio.run(coro_fn())

    return contextvars.Context().run(go)


@pytest.fixture
def store(monkeypatch):
    """A store holding only V's row, answering under the scope it is passed."""
    import server.memory.store as memory_repo

    calls: dict[str, list[Any]] = {"writes": [], "usage": []}

    async def get_by_key(identity, key, *, scope, **kw):
        ok = key == _V_ROW.memory_key and row_in_read_scope(
            scope, _V_ROW.user_id, _V_ROW.session_id
        )
        return _V_ROW if ok else None

    async def get_by_id(memory_id, *, scope, namespace=None, **kw):
        ok = memory_id == _V_ROW.memory_id and row_in_read_scope(
            scope, _V_ROW.user_id, _V_ROW.session_id
        )
        return _V_ROW if ok and namespace in (None, _V_ROW.namespace) else None

    async def get_many_by_keys(identity, keys, *, scope, **kw):
        row = await get_by_key(identity, _V_ROW.memory_key, scope=scope)
        return [row] if row is not None and row.memory_key in keys else []

    async def record_usage_event(**kw):
        calls["usage"].append(kw)

    async def insert_versions(rows, *_, **kw):
        calls["writes"].extend(rows)

    monkeypatch.setattr(memory_repo, "get_by_key", get_by_key)
    monkeypatch.setattr(memory_repo, "get_by_id", get_by_id)
    monkeypatch.setattr(memory_repo, "get_many_by_keys", get_many_by_keys)
    monkeypatch.setattr(memory_repo, "record_usage_event", record_usage_event)
    monkeypatch.setattr("server.memory.app.handlers.insert_memory_versions", insert_versions)
    monkeypatch.setattr(memory_repo, "insert_versions_over_heads", insert_versions)
    return calls


def test_request_scope_is_the_whole_namespace_without_auth() -> None:
    from server.identity.app.request_scope import request_scope

    assert contextvars.Context().run(request_scope).narrowed is False


def test_request_scope_binds_the_pinned_user() -> None:
    from server.identity.app.request_scope import request_scope

    async def read():
        return request_scope()

    scope = _run_as(_pinned("U"), read)
    assert (scope.scope, scope.user_id) == ("own_user", "U")


def test_delete_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app.handlers import handle_memory_delete

    inp = MemoryDeleteInput(org="o", namespace="n", agent="*", user_id="U", memory_key="k")
    out = _run_as(_pinned("U"), lambda: handle_memory_delete(inp))
    assert out.found is False and out.tombstoned is False
    assert store["writes"] == []


def test_lock_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_lock import MemoryLockInput
    from server.memory.app.lock import handle_memory_lock

    inp = MemoryLockInput(
        org="o", namespace="n", agent="*", user_id="U", memory_key="k", mode="read_only", reason="r"
    )
    out = _run_as(_pinned("U"), lambda: handle_memory_lock(inp))
    assert out.locked is False and out.reason == "not_found"
    assert store["writes"] == []


def test_touch_with_wildcard_agent_does_not_find_another_users_row(store) -> None:
    from server.memory.api.models_crud import MemoryTouchInput
    from server.memory.app.handlers import handle_memory_touch

    inp = MemoryTouchInput(
        org="o", namespace="n", agent="*", user_id="U", memory_key="k", ttl_seconds=60
    )
    _run_as(_pinned("U"), lambda: handle_memory_touch(inp))
    assert store["writes"] == []


def test_if_match_probe_does_not_disclose_another_users_hash(store) -> None:
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app.write import _set_check_if_match

    inp = MemorySetInput(
        org="o",
        namespace="n",
        agent="*",
        user_id="U",
        memory_key="k",
        value="x",
        if_match_sha256="0" * 64,
    )
    w = SimpleNamespace(input=inp, existing=None)
    with pytest.raises(HTTPException) as exc:
        _run_as(_pinned("U"), lambda: _set_check_if_match(w))
    assert exc.value.status_code == 409
    assert exc.value.detail["current_sha256"] == ""


@pytest.mark.parametrize("by", ["id", "key"])
def test_outcome_on_another_users_row_is_not_recorded(store, monkeypatch, by) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    class _NoCH:
        def __getattr__(self, name):
            raise AssertionError("no counter may be read or written for a foreign row")

    monkeypatch.setattr(ch_mod, "get_client", lambda: _NoCH())
    target = {"memory_id": _V_ROW.memory_id} if by == "id" else {"memory_key": "k"}
    inp = MemoryOutcomeInput(org="o", namespace="n", outcome="negative", weight=100, **target)
    out = _run_as(_pinned("U"), lambda: handle_memory_outcome(inp))
    assert out.recorded is False and out.quarantined is False
    assert store["usage"] == []


def test_outcome_by_id_in_another_namespace_is_not_recorded(store, monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    monkeypatch.setattr(ch_mod, "get_client", lambda: None)
    inp = MemoryOutcomeInput(
        org="o", namespace="other", memory_id=_V_ROW.memory_id, outcome="positive"
    )
    out = _run_as(None, lambda: handle_memory_outcome(inp))
    assert out.recorded is False
    assert store["usage"] == []


def test_outcome_by_id_in_scope_is_recorded(store, monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.api.models_temporal import MemoryOutcomeInput
    from server.memory.app.ops import handle_memory_outcome

    class _CH:
        async def query_one(self, sql, params=None):
            return {"evidence_count": 1}

    monkeypatch.setattr(ch_mod, "get_client", lambda: _CH())
    monkeypatch.setattr(
        "server.memory.app.recall_credit.note_closer_from_request", lambda *a, **k: None
    )
    inp = MemoryOutcomeInput(org="o", namespace="n", memory_id=_V_ROW.memory_id, outcome="positive")
    out = _run_as(_pinned("V"), lambda: handle_memory_outcome(inp))
    assert out.recorded is True
    assert [e["event_type"] for e in store["usage"]] == ["evidence"]


def test_run_credit_lookup_carries_the_scope() -> None:
    from server.identity.domain.read_scope import ReadFilters
    from server.tasks.store.runs import live_memories_by_key

    seen: dict[str, Any] = {}

    class _CH:
        async def query_all(self, sql, params=None):
            seen.update(sql=sql, params=params)
            return []

    scope = ReadFilters(scope="own_user", user_id="U", session_id=None)
    asyncio.run(live_memories_by_key(_CH(), "o", "n", ["k"], scope=scope))
    assert "user_id = {scope_uid:String}" in seen["sql"]
    assert seen["params"]["scope_uid"] == "U"


class _SqlCH:
    """Records every statement; answers nothing."""

    def __init__(self) -> None:
        self.sql: list[tuple[str, dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.sql.append((sql, dict(params or {})))
        return []


def _scoped_to_u(ch: _SqlCH) -> bool:
    return any("user_id = {scope_uid:String}" in s and p.get("scope_uid") == "U" for s, p in ch.sql)


def test_same_key_supersession_reads_only_the_writers_rows() -> None:
    from server.memory.app.key_supersession import supersede_same_key_rows

    ch = _SqlCH()
    rows = [{"memory_key": "k", "memory_id": "m"}]
    _run_as(
        _pinned("U"),
        lambda: supersede_same_key_rows(
            follows=[], org="o", namespace="n", new_rows=rows, ch=ch, policy="newest_wins"
        ),
    )
    assert _scoped_to_u(ch)


def test_value_update_peers_are_the_writers_rows() -> None:
    from server.memory.app.value_updates import _peers_joined, _peers_many

    ch = _SqlCH()
    _run_as(
        _pinned("U"),
        lambda: _peers_many(
            ch, org="o", namespace="n", items=[("semantic", "k", [0.1])], min_cosine=0.5
        ),
    )
    _run_as(
        _pinned("U"),
        lambda: _peers_joined(
            ch,
            org="o",
            namespace="n",
            memory_type="semantic",
            memory_key="k",
            embedding=[0.1],
            min_cosine=0.5,
        ),
    )
    assert len(ch.sql) == 2
    assert all("m.user_id = {scope_uid:String}" in s for s, _ in ch.sql)


def test_the_per_fact_value_update_read_stays_in_the_writers_scope() -> None:
    from server.memory.app.value_updates import _peers_codes_first

    ch = _SqlCH()
    _run_as(
        _pinned("U"),
        lambda: _peers_codes_first(
            ch,
            org="o",
            namespace="n",
            memory_type="semantic",
            memory_key="k",
            embedding=[0.1],
            min_cosine=0.5,
        ),
    )
    assert len(ch.sql) == 1 and "JOIN" not in ch.sql[0][0]  # stage A, no candidate
    assert _scoped_to_u(ch)


def test_recall_echo_keys_are_the_callers_keys(monkeypatch) -> None:
    import server.core.ch as ch_mod
    from server.memory.app.pipeline import _existing_keys_named
    from server.memory.app.pipeline_state import WorkflowState

    ch = _SqlCH()
    monkeypatch.setattr(ch_mod, "get_client", lambda: ch)
    state = WorkflowState(org="o", namespace="n", user_id="U")
    _run_as(_pinned("U"), lambda: _existing_keys_named(state, {"k"}))
    assert _scoped_to_u(ch)


def test_run_retention_touches_only_the_callers_rows() -> None:
    from server.memory.app.run_retention import load_run_rows

    ch = _SqlCH()
    _run_as(_pinned("U"), lambda: load_run_rows(ch, org="o", namespace="n", run_id="r", limit=5))
    assert _scoped_to_u(ch)


def test_bulk_dedup_keeper_is_in_the_callers_session(monkeypatch) -> None:
    from server.memory.api.models_bulk import MemoryBulkSetInput
    from server.memory.app.bulk_set import _bulk_identity_dupes
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_bulk_dedup_scope", "identity")
    auth = AuthInfo(
        subject="sa:bot",
        org="o",
        namespaces=("n",),
        roles=("writer",),
        is_service_account=True,
        read_scope="own_session",
    )
    inp = MemoryBulkSetInput(
        org="o",
        namespace="n",
        agent="a",
        user_id="U",
        session_id="S",
        entries=[{"memory_key": "k", "value": "v"}],
    )
    ch = _SqlCH()

    def go() -> Any:
        set_current_auth(auth)
        bind_request_read_filters(
            auth, resolve_read_filters(requested=None, user_id="U", session_id="S", auth=auth)
        )
        return asyncio.run(_bulk_identity_dupes(ch, inp))

    contextvars.Context().run(go)
    sql, params = ch.sql[0]
    assert "session_id = {scope_sid:String}" in sql and params["scope_sid"] == "S"


@pytest.mark.parametrize(
    "op",
    [
        "memory.record.write",
        "decision.record",
        "procedure.record_run",
        "conversation.create",
        "conversation.append",
        "context.offload",
    ],
)
def test_writes_to_tables_keyed_without_a_user_are_refused_when_narrowed(op: str) -> None:
    from server.identity.domain.read_scope import honours_read_scope

    assert not honours_read_scope(op)


@pytest.fixture
def runs(monkeypatch):
    """A run ledger holding V's run `r-v`."""
    from server.tasks.app import run as run_app

    v_run = {"run_id": "r-v", "user_id": "V", "session_id": "-", "agent": "bot"}
    v_run["started_at"] = run_app._now()
    inserted: list[dict[str, Any]] = []

    async def fetch_run(ch, org, ns, run_id):
        return dict(v_run) if run_id == "r-v" else None

    async def insert_runs(ch, rows):
        inserted.extend(rows)

    monkeypatch.setattr(run_app, "get_client", lambda: None)
    monkeypatch.setattr(run_app.runs_store, "fetch_run", fetch_run)
    monkeypatch.setattr(run_app.runs_store, "insert_runs", insert_runs)
    return inserted


def test_run_begin_on_another_users_run_starts_a_new_run(runs) -> None:
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import handle_run_begin

    inp = RunBeginInput(org="o", namespace="n", agent="bot", user_id="U", run_id="r-v", goal="g")
    out = _run_as(_pinned("U"), lambda: handle_run_begin(inp))
    assert out.run_id != "r-v"
    assert [r["run_id"] for r in runs] == [out.run_id] and runs[0]["user_id"] == "U"
    # V itself (and an unbounded caller) re-begins its own run.
    out = _run_as(_pinned("V"), lambda: handle_run_begin(inp.model_copy(update={"user_id": "V"})))
    assert out.run_id == "r-v"


class _KeeperRunsCH:
    """agent_runs as last-write-wins rows and agent_run_claims as a strict
    KeeperMap: a second INSERT of a claim key raises "Node exists"."""

    def __init__(self) -> None:
        self.claims: dict[str, dict[str, str]] = {}

    async def query_one(self, sql: str, params: Any = None) -> Any:
        if "system.tables" in sql:
            return {"engine": "KeeperMap"}
        return self.claims.get(params["k"])

    async def execute(self, sql: str, params: Any = None) -> None:
        await asyncio.sleep(0)
        if "DELETE" in sql:
            self.claims.pop(params["k"], None)
            return
        if params["k"] in self.claims:
            raise RuntimeError("Code: 999. Coordination::Exception: Node exists")
        self.claims[params["k"]] = {"user_id": params["uid"], "session_id": params["sid"]}


@pytest.fixture
def keeper_runs(monkeypatch):
    """A Keeper-backed run ledger whose existence check yields, so two begins
    both see the run absent before either writes."""
    from server.tasks.app import run as run_app

    ch = _KeeperRunsCH()
    final: dict[str, dict[str, Any]] = {}

    async def fetch_run(ch, org, ns, run_id):
        row = final.get(run_id)
        await asyncio.sleep(0)
        return row

    async def insert_runs(ch, rows):
        final.update({r["run_id"]: dict(r) for r in rows})

    monkeypatch.setattr(run_app, "get_client", lambda: ch)
    monkeypatch.setattr(run_app.runs_store, "_claims_atomic", None, raising=False)
    monkeypatch.setattr(run_app.runs_store, "fetch_run", fetch_run)
    monkeypatch.setattr(run_app.runs_store, "insert_runs", insert_runs)
    return final


async def _begin_as(auth: AuthInfo, user: str, goal: str) -> str:
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import handle_run_begin

    set_current_auth(auth)
    bind_request_read_filters(
        auth, resolve_read_filters(requested=None, user_id=None, session_id=None, auth=auth)
    )
    inp = RunBeginInput(
        org="o", namespace="n", agent="bot", user_id=user, run_id="shared", goal=goal
    )
    return (await handle_run_begin(inp)).run_id


def test_concurrent_begins_on_one_run_id_keep_both_runs(keeper_runs) -> None:
    async def both() -> list[str]:
        return list(
            await asyncio.gather(
                _begin_as(_pinned("U"), "U", "U's goal"), _begin_as(_pinned("V"), "V", "V's goal")
            )
        )

    ids = contextvars.Context().run(lambda: asyncio.run(both()))
    assert ids.count("shared") == 1 and len(set(ids)) == 2
    for run_id, user in zip(ids, ("U", "V"), strict=True):
        assert keeper_runs[run_id]["user_id"] == user
        assert keeper_runs[run_id]["goal"] == f"{user}'s goal"


def test_a_re_sent_begin_keeps_its_claimed_run_id(keeper_runs) -> None:
    async def twice() -> list[str]:
        return [await _begin_as(_pinned("U"), "U", "g"), await _begin_as(_pinned("U"), "U", "g")]

    assert contextvars.Context().run(lambda: asyncio.run(twice())) == ["shared", "shared"]


def test_a_wide_caller_does_not_take_a_narrowed_users_claim(keeper_runs) -> None:
    wide = AuthInfo(
        subject="sa:bot", org="o", namespaces=("n",), roles=("writer",), is_service_account=True
    )

    async def go() -> list[str]:
        return list(
            await asyncio.gather(_begin_as(_pinned("U"), "U", "g"), _begin_as(wide, "-", "g"))
        )

    ids = contextvars.Context().run(lambda: asyncio.run(go()))
    assert ids.count("shared") == 1 and len(set(ids)) == 2
    for run_id, user in zip(ids, ("U", "-"), strict=True):
        assert keeper_runs[run_id]["user_id"] == user


def test_a_wide_caller_later_starts_a_new_run_on_a_narrowed_users_run(keeper_runs) -> None:
    """After U's begin has released its claim, the row check applies the claim's
    rule: a begin re-opens only its own user's run, so the wide caller gets a fresh id."""
    wide = AuthInfo(
        subject="sa:bot", org="o", namespaces=("n",), roles=("writer",), is_service_account=True
    )

    async def go() -> tuple[str, str, str]:
        first = await _begin_as(_pinned("U"), "U", "g")
        other = await _begin_as(wide, "-", "g")
        same_user = await _begin_as(wide, "U", "g")
        return first, other, same_user

    first, other, same_user = contextvars.Context().run(lambda: asyncio.run(go()))
    assert first == "shared" and other != "shared" and same_user == "shared"
    assert keeper_runs["shared"]["user_id"] == "U"


def test_run_complete_on_another_users_run_is_not_found(runs) -> None:
    from server.core.errors import NotFoundError
    from server.tasks.api.models_run import RunCompleteInput
    from server.tasks.app.run import handle_run_complete

    inp = RunCompleteInput(org="o", namespace="n", run_id="r-v", status="completed")
    with pytest.raises(NotFoundError) as foreign:
        _run_as(_pinned("U"), lambda: handle_run_complete(inp))
    with pytest.raises(NotFoundError) as missing:
        _run_as(_pinned("U"), lambda: handle_run_complete(inp.model_copy(update={"run_id": "x"})))
    assert str(foreign.value).replace("r-v", "x") == str(missing.value)
    assert runs == []


def test_set_from_messages_skips_its_record_projection_when_narrowed(monkeypatch) -> None:
    from server.memory.app import records
    from server.memory.app.inline_records import _write_inline_record_projection

    written: list[Any] = []

    async def record_write(inp):
        written.append(inp)

    async def schema_register(inp):
        return None

    monkeypatch.setattr(records, "handle_memory_record_write", record_write)
    monkeypatch.setattr(records, "handle_memory_schema_register", schema_register)
    inp = SimpleNamespace(
        org="o", namespace="n", agent="a", user_id="U", session_id="-", memory_key="k"
    )

    def project() -> Any:
        return _write_inline_record_projection(inp, {"f": "x"}, {"f": "string"}, evidence_ids=[])

    assert _run_as(_pinned("U"), project) is False
    assert written == []
    assert _run_as(None, project) is True
    assert [w.memory_key for w in written] == ["k"]


_V_PINNED_ROW = {
    "memory_id": "v1",
    "memory_key": "v-fact",
    "value": "V lives in Oslo",
    "pinned": 1,
    "agent": "bot",
    "user_id": "V",
    "session_id": "-",
    "run_id": "-",
    "valid_at": None,
    "dist": 0.05,
}


def _type(r: dict[str, Any]) -> str:
    return str(r.get("memory_type") or "semantic")


def _ident_only(r: dict[str, Any]) -> dict[str, Any]:
    return {k: r[k] for k in ("agent", "user_id", "session_id", "memory_key", "dist")}


def _vector_type(r: dict[str, Any]) -> str:
    """The type on the row's embedding row; it can disagree with the memory row's."""
    return str(r.get("vector_type") or _type(r))


class _NamespaceCH(_SqlCH):
    """Answers the peer statements from `rows` (the namespace) the way ClickHouse
    would: a scoped statement sees only U's rows, each statement keeps its own
    LIMIT, stage B returns the ids it is asked for."""

    rows: list[dict[str, Any]] = []

    async def query_all(self, sql, params=None):
        await super().query_all(sql, params)
        p = params or {}
        rows = sorted(self.rows, key=lambda r: r["dist"])
        if "scope_uid" in sql:
            rows = [r for r in rows if r["user_id"] == p.get("scope_uid")]
        if "m.pinned = 1" in sql:  # every pinned memory row of the type, by distance
            pinned = [r for r in rows if r["pinned"] and _type(r) == "semantic"]
            pinned = [r for r in pinned if r["dist"] <= 0.3][:10]
            if "BY qidx" in sql:  # every fact of the batch, as its own `qidx`
                return [{**r, "qidx": q} for q in range(len(p["keys"])) for r in pinned]
            return pinned
        if "IN {ids:" in sql:  # stage B filters the type
            ids = {tuple(i) for i in p["ids"]}
            return [
                r
                for r in self.rows
                if tuple(_ident_only(r).values())[:4] in ids and _type(r) == p.get("mt")
            ]
        if "LIMIT {k:UInt32}" in sql:  # stage A ranks every vector, any type
            if "memory_type = {mt:String}" in sql:  # a vector-side type filter
                rows = [r for r in rows if _vector_type(r) == p.get("mt")]
            return [_ident_only(r) for r in rows[: int(p["k"])]]
        by_qidx = re.search(r"LIMIT (\d+) BY qidx", sql)
        if by_qidx:  # the batched candidates filter the type before the LIMIT
            typed = [r for r in rows if _type(r) == "semantic"]
            return [{**r, "qidx": 0} for r in typed[: int(by_qidx.group(1))]]
        return []


class _PinnedV(_NamespaceCH):
    """The namespace holds only V's pinned row, close to the write."""

    rows = [_V_PINNED_ROW]


def _check_on_commit(monkeypatch, ch: _SqlCH) -> Any:
    from server.memory.app import conflicts

    monkeypatch.setattr(conflicts, "get_client", lambda: ch)
    return conflicts.check_on_commit(
        org="o",
        namespace="n",
        agent="bot",
        memory_type="semantic",
        memory_key="k",
        new_value="U lives in Lisbon",
        new_embedding=[0.1, 0.2],
        resolved_threshold=0.85,
        resolved_duplicate_threshold=0.9,
        resolved_policy="auto_reinforce",
        allowlisted_keys=set(),
    )


def test_conflict_peers_are_the_writers_and_a_foreign_pinned_row_still_blocks(
    monkeypatch,
) -> None:
    ch = _PinnedV()
    out = _run_as(_pinned("U"), lambda: _check_on_commit(monkeypatch, ch))
    stage_a = [s for s, _ in ch.sql if "LIMIT {k:UInt32}" in s]
    # The writer's own stage A, then the namespace's pinned rows, unscoped.
    assert ["scope_uid" in s for s in stage_a] == [True]
    pinned = [s for s, _ in ch.sql if "m.pinned = 1" in s]
    assert len(pinned) == 1 and "scope_uid" not in pinned[0]
    assert out.verdict == "rejected" and out.pinned_blocker_id == "v1"
    assert all(p.memory_id != "v1" for p in out.peers), "the foreign row is never named"


def test_a_namespace_wide_writer_runs_no_separate_pinned_statement(monkeypatch) -> None:
    ch = _PinnedV()
    out = _run_as(None, lambda: _check_on_commit(monkeypatch, ch))
    assert len([s for s, _ in ch.sql if "LIMIT {k:UInt32}" in s]) == 1
    assert out.verdict == "rejected" and out.pinned_blocker_id == "v1"


def test_batched_conflict_candidates_are_the_writers_rows() -> None:
    from server.memory.app.conflicts import candidate_rows_many

    ch = _SqlCH()
    _run_as(
        _pinned("U"),
        lambda: candidate_rows_many(
            org="o", namespace="n", items=[("semantic", "k", [0.1], 0.3)], ch=ch
        ),
    )
    candidates, pinned = ch.sql
    assert (
        "m.user_id = {scope_uid:String}" in candidates[0] and "m.pinned = 1 OR" not in candidates[0]
    )
    # Other users' pinned rows come from their own unscoped statement.
    assert "scope_uid" not in pinned[0] and "m.pinned = 1" in pinned[0]
    assert "LIMIT 10 BY qidx" in pinned[0]


def test_a_namespace_wide_bulk_writer_runs_no_pinned_statement() -> None:
    from server.memory.app.conflicts import candidate_rows_many

    ch = _SqlCH()
    _run_as(
        None,
        lambda: candidate_rows_many(
            org="o", namespace="n", items=[("semantic", "k", [0.1], 0.3)], ch=ch
        ),
    )
    assert len(ch.sql) == 1 and "LIMIT 10 BY qidx" in ch.sql[0][0]


def _own_row(i: int) -> dict[str, Any]:
    return {
        **_V_PINNED_ROW,
        "memory_id": f"u{i}",
        "memory_key": f"u-fact-{i}",
        "value": f"U lives in Lisbon, note number {i}",
        "pinned": 0,
        "user_id": "U",
        "dist": 0.01,
    }


class _CloserOwnRows(_NamespaceCH):
    """Ten of U's own rows sit closer to the write than V's pinned row, so a
    statement that keeps only the 10 nearest drops V's row."""

    rows = [_own_row(i) for i in range(10)] + [_V_PINNED_ROW]


class _ManyCloserOwnRows(_NamespaceCH):
    """40 of U's own rows sit closer than V's pinned row: more than any nearest-N window."""

    rows = [_own_row(i) for i in range(40)] + [_V_PINNED_ROW]


class _PinnedRowWithAnotherTypesVector(_NamespaceCH):
    """V's live pinned memory row is semantic, its embedding row says episodic: the
    memory row decides the type, so it still blocks a semantic write."""

    rows = [{**_V_PINNED_ROW, "vector_type": "episodic"}]


class _CloserOwnRowsOfBothTypes(_NamespaceCH):
    """Ten of U's semantic rows and 22 of its episodic rows sit closer than V's pinned
    semantic row: stage A's first window (32, any type) holds none of V's row."""

    rows = (
        [_own_row(i) for i in range(10)]
        + [{**_own_row(100 + i), "memory_type": "episodic", "dist": 0.02} for i in range(22)]
        + [_V_PINNED_ROW]
    )


@pytest.mark.parametrize(
    "namespace_rows",
    [
        _CloserOwnRows,
        _ManyCloserOwnRows,
        _CloserOwnRowsOfBothTypes,
        _PinnedRowWithAnotherTypesVector,
    ],
)
def test_bulk_and_single_both_reject_a_write_close_to_a_foreign_pinned_row(
    monkeypatch, namespace_rows: type[_NamespaceCH]
) -> None:
    from server.memory.app import conflicts

    ch = namespace_rows()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)

    def check(candidate_rows: Any) -> Any:
        return conflicts.check_on_commit(
            org="o",
            namespace="n",
            agent="bot",
            memory_type="semantic",
            memory_key="k",
            new_value="U lives in Lisbon",
            new_embedding=[0.1, 0.2],
            candidate_rows=candidate_rows,
            resolved_threshold=0.85,
            resolved_duplicate_threshold=0.9,
            resolved_policy="auto_reinforce",
            allowlisted_keys=set(),
        )

    async def bulk() -> Any:
        rows = await conflicts.candidate_rows_many(
            org="o", namespace="n", items=[("semantic", "k", [0.1, 0.2], 0.3)], ch=ch
        )
        return await check(rows[0])

    single = _run_as(_pinned("U"), lambda: check(None))
    batched = _run_as(_pinned("U"), bulk)
    assert single.verdict == batched.verdict == "rejected"
    assert single.pinned_blocker_id == batched.pinned_blocker_id == "v1"


class _OwnPinnedRowBeyondTen(_NamespaceCH):
    """Ten of U's rows sit nearest, U's own pinned row is the 11th."""

    rows = [_own_row(i) for i in range(10)] + [
        {**_own_row(10), "memory_id": "u-pinned", "pinned": 1, "dist": 0.05}
    ]


class _OwnPinnedRowBeyondTenAndVs(_NamespaceCH):
    rows = _OwnPinnedRowBeyondTen.rows + [{**_V_PINNED_ROW, "dist": 0.06}]


def test_a_narrowed_writers_own_rows_get_the_namespace_wide_verdict(monkeypatch) -> None:
    """The pinned arm adds only other users' pinned rows: the writer's own pinned
    row past its 10 places does not block, as it does not for a namespace-wide
    writer; V's pinned row still does."""
    wide = _run_as(None, lambda: _check_on_commit(monkeypatch, _OwnPinnedRowBeyondTen()))
    own = _run_as(_pinned("U"), lambda: _check_on_commit(monkeypatch, _OwnPinnedRowBeyondTen()))
    assert own.verdict == wide.verdict != "rejected"
    assert [p.memory_id for p in own.peers] == [p.memory_id for p in wide.peers]
    out = _run_as(
        _pinned("U"), lambda: _check_on_commit(monkeypatch, _OwnPinnedRowBeyondTenAndVs())
    )
    assert out.verdict == "rejected" and out.pinned_blocker_id == "v1"

    async def bulk() -> Any:
        from server.memory.app import conflicts

        rows = await conflicts.candidate_rows_many(
            org="o",
            namespace="n",
            items=[("semantic", "k", [0.1, 0.2], 0.3)],
            ch=_OwnPinnedRowBeyondTen(),
        )
        return rows[0]

    assert all(r["memory_id"] != "u-pinned" for r in _run_as(_pinned("U"), bulk))


def test_a_bulk_fact_with_prefetched_rows_runs_no_pinned_statement(monkeypatch) -> None:
    from server.memory.app import conflicts

    ch = _PinnedV()
    monkeypatch.setattr(conflicts, "get_client", lambda: ch)

    def check() -> Any:
        return conflicts.check_on_commit(
            org="o",
            namespace="n",
            agent="bot",
            memory_type="semantic",
            memory_key="k",
            new_value="U lives in Lisbon",
            new_embedding=[0.1, 0.2],
            candidate_rows=[dict(_V_PINNED_ROW)],
            resolved_threshold=0.85,
            resolved_policy="auto_reinforce",
            allowlisted_keys=set(),
        )

    out = _run_as(_pinned("U"), check)
    assert ch.sql == [], "one pinned read per batch, never one per fact"
    assert out.verdict == "rejected" and out.pinned_blocker_id == "v1"
    assert all(p.memory_id != "v1" for p in out.peers)


def test_request_narrowed_follows_the_bound_scope() -> None:
    from server.identity.app.request_scope import request_narrowed

    async def narrowed() -> bool:
        return request_narrowed()

    assert _run_as(None, narrowed) is False
    assert _run_as(_pinned("U"), narrowed) is True


def test_a_re_sent_begin_without_a_keeper_lands_on_the_same_run(monkeypatch) -> None:
    """No atomic claim: a narrowed caller's new id becomes its own server-made id. A
    re-sent begin (the first's answer was lost) gets that same id and re-opens the
    run, so the ledger holds one run, not two; another user asking for the same id
    gets a different one."""
    from server.tasks.app import run as run_app

    final: dict[str, dict[str, Any]] = {}

    async def fetch_run(ch, org, ns, run_id):
        return final.get(run_id)

    async def insert_runs(ch, rows):
        final.update({r["run_id"]: dict(r) for r in rows})

    async def no_claim(*_a, **_k):
        return run_app.runs_store.NoClaim.UNAVAILABLE

    monkeypatch.setattr(run_app, "get_client", lambda: None)
    monkeypatch.setattr(run_app.runs_store, "fetch_run", fetch_run)
    monkeypatch.setattr(run_app.runs_store, "insert_runs", insert_runs)
    monkeypatch.setattr(run_app.runs_store, "claim_run", no_claim)

    first = asyncio.run(_begin_as(_pinned("U"), "U", "g"))
    again = asyncio.run(_begin_as(_pinned("U"), "U", "g"))
    other = asyncio.run(_begin_as(_pinned("V"), "V", "g"))
    assert first == again != "shared"
    assert other not in (first, "shared")
    assert sorted(final) == sorted({first, other})


def test_a_begin_re_sent_after_the_keeper_returns_re_opens_its_own_run(monkeypatch) -> None:
    """The first begin had no claim and went to the caller's own id; its answer was
    lost and the Keeper came back before the retry. The retry wins the claim on the
    requested id but re-opens the first run: same id, same start, one ledger row."""
    from server.tasks.app import run as run_app

    final: dict[str, dict[str, Any]] = {}
    claims = iter(
        [
            run_app.runs_store.NoClaim.UNAVAILABLE,
            run_app.runs_store.RunClaim(won=True, user_id="U", session_id="-"),
        ]
    )

    async def fetch_run(ch, org, ns, run_id):
        return final.get(run_id)

    async def insert_runs(ch, rows):
        final.update({r["run_id"]: dict(r) for r in rows})

    async def claim(*_a, **_k):
        return next(claims)

    async def delete_claim(*_a, **_k):
        return None

    monkeypatch.setattr(run_app, "get_client", lambda: None)
    monkeypatch.setattr(run_app.runs_store, "fetch_run", fetch_run)
    monkeypatch.setattr(run_app.runs_store, "insert_runs", insert_runs)
    monkeypatch.setattr(run_app.runs_store, "claim_run", claim)
    monkeypatch.setattr(run_app.runs_store, "delete_claim", delete_claim)

    first = asyncio.run(_begin_as(_pinned("U"), "U", "g"))
    started = final[first]["started_at"]
    again = asyncio.run(_begin_as(_pinned("U"), "U", "g"))
    assert again == first != "shared"
    assert list(final) == [first]
    assert final[first]["started_at"] == started


def test_fallback_run_ids_do_not_collide_across_a_separator_in_the_identity() -> None:
    from server.tasks.api.models_run import RunBeginInput
    from server.tasks.app.run import _own_run_id

    def own(user: str, session: str) -> str:
        inp = RunBeginInput(
            org="o", namespace="n", agent="a", user_id=user, session_id=session, goal="g"
        )
        return _own_run_id(inp, "req")

    assert own("u\x1fs", "t") != own("u", "s\x1ft")
