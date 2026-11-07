"""A lock binds the bulk write path as it binds memory.set.

memory.set answers 423 LOCKED to a value change on a read_only key. A bulk
batch (memory.bulk_set, set_from_messages) skips just that entry and names it
in `skipped_locked`, and a permitted write (a no_delete key) carries the lock
forward on its version row instead of writing it away. The attestation binds
the old content, so a value change drops it (`build_revision_row`).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from server.memory.api.models_bulk import MemoryBulkSetInput, _BulkSetEntry
from server.memory.app import bulk_set as _bulk_set_mod
from server.memory.app import session_summary as _session_summary_mod
from server.memory.app.extract import handler as mem_extract
from tests.unit.memory_rows import projection_row


@pytest.fixture(autouse=True)
def _no_delete_in_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    """No delete races these writes: the fence before the insert finds no later version."""

    async def _none(*_a: Any, **_k: Any) -> dict[Any, int]:
        return {}

    monkeypatch.setattr("server.memory.store.versions_above", _none)


_NOW = datetime.now(UTC).replace(tzinfo=None)
_MID = "11111111-1111-1111-1111-111111111111"


def _live(**cols: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "memory_key": "user_deploy_day",
        "memory_id": _MID,
        "content_sha256": "a" * 64,
        "pinned": 0,
        "created_at": _NOW - timedelta(days=9),
        "lock_mode": "",
        "lock_reason": "",
        "lock_until": None,
        "locked_by": "",
        "locked_at": None,
        "verified_by": "",
        "verified_at": None,
        "verified_sha256": "",
        "on_expire": "",
        "expire_reason": "",
    }
    base.update(cols)
    return base


class _StubCH:
    def __init__(self, live: list[dict[str, Any]]) -> None:
        self.live = live
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []

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
                    session_id="s",
                    **r,
                )
                for r in self.live
            ]
        return []

    async def insert(self, table: str, rows: list[dict[str, Any]], **_: Any) -> None:
        self.inserts.append((table, rows))


class _StubEmbedder:
    model_name = "fake-test"
    dim = 3

    async def embed_many(self, texts: list[str]) -> list[list[float]]:
        return [[0.1, 0.2, 0.3] for _ in texts]


@pytest.fixture
def wire(monkeypatch: pytest.MonkeyPatch):
    def make(ch: _StubCH) -> None:
        monkeypatch.setattr(_bulk_set_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_session_summary_mod, "get_client", lambda: ch)
        monkeypatch.setattr(_bulk_set_mod, "get_embedder", lambda: _StubEmbedder())
        monkeypatch.setattr(_session_summary_mod, "get_embedder", lambda: _StubEmbedder())

        async def _none(*_: Any, **__: Any) -> Any:
            return None

        async def _empty(*_: Any, **__: Any) -> dict[str, str]:
            return {}

        monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _none)
        monkeypatch.setattr("server.memory.app.blacklist_guard.note_hit", _none)
        monkeypatch.setattr(
            "server.memory.app.entity_resolution.resolve_entities_for_write", _empty
        )
        monkeypatch.setattr("server.memory.store.insert_embedding_rows", _none)
        monkeypatch.setattr("server.memory.store.usage.record_usage_event", _none)
        for name in ("write_entity_vectors", "write_entity_edges", "write_entity_triples"):
            monkeypatch.setattr(_bulk_set_mod, name, _none)

    return make


def _input(*keys: str) -> MemoryBulkSetInput:
    return MemoryBulkSetInput(
        org="test",
        namespace="personal",
        agent="cursor",
        user_id="u",
        session_id="s",
        entries=[
            _BulkSetEntry(memory_key=k, memory_type="semantic", value=f"{k} is Thursday")
            for k in keys
        ],
    )


def _memory_rows(ch: _StubCH) -> list[dict[str, Any]]:
    return [r for table, rows in ch.inserts if table == "loom.memory" for r in rows]


@pytest.mark.asyncio
async def test_value_change_to_a_read_only_key_is_skipped(wire) -> None:
    ch = _StubCH([_live(lock_mode="read_only", lock_reason="audited", locked_by="human:alice")])
    wire(ch)
    out = await mem_extract.handle_memory_bulk_set(_input("user_deploy_day", "user_new_fact"))
    assert out.skipped_locked == ["user_deploy_day"]
    assert out.skipped_pinned == []
    rows = _memory_rows(ch)
    assert [r["memory_key"] for r in rows] == ["user_new_fact"]
    # The written new key lands the lock columns at their defaults — every row carries them.
    assert rows[0]["lock_mode"] == "" and rows[0]["lock_until"] is None
    assert {r.memory_key: r.version for r in out.written}["user_deploy_day"] == 0


@pytest.mark.asyncio
async def test_a_lapsed_read_only_lock_does_not_skip(wire) -> None:
    ch = _StubCH([_live(lock_mode="read_only", lock_until=_NOW - timedelta(hours=1))])
    wire(ch)
    out = await mem_extract.handle_memory_bulk_set(_input("user_deploy_day"))
    assert out.skipped_locked == []
    assert [r["memory_key"] for r in _memory_rows(ch)] == ["user_deploy_day"]


@pytest.mark.asyncio
async def test_a_permitted_write_carries_the_lock_and_attestation(wire) -> None:
    until = _NOW + timedelta(days=1)
    locked_at = _NOW - timedelta(days=1)
    verified_at = _NOW - timedelta(days=2)
    ch = _StubCH(
        [
            _live(
                lock_mode="no_delete",
                lock_reason="keep",
                lock_until=until,
                locked_by="human:alice",
                locked_at=locked_at,
                verified_by="human:bob",
                verified_at=verified_at,
                verified_sha256="a" * 64,
                on_expire="archive",
                expire_reason="quarterly",
            )
        ]
    )
    wire(ch)
    out = await mem_extract.handle_memory_bulk_set(_input("user_deploy_day"))
    assert out.skipped_locked == []
    (row,) = _memory_rows(ch)
    assert row["memory_id"] == _MID
    assert (row["lock_mode"], row["lock_reason"], row["lock_until"]) == ("no_delete", "keep", until)
    assert (row["locked_by"], row["locked_at"]) == ("human:alice", locked_at)
    assert (row["verified_by"], row["verified_at"]) == ("human:bob", verified_at)
    # The attestation is over the old bytes, so the new version reads it as stale.
    assert row["verified_sha256"] == "a" * 64 != row["content_sha256"]
    assert (row["on_expire"], row["expire_reason"]) == ("archive", "quarterly")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("live", "verdict"), [({"pinned": 1}, "pinned"), ({"lock_mode": "read_only"}, "locked")]
)
async def test_a_refused_entry_carries_its_verdict_and_a_restatement_does_not(
    wire, live: dict[str, Any], verdict: str
) -> None:
    # One batch restates the live value, then changes it: only the change is
    # refused, so only its own result says so (the key's list names it either way).
    from server.core.hashing import sha256_value

    same = sha256_value("user_deploy_day is Thursday")
    ch = _StubCH([_live(content_sha256=same, **live)])
    wire(ch)
    entries = [
        _BulkSetEntry(memory_key="user_deploy_day", memory_type="semantic", value=v)
        for v in ("user_deploy_day is Thursday", "user_deploy_day is Friday")
    ]
    inp = _input("user_deploy_day").model_copy(update={"entries": entries})
    out = await mem_extract.handle_memory_bulk_set(inp)
    assert [(r.version, r.conflict_verdict) for r in out.written] == [(0, ""), (0, verdict)]


@pytest.mark.asyncio
async def test_an_entry_the_blacklist_could_not_check_says_so(
    wire, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An entry refused because the request's values are too long for the blacklist's
    regexes is told apart from one a pattern matched: sent in a smaller request it is
    checked."""
    from server.memory.app import blacklist_guard as bl

    wire(_StubCH([]))

    async def _find(*, org: str, namespace: str, value: str) -> Any:
        if "Friday" in value:
            return bl._over_budget(org, namespace, "namespace", len(value), 300, alone=False)
        if "Monday" in value:
            return bl.BlacklistPattern("p1", org, namespace, "Monday", "substring", "namespace", "")
        return None

    monkeypatch.setattr("server.memory.app.blacklist_guard.find_match", _find)
    entries = [
        _BulkSetEntry(memory_key=k, memory_type="semantic", value=f"{k} is {day}")
        for k, day in (("a", "Friday"), ("b", "Monday"), ("c", "Thursday"))
    ]
    inp = _input("a").model_copy(update={"entries": entries})
    out = await mem_extract.handle_memory_bulk_set(inp)
    verdicts = {r.memory_key: r.conflict_verdict for r in out.written}
    assert verdicts["a"] == "blacklist_unchecked" and verdicts["b"] == "blacklisted"
