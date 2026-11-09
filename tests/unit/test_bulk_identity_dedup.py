"""An identical fact under a new key or a new session is a reinforcement,
not a row.

The bulk write path's byte-identical gate spans the whole identity, not one
session: a live active row anywhere in the same identity with the same
content_sha256 is the fact, so a fact re-observed every session or extracted
under a key variant reinforces it and is not written.
"""

from __future__ import annotations

from typing import Any

import pytest

from server.core.hashing import sha256_value
from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from server.memory.app.extract import handler as mem_extract
from tests.unit import clickhouse_local
from tests.unit.memory_rows import projection_row

VALUE = "User's task branch is master."


class _StubCH:
    def __init__(self, keeper: tuple[str, str] | None, *, fail_lookup: bool = False) -> None:
        self.keeper = keeper
        self.fail_lookup = fail_lookup
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []
        self.lookups: list[tuple[str, dict[str, Any]]] = []

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "content_sha256 IN" in sql:
            self.lookups.append((sql, params or {}))
            if self.fail_lookup:
                raise RuntimeError("Code: 241. DB::Exception: memory limit")
            if self.keeper is None:
                return []
            key, mid = self.keeper
            return [{"head_sha": sha256_value(VALUE), "memory_key": key, "memory_id": mid}]
        return []

    async def insert(
        self, table: str, rows: list[dict[str, Any]], settings: dict[str, Any] | None = None
    ) -> None:
        self.insert_calls.append((table, rows, settings or {}))


class _StubEmbedder:
    model_name = "fake-test"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch):
    events: list[dict[str, Any]] = []

    def make(ch: _StubCH) -> list[dict[str, Any]]:
        monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
        monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

        async def _none(*_: Any, **__: Any) -> Any:
            return None

        async def _empty(*_: Any, **__: Any) -> dict[str, str]:
            return {}

        async def _record(**kw: Any) -> None:
            events.append(kw)

        monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
        monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
        monkeypatch.setattr(
            "server.memory.app.entity_resolution.resolve_entities_for_write", _empty
        )
        monkeypatch.setattr("server.memory.store.insert_embedding_rows", _none)
        monkeypatch.setattr("server.memory.store.usage.record_usage_event", _record)
        for name in ("write_entity_vectors", "write_entity_edges", "write_entity_triples"):
            monkeypatch.setattr(_bulk_set_mod, name, _none)
        return events

    return make


def _input(key: str = "user_clickhouse_task_branch", session: str = "s-new") -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id=session,
        entries=[_BulkSetEntry(memory_key=key, memory_type="semantic", value=VALUE)],
    )


def _memory_rows(ch: _StubCH) -> list[dict[str, Any]]:
    return [r for table, rows, _ in ch.insert_calls if table == "loom.memory" for r in rows]


@pytest.mark.asyncio
async def test_same_content_under_another_key_reinforces_the_keeper_and_writes_nothing(
    harness,
) -> None:
    ch = _StubCH(keeper=("user_task_branch", "mid-keeper"))
    events = harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert _memory_rows(ch) == []
    assert out.identity_duplicates == {"user_clickhouse_task_branch": "user_task_branch"}
    assert [r.memory_key for r in out.written] == ["user_clickhouse_task_branch"]
    assert out.merged == ["user_clickhouse_task_branch"]  # the point path's name for a restatement
    assert out.written[0].version == 0
    assert [(e["memory_id"], e["event_type"]) for e in events] == [("mid-keeper", "evidence")]
    sql, params = ch.lookups[0]
    # any session, any key: no session or key filter is bound
    assert set(params) == {"org", "ns", "agent", "uid", "shas", "live"}
    assert "select_sequential_consistency = 1" in sql  # a lagging replica must not hide the keeper
    assert "IN {live:Array(String)}" in sql  # any live status is the same fact
    assert params["live"] == ["active", "duplicate", "conflicted"]
    assert params["shas"] == [sha256_value(VALUE)] and params["uid"] == "u"


@pytest.mark.asyncio
async def test_same_key_in_a_new_session_is_also_a_reinforcement(harness) -> None:
    ch = _StubCH(keeper=("user_task_branch", "mid-keeper"))
    events = harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input(key="user_task_branch"))
    assert _memory_rows(ch) == [] and out.identity_duplicates == {
        "user_task_branch": "user_task_branch"
    }
    assert len(events) == 1


@pytest.mark.asyncio
async def test_new_content_is_written_as_before(harness) -> None:
    ch = _StubCH(keeper=None)
    events = harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert len(_memory_rows(ch)) == 1 and out.identity_duplicates == {} and events == []


@pytest.mark.asyncio
async def test_lookup_failure_is_fail_open(harness) -> None:
    ch = _StubCH(keeper=("user_task_branch", "mid-keeper"), fail_lookup=True)
    harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert len(_memory_rows(ch)) == 1 and out.identity_duplicates == {}


@pytest.mark.asyncio
async def test_session_scope_restores_the_old_gate(
    harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.settings import settings

    monkeypatch.setattr(settings, "memory_bulk_dedup_scope", "session")
    ch = _StubCH(keeper=("user_task_branch", "mid-keeper"))
    harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert ch.lookups == [] and len(_memory_rows(ch)) == 1 and out.identity_duplicates == {}


def test_default_scope_is_identity() -> None:
    from server.settings import Settings

    assert Settings.model_fields["memory_bulk_dedup_scope"].default == "identity"


class _StubCHWithLiveKey(_StubCH):
    """The per-key lookup also finds the live row: same key, same bytes."""

    def __init__(self, live_key: str, live_id: str) -> None:
        super().__init__(keeper=None)
        self.live_key, self.live_id = live_key, live_id

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        if "IN {keys:Array(Tuple(" in sql:  # the batched live-row lookup
            return [
                projection_row(
                    org="test",
                    namespace="personal",
                    agent="cursor",
                    user_id="u",
                    session_id="s-new",
                    memory_key=self.live_key,
                    memory_id=self.live_id,
                    content_sha256=sha256_value(VALUE),
                )
            ]
        return await super().query_all(sql, params)


@pytest.mark.asyncio
async def test_same_key_same_bytes_reinforces_the_live_row_instead_of_vanishing(harness) -> None:
    """A byte-identical restatement of the same key records the evidence
    event the fold turns into the search-side reinforcement — and still
    writes nothing."""
    ch = _StubCHWithLiveKey("user_clickhouse_task_branch", "mid-live")
    events = harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert _memory_rows(ch) == []  # not rewritten, not re-embedded
    assert out.written[0].version == 0
    assert [e["memory_id"] for e in events] == ["mid-live"]
    assert events[0]["event_type"] == "evidence" and events[0]["event_count"] == 1


def _version_row(
    key: str,
    value: str,
    version: int,
    *,
    session: str = "s-old",
    status: str = "active",
    tombstoned: bool = False,
    memory_id: str = "",
) -> str:
    quoted = value.replace("'", "\\'")
    tombstoned_at = "now()" if tombstoned else "NULL"
    mid = f"'{memory_id}'" if memory_id else "generateUUIDv4()"
    return (
        "INSERT INTO loom.memory (org, namespace, agent, user_id, session_id, memory_key, "
        "memory_id, value, content_sha256, status, tombstoned_at, version) VALUES ('test', "
        f"'personal', 'cursor', 'u', '{session}', '{key}', {mid}, '{quoted}', "
        f"'{sha256_value(value)}', '{status}', {tombstoned_at}, {version})"
    )


def _versions_ch(*rows: str) -> clickhouse_local.LocalCH:
    """loom.memory holding `rows` as separate parts: merges are stopped, so a superseded
    version stays in the table the way it does until ClickHouse merges it away."""
    return clickhouse_local.LocalCH(
        clickhouse_local.ddl("memory"), "SYSTEM STOP MERGES loom.memory", *rows
    )


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_superseded_version_with_the_same_bytes_is_not_a_keeper() -> None:
    """K held VALUE, then was revised; until the parts merge the old version is still
    in the table. K's current value is compared, so only a key that holds VALUE now
    is the keeper."""
    ch = _versions_ch(
        _version_row("user_task_branch", VALUE, 1),
        _version_row("user_task_branch", "User's task branch is main.", 2),
        _version_row("user_task_note", VALUE, 1, session="s-other"),
    )
    got = await _bulk_set_mod._bulk_identity_dupes(ch, _input())
    assert [key for key, _ in got.values()] == ["user_task_note"]


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_key_whose_current_value_holds_the_bytes_is_the_keeper() -> None:
    """The reverse revision: K's newest version holds VALUE, so it is the keeper,
    under any key and any session."""
    ch = _versions_ch(
        _version_row("user_task_branch", "User's task branch is main.", 1),
        _version_row("user_task_branch", VALUE, 2),
        _version_row("other_key", "unrelated", 1, session="s-other"),
    )
    got = await _bulk_set_mod._bulk_identity_dupes(ch, _input())
    assert [key for key, _ in got.values()] == ["user_task_branch"]


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_a_key_deleted_or_retired_since_is_not_a_keeper() -> None:
    """A key whose newest version is a tombstone or no longer live is not the fact,
    though its newest version still carries the bytes."""
    ch = _versions_ch(
        _version_row("user_task_branch", VALUE, 1),
        _version_row("user_task_branch", VALUE, 2, tombstoned=True),
        _version_row("user_task_alt", VALUE, 1),
        _version_row("user_task_alt", VALUE, 2, status="superseded"),
        _version_row("user_task_note", VALUE, 1, session="s-other"),
    )
    got = await _bulk_set_mod._bulk_identity_dupes(ch, _input())
    assert [key for key, _ in got.values()] == ["user_task_note"]


@pytest.mark.skipif(clickhouse_local.CLICKHOUSE is None, reason="clickhouse binary not installed")
@pytest.mark.asyncio
async def test_the_same_key_in_two_sessions_picks_one_keeper_every_time() -> None:
    """Two sessions hold the same key and bytes: the keeper is the lower session's
    row, so a retry reinforces the same memory_id."""
    first = "11111111-1111-1111-1111-111111111111"
    second = "22222222-2222-2222-2222-222222222222"
    ch = _versions_ch(
        _version_row("user_task_branch", VALUE, 1, session="z", memory_id=second),
        _version_row("user_task_branch", VALUE, 1, session="a", memory_id=first),
    )
    got = await _bulk_set_mod._bulk_identity_dupes(ch, _input())
    assert list(got.values()) == [("user_task_branch", first)]


@pytest.mark.asyncio
async def test_under_postgres_a_keeper_is_one_the_authority_still_holds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse may not hold a rewrite or a delete yet: a keeper whose head on the
    authority holds other bytes, or none, is not one, and its entry is written."""
    from server.memory.app import bulk_set
    from server.memory.store import pg_memory

    sha = sha256_value(VALUE)
    found = [
        {"head_sha": sha, "session_id": "s1", "memory_key": "kept", "memory_id": "m1"},
        {"head_sha": sha, "session_id": "s1", "memory_key": "rewritten", "memory_id": "m2"},
        {"head_sha": sha, "session_id": "s1", "memory_key": "deleted", "memory_id": "m3"},
    ]

    async def _heads(keys: Any) -> dict[Any, Any]:
        held = {"kept": {"content_sha256": sha}, "rewritten": {"content_sha256": "b" * 64}}
        # the authority's id: a key re-set since keeps its bytes under a new one
        return {
            k: {"status": "active", "memory_id": f"auth-{k[5]}", **held[k[5]]}
            for k in keys
            if k[5] in held
        }

    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    kept = await bulk_set._keepers_on_the_authority(_input(), found)
    assert [(r["memory_key"], r["memory_id"]) for r in kept] == [("kept", "auth-kept")]


@pytest.mark.asyncio
async def test_under_postgres_a_keeper_clickhouse_does_not_hold_yet_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A keeper written a moment ago answered at its commit, before ClickHouse held it:
    the authority's undelivered revisions add it, in the caller's read scope, and it is
    checked against its head like the keepers ClickHouse found."""
    from server.identity.domain.read_scope import ReadFilters
    from server.memory.app import bulk_set
    from server.memory.store import pg_memory

    sha = sha256_value(VALUE)
    scope = ReadFilters(scope="own_session", user_id="u", session_id="s-new")
    asked: list[Any] = []

    async def _unpublished(*args: Any, scope: Any) -> list[dict[str, Any]]:
        asked.append((*args, scope))
        return [
            {"head_sha": sha, "session_id": "s1", "memory_key": "just_written", "memory_id": "m1"},
            {"head_sha": sha, "session_id": "s1", "memory_key": "rewritten", "memory_id": "m2"},
        ]

    async def _heads(keys: Any) -> dict[Any, Any]:
        held = {"just_written": sha, "rewritten": "b" * 64}
        return {
            k: {"status": "active", "memory_id": f"auth-{k[5]}", "content_sha256": held[k[5]]}
            for k in keys
        }

    class _ClickHouseHoldsNone:
        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return []

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(bulk_set, "request_scope", lambda: scope)
    monkeypatch.setattr(pg_memory, "unpublished_keepers", _unpublished)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    got = await bulk_set._bulk_identity_dupes(_ClickHouseHoldsNone(), _input())
    assert got == {sha: ("just_written", "auth-just_written")}
    assert asked == [("test", "personal", "cursor", "u", [sha], scope)]


@pytest.mark.asyncio
async def test_under_postgres_a_key_rewritten_a_moment_ago_keeps_its_new_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClickHouse still shows the key's old bytes while its rewrite is undelivered: a batch
    carrying both finds the key holding the new ones, not a copy of the old."""
    from server.memory.app import bulk_set
    from server.memory.store import pg_memory

    old, new = "the old fact", "the new fact"
    old_sha, new_sha = sha256_value(old), sha256_value(new)

    async def _unpublished(*_a: Any, scope: Any) -> list[dict[str, Any]]:
        return [{"head_sha": new_sha, "session_id": "s1", "memory_key": "k", "memory_id": "m"}]

    async def _heads(keys: Any) -> dict[Any, Any]:
        return {
            k: {"status": "active", "memory_id": "auth-k", "content_sha256": new_sha} for k in keys
        }

    class _ClickHouseShowsTheOld:
        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            return [{"head_sha": old_sha, "session_id": "s1", "memory_key": "k", "memory_id": "m"}]

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "unpublished_keepers", _unpublished)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    batch = _input()
    batch.entries = [
        _BulkSetEntry(memory_key="x", memory_type="semantic", value=old),
        _BulkSetEntry(memory_key="y", memory_type="semantic", value=new),
    ]
    got = await bulk_set._bulk_identity_dupes(_ClickHouseShowsTheOld(), batch)
    assert got == {new_sha: ("k", "auth-k")}


@pytest.mark.asyncio
async def test_under_postgres_a_keeper_delivered_between_the_two_reads_is_found(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The undelivered keepers are read before ClickHouse: a keeper whose delivery lands
    in between is owed to ClickHouse at the first read and held by it at the second, so
    one of the two finds it, never neither."""
    from server.memory.app import bulk_set
    from server.memory.store import pg_memory

    sha = sha256_value(VALUE)
    keeper = {"head_sha": sha, "session_id": "s1", "memory_key": "k", "memory_id": "m"}
    delivered = False

    async def _unpublished(*_a: Any, scope: Any) -> list[dict[str, Any]]:
        return [] if delivered else [keeper]

    async def _heads(keys: Any) -> dict[Any, Any]:
        return {k: {"status": "active", "memory_id": "auth-k", "content_sha256": sha} for k in keys}

    class _ClickHouseAfterTheDelivery:
        async def query_all(self, *_a: Any, **_k: Any) -> list[dict[str, Any]]:
            nonlocal delivered
            found = [keeper] if delivered else []
            delivered = True  # the flush lands once the first read has run
            return found

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr(pg_memory, "unpublished_keepers", _unpublished)
    monkeypatch.setattr(pg_memory, "heads_by_keys", _heads)
    got = await bulk_set._bulk_identity_dupes(_ClickHouseAfterTheDelivery(), _input())
    assert got == {sha: ("k", "auth-k")}


class _StubCHWithAheadHead(_StubCHWithLiveKey):
    """The live row holds other bytes, at a version from a replica whose clock is ahead."""

    def __init__(self, version: int) -> None:
        super().__init__("user_clickhouse_task_branch", "mid-live")
        self.version = version

    async def query_all(
        self, sql: str, params: dict[str, Any] | None = None
    ) -> list[dict[str, Any]]:
        rows = await super().query_all(sql, params)
        if "IN {keys:Array(Tuple(" in sql:
            return [r | {"version": self.version, "content_sha256": "c" * 64} for r in rows]
        return rows


@pytest.mark.asyncio
async def test_a_bulk_write_goes_above_a_head_written_ahead_of_this_clock(harness) -> None:
    from server.memory.store.rows import next_version

    ahead = next_version() + 15 * 60 * 10**9
    ch = _StubCHWithAheadHead(ahead)
    harness(ch)
    out = await mem_extract.handle_memory_bulk_set(_input())
    assert [r["version"] for r in _memory_rows(ch)] == [ahead + 2]
    assert out.written[0].version == ahead + 2


@pytest.mark.asyncio
async def test_two_entries_of_a_key_over_a_head_ahead_never_tie(harness) -> None:
    from server.memory.store.rows import next_version

    ahead = next_version() + 15 * 60 * 10**9
    ch = _StubCHWithAheadHead(ahead)
    harness(ch)
    inp = _input()
    inp.entries.append(
        _BulkSetEntry(memory_key=inp.entries[0].memory_key, memory_type="semantic", value="later")
    )
    await mem_extract.handle_memory_bulk_set(inp)
    versions = [r["version"] for r in _memory_rows(ch)]
    assert versions[0] >= ahead + 2 and versions == sorted(set(versions))  # strictly rising


@pytest.mark.asyncio
async def test_under_postgres_the_lookup_holds_a_deleted_head_apart_from_the_live_ones(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One read, deleted heads included: a write over a deleted key goes above it, while
    the skip, pin and id state is built from the live rows alone."""
    from datetime import datetime

    from server.memory.app import bulk_rows, bulk_set
    from server.memory.store import MemoryRow

    asked: dict[str, Any] = {}

    def _head(key: str, **over: Any) -> MemoryRow:
        return MemoryRow(
            memory_id=f"m-{key}",
            org="test",
            namespace="personal",
            agent="cursor",
            user_id="u",
            session_id="s-new",
            memory_key=key,
            memory_type="semantic",
            value="v",
            confidence=1.0,
            version=7,
            pinned=False,
            created_at=datetime(2026, 1, 1),
            expires_at=None,
            **over,
        )

    async def _heads(keys: Any, **kw: Any) -> dict[Any, MemoryRow]:
        asked.update(kw)
        rows = [_head("live"), _head("gone", tombstoned_at=datetime(2026, 1, 2))]
        return {k: r for k in keys for r in rows if r.memory_key == k[5]}

    monkeypatch.setattr("server.core.pg.enabled", lambda: True)
    monkeypatch.setattr("server.memory.store.memory_rows_by_identity_keys", _heads)
    inp = _input("live")
    inp.entries.append(_BulkSetEntry(memory_key="gone", memory_type="semantic", value="back"))
    state = bulk_rows._BulkWriteState()
    await bulk_set._bulk_existing_lookup(object(), inp, state)
    assert asked["include_tombstoned"] is True and state.existing_lookup_ok
    assert set(state.existing_rows) == {"live"} == set(state.existing_ids)
    assert set(state.deleted_heads) == {"gone"}
