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
