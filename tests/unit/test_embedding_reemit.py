"""Every state-of-row change on loom.memory_embeddings is a
versioned re-emit of the live rows — the RMT rule, never a lightweight
UPDATE — and no statement under server/ UPDATEs the vector table."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from server.memory import store as M

ROOT = Path(__file__).resolve().parents[2]


class _CH:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.query_all_calls: list[tuple[str, dict[str, Any]]] = []
        self.insert_calls: list[tuple[str, list[dict[str, Any]], dict[str, Any]]] = []

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return list(self.rows)

    async def insert(self, table, rows, settings=None):
        self.insert_calls.append((table, rows, settings or {}))


_LIVE = {
    "org": "o",
    "namespace": "n",
    "agent": "a",
    "user_id": "u",
    "session_id": "s",
    "memory_key": "k",
    "scope": "private",
    "embedding_model": "m",
    "embedding_dim": 3,
    "embedding": [0.1, 0.2, 0.3],
    "memory_key_embedding": [0.4, 0.5, 0.6],
    "memory_key_embedding_dim": 3,
    "content_excerpt": "text",
    "tags": ["t"],
    "confidence": 0.9,
    "expires_at": datetime(2026, 1, 1),
    "version": 11,
    "reinforcement_count": 4,
    "status": "active",
    "content_excerpt_lc": "text",  # MATERIALIZED: dropped
}


@pytest.mark.asyncio
async def test_reemit_carries_every_persisted_column_and_applies_the_overrides() -> None:
    ch = _CH([dict(_LIVE), {**_LIVE, "embedding_model": "m2"}])
    n = await M.reemit_embedding_rows_for_key(
        "o", "n", "a", "u", "s", "k", ch=ch, expires_at=None, confidence=0.45
    )
    assert n == 2
    sql, params = ch.query_all_calls[0]
    assert "FROM loom.memory_embeddings FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "ns": "n", "a": "a", "u": "u", "s": "s", "k": "k"}
    ((table, rows, settings),) = ch.insert_calls
    assert table == "loom.memory_embeddings"
    assert settings == {"async_insert": 0, "wait_for_async_insert": 1}
    for row in rows:
        assert set(row) >= set(M.EMBEDDING_PERSISTED_COLS)
        assert "content_excerpt_lc" not in row  # MATERIALIZED columns are the server's
        assert row["embedding"] == [0.1, 0.2, 0.3] and row["memory_key_embedding"] == [
            0.4,
            0.5,
            0.6,
        ]
        assert row["expires_at"] is None and row["confidence"] == 0.45
        assert row["reinforcement_count"] == 4 and row["tags"] == ["t"]
        assert row["version"] == 12  # one above the row it read
    assert {r["embedding_model"] for r in rows} == {"m", "m2"}


@pytest.mark.asyncio
async def test_reemit_with_no_live_row_inserts_nothing() -> None:
    ch = _CH([])
    assert await M.reemit_embedding_rows_for_key("o", "n", "a", "u", "s", "k", ch=ch, tags=[]) == 0
    assert ch.insert_calls == []


@pytest.mark.asyncio
async def test_tombstone_is_a_reemit_with_tombstoned_at() -> None:
    ch = _CH([dict(_LIVE)])
    ts = datetime(2026, 9, 14, 23, 0, 0)
    assert await M.tombstone_embeddings_for_key("o", "n", "a", "u", "s", "k", ts=ts, ch=ch) == 1
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["tombstoned_at"] == ts and rows[0]["embedding"] == [0.1, 0.2, 0.3]


@pytest.mark.asyncio
async def test_shared_reemit_reads_the_scope_shared_rows_of_the_key() -> None:
    ch = _CH([{**_LIVE, "agent": "_shared", "scope": "shared"}])
    ts = datetime(2026, 9, 14)
    assert await M.reemit_shared_embedding_rows("o", "n", "k", ch=ch, tombstoned_at=ts) == 1
    sql, params = ch.query_all_calls[0]
    assert "scope = 'shared'" in sql and "FINAL" in sql and "tombstoned_at IS NULL" in sql
    assert params == {"org": "o", "ns": "n", "k": "k"}
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["scope"] == "shared" and rows[0]["tombstoned_at"] == ts


class _VectorsCH(_CH):
    """Answers the memory_embeddings read with `rows` and the entity_vectors read
    with `entity_rows`."""

    def __init__(self, rows: list[dict[str, Any]], entity_rows: list[dict[str, Any]]) -> None:
        super().__init__(rows)
        self.entity_rows = entity_rows

    async def query_all(self, sql, params=None):
        self.query_all_calls.append((sql, params or {}))
        return list(self.entity_rows if "loom.entity_vectors" in sql else self.rows)


@pytest.mark.asyncio
async def test_retiring_a_key_tombstones_at_the_new_rows_version_and_its_entity_vectors() -> None:
    """The tombstone copy takes the unembedded row's version, not a fresh one: a
    concurrent embedded write's newer vector outranks it. The key's entity vectors
    go too, and only this identity's (the table has no session column)."""
    ent = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "memory_key": "k"}
    other = {**ent, "agent": "someone-else"}
    ch = _VectorsCH([dict(_LIVE)], [dict(ent), dict(other)])
    previous = SimpleNamespace(
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="s",
        memory_key="k",
        memory_type="semantic",
        sensitivity="",
    )
    assert await M.tombstone_vectors_of(previous, version=42, ch=ch) == 1
    (emb_table, emb_rows, _), (ent_table, ent_rows, _) = ch.insert_calls
    assert emb_table == "loom.memory_embeddings"
    assert emb_rows[0]["version"] == 42 and emb_rows[0]["tombstoned_at"] is not None
    assert ent_table == "loom.entity_vectors"
    assert [r["agent"] for r in ent_rows] == ["a"] and ent_rows[0]["tombstoned_at"] is not None


@pytest.mark.asyncio
async def test_a_never_embedded_previous_version_reads_nothing() -> None:
    ch = _VectorsCH([dict(_LIVE)], [])
    previous = SimpleNamespace(memory_type="semantic", sensitivity="high")
    assert await M.tombstone_vectors_of(previous, version=42, ch=ch) == 0
    assert ch.query_all_calls == [] and ch.insert_calls == []


_UPDATE_RE = re.compile(
    r"(?:^|\W)(?:UPDATE\s+(?:loom\.)?memory_embeddings\b|"
    r"ALTER\s+TABLE\s+(?:loom\.)?memory_embeddings\s+UPDATE\b)",
    re.IGNORECASE,
)


def test_nothing_under_server_updates_the_vector_table() -> None:
    """One lightweight UPDATE on
    loom.memory_embeddings leaves a patch part that switches the fact leg's
    codes-first read off for every search."""
    offenders = []
    for path in sorted((ROOT / "server").rglob("*.py")):
        text = path.read_text(encoding="utf-8")
        if _UPDATE_RE.search(text):
            offenders.append(str(path.relative_to(ROOT)))
    assert offenders == [], offenders


@pytest.mark.asyncio
async def test_retiring_leaves_a_newer_writes_vectors_alone() -> None:
    """Rows a newer write landed after the retiring version was drawn (an embedding
    at or above it, an entity vector created in a later second) are not re-emitted."""
    v = 1_800_000_000 * 10**9 + 5  # drawn at second 1_800_000_000
    second = datetime.fromtimestamp(1_800_000_000, UTC).replace(tzinfo=None)
    newer_emb = {**_LIVE, "embedding_model": "m2", "version": v + 1}
    ent = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "memory_key": "k"}
    old_ent = {**ent, "entity_text": "old", "created_at": second}
    new_ent = {**ent, "entity_text": "new", "created_at": second + timedelta(seconds=1)}
    ch = _VectorsCH([dict(_LIVE), newer_emb], [old_ent, new_ent])
    assert await M.retire_vectors_for_keys([(("o", "n", "a", "u", "s", "k"), v)], ch=ch) == 1
    (_, emb_rows, _), (_, ent_rows, _) = ch.insert_calls
    assert [r["embedding_model"] for r in emb_rows] == ["m"]
    assert [r["entity_text"] for r in ent_rows] == ["old"]


class _RMTCH:
    """A memory_embeddings / entity_vectors store that answers reads as FINAL does
    (per sort key the highest version, or the later insert on a tie)."""

    def __init__(self) -> None:
        self.emb: list[dict[str, Any]] = []
        self.ent: list[dict[str, Any]] = []

    @staticmethod
    def _final(rows: list[dict[str, Any]], key: Any, ver: Any) -> list[dict[str, Any]]:
        best: dict[Any, dict[str, Any]] = {}
        for r in rows:
            if key(r) not in best or ver(r) >= ver(best[key(r)]):
                best[key(r)] = r
        return [r for r in best.values() if r.get("tombstoned_at") is None]

    def live_emb(self) -> list[dict[str, Any]]:
        return self._final(
            self.emb,
            lambda r: (r["agent"], r["embedding_model"], r["memory_key"]),
            lambda r: r["version"],
        )

    def live_ent(self) -> list[dict[str, Any]]:
        return self._final(
            self.ent, lambda r: (r["entity_text"], r["memory_key"]), lambda r: r["created_at"]
        )

    async def query_all(self, sql, params=None):
        return [dict(r) for r in (self.live_ent() if "entity_vectors" in sql else self.live_emb())]

    async def insert(self, table, rows, settings=None):
        (self.ent if table == "loom.entity_vectors" else self.emb).extend(dict(r) for r in rows)


@pytest.mark.asyncio
async def test_an_older_high_set_paused_after_its_insert_does_not_retire_a_newer_vector(
    monkeypatch,
) -> None:
    """Writer A re-sets the key as 'high' and pauses after its memory insert; writer B
    lands a newer embedded version (its vector and an entity vector a second later);
    A then retires. B's vectors stay live, the old generation's are retired."""
    from server.memory.api.models_crud import MemorySetInput
    from server.memory.app import set_commit

    ch = _RMTCH()
    v_old = M.next_version()
    ent = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "memory_key": "k"}
    a_second = datetime.now(UTC).replace(tzinfo=None, microsecond=0)
    ch.emb.append({**_LIVE, "version": v_old})
    ch.ent.append({**ent, "entity_text": "old", "created_at": a_second - timedelta(seconds=5)})
    v_a = M.next_version()

    async def insert_then_writer_b_lands(rows, **_kw):
        assert rows[0]["version"] == v_a
        await asyncio.sleep(0)  # A pauses here; B's whole write lands
        ch.emb.append({**_LIVE, "content_excerpt": "B", "version": M.next_version()})
        ch.ent.append({**ent, "entity_text": "new", "created_at": a_second + timedelta(seconds=1)})
        return 1

    async def no_projection(*_a, **_k):
        return None

    monkeypatch.setattr(set_commit, "insert_memory_versions", insert_then_writer_b_lands)
    monkeypatch.setattr(set_commit, "_project_set_entities_fail_open", no_projection)
    existing = SimpleNamespace(
        **{
            k: _LIVE[k]
            for k in ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
        },
        memory_type="semantic",
        sensitivity="",
    )
    w = set_commit._SetWrite(
        input=MemorySetInput(
            org="o",
            namespace="n",
            agent="a",
            user_id="u",
            session_id="s",
            memory_key="k",
            value="secret",
        ),
        version=v_a,
        retyped_from="",
        existing=existing,
        expires_at=None,
        memory_row={"version": v_a},
    )
    await set_commit._set_plain_insert(w, ch)
    assert [r["content_excerpt"] for r in ch.live_emb()] == ["B"]
    assert [r["entity_text"] for r in ch.live_ent()] == ["new"]


_KEY = ("o", "n", "a", "u", "s", "k")


class _RetireAfterReadCH(_RMTCH):
    """FINAL fake where a retire (a tombstone at a newer memory version) commits
    right after the first read: the window a delayed metadata re-emit sits in."""

    def __init__(self) -> None:
        super().__init__()
        self.retired = False

    async def query_all(self, sql, params=None):
        rows = await super().query_all(sql, params)
        if not self.retired:
            self.retired = True
            self.emb.append({**_LIVE, "version": M.next_version(), "tombstoned_at": datetime.now()})
        return rows


async def _touch(ch):
    await M.reemit_embedding_rows_for_key(*_KEY, ch=ch, expires_at=None)


async def _verify(ch):
    await M.reemit_embedding_rows_for_key(*_KEY, ch=ch, verified=1)


async def _touch_many(ch):
    await M.reemit_embedding_rows_for_keys([(_KEY, {"expires_at": None})], ch=ch)


async def _status_mirror(ch):
    await M.mirror_status_to_embeddings([{**_LIVE, "status": "superseded"}], ch=ch)


async def _unsupersede(ch):
    await M.unsupersede_embeddings(*_KEY, ch=ch)


async def _restamp(ch):
    await M.restamp_embedding_rows(
        await ch.query_all("memory_embeddings"), status="duplicate", ch=ch
    )


async def _repair_dim(ch):
    from server.memory.store.integrity import repair_dim0_rows

    await repair_dim0_rows(await ch.query_all("memory_embeddings"), ch=ch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reemit", [_touch, _verify, _touch_many, _status_mirror, _unsupersede, _restamp, _repair_dim]
)
async def test_a_reemit_read_before_a_retire_does_not_bring_the_vector_back(reemit) -> None:
    ch = _RetireAfterReadCH()
    ch.emb.append({**_LIVE, "version": M.next_version()})
    await reemit(ch)
    assert ch.retired and ch.live_emb() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reemit", [_touch, _status_mirror, _unsupersede, _restamp])
async def test_a_reemit_still_replaces_the_row_it_read(reemit) -> None:
    ch = _RMTCH()
    ch.emb.append({**_LIVE, "status": "superseded" if reemit is _unsupersede else "active"})
    await reemit(ch)
    (live,) = ch.live_emb()
    assert live["version"] == _LIVE["version"] + 1
    assert live["status"] == {_status_mirror: "superseded", _restamp: "duplicate"}.get(
        reemit, "active"
    )


@pytest.mark.asyncio
async def test_an_explicit_version_override_is_kept() -> None:
    ch = _CH([dict(_LIVE)])
    await M.reemit_embedding_rows_for_key(*_KEY, ch=ch, tombstoned_at=datetime.now(), version=99)
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["version"] == 99
