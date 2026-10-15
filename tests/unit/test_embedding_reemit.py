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
    n = await M.tombstone_embeddings_for_key("o", "n", "a", "u", "s", "k", ts=ts, version=50, ch=ch)
    assert n == 1
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["tombstoned_at"] == ts and rows[0]["embedding"] == [0.1, 0.2, 0.3]
    assert rows[0]["version"] == 50  # the retiring memory row's, not the read row's + 1


@pytest.mark.asyncio
async def test_a_tombstone_keeps_a_row_newer_than_the_retiring_version() -> None:
    ch = _CH([dict(_LIVE)])  # version 11
    ts = datetime(2026, 9, 14)
    assert await M.tombstone_embeddings_for_key(*_KEY, ts=ts, version=11, ch=ch) == 0
    assert ch.insert_calls == []


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
    memory_row = {**_LIVE, "status": "superseded", "version": M.next_version()}
    await M.mirror_status_to_embeddings([memory_row], ch=ch)


async def _unsupersede(ch):
    await M.unsupersede_embeddings(*_KEY, ch=ch)


async def _restamp(ch):
    version = M.next_version()  # a restamp's version is drawn before its read
    rows = await ch.query_all("memory_embeddings")
    await M.restamp_embedding_rows(rows, status="duplicate", version=version, ch=ch)


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


class _DeleteAfterReadCH(_RMTCH):
    """FINAL fake where memory.delete's mirror (`tombstone_embeddings_for_key` at the
    delete's memory version) runs right after a metadata re-emit's first read."""

    def __init__(self) -> None:
        super().__init__()
        self.retired = False

    async def query_all(self, sql, params=None):
        rows = await super().query_all(sql, params)
        if not self.retired:
            self.retired = True
            await M.tombstone_embeddings_for_key(
                *_KEY, ts=datetime.now(), version=M.next_version(), ch=self
            )
        return rows


@pytest.mark.asyncio
@pytest.mark.parametrize("reemit", [_touch, _verify, _touch_many, _status_mirror, _unsupersede])
async def test_a_reemit_read_before_a_delete_mirror_does_not_bring_the_vector_back(reemit) -> None:
    ch = _DeleteAfterReadCH()
    ch.emb.append({**_LIVE, "version": M.next_version()})
    await reemit(ch)
    assert ch.retired and ch.live_emb() == []


@pytest.mark.asyncio
@pytest.mark.parametrize("reemit", [_touch, _unsupersede])
async def test_a_metadata_copy_still_replaces_the_row_it_read(reemit) -> None:
    ch = _RMTCH()
    ch.emb.append({**_LIVE, "status": "superseded" if reemit is _unsupersede else "active"})
    await reemit(ch)
    (live,) = ch.live_emb()
    assert live["version"] == _LIVE["version"] + 1
    assert live["status"] == "active"


@pytest.mark.asyncio
async def test_a_status_retire_writes_the_memory_rows_version_and_keeps_newer_rows() -> None:
    ch = _RMTCH()
    newer = {**_LIVE, "agent": "b", "version": 50}
    ch.emb += [dict(_LIVE), newer]
    rows = [
        {**_LIVE, "status": "superseded", "version": 30},
        {**newer, "status": "superseded", "version": 30},
    ]
    await M.mirror_status_to_embeddings(rows, ch=ch)
    by_agent = {r["agent"]: r for r in ch.live_emb()}
    assert (by_agent["a"]["status"], by_agent["a"]["version"]) == ("superseded", 30)
    assert by_agent["b"] == newer


@pytest.mark.asyncio
async def test_an_explicit_version_override_is_kept() -> None:
    ch = _CH([dict(_LIVE)])
    await M.reemit_embedding_rows_for_key(*_KEY, ch=ch, tombstoned_at=datetime.now(), version=99)
    ((_, rows, _),) = ch.insert_calls
    assert rows[0]["version"] == 99


# ---- every retire site against a paused metadata copy ---------------------------------
# A touch (or any metadata copy) reads the live vector row, pauses, and writes read + 1
# after the retire. Each retire must be stamped above that, so the copy loses under FINAL.


class _SiteCH(_RMTCH):
    """FINAL fake that also answers the status-divergence scan and leaves every
    other loom.memory read empty."""

    async def query_all(self, sql, params=None):
        if "embeddings_status" in sql:
            return [{**_LIVE, "status": "superseded", "invalid_at": None}]
        if "entity_vectors" in sql or "memory_embeddings" in sql:
            return await super().query_all(sql, params)
        return []

    async def insert(self, table, rows, settings=None):
        if table in ("loom.memory_embeddings", "loom.entity_vectors"):
            await super().insert(table, rows, settings)


def _memory_row(**over: Any) -> Any:
    base = {
        "memory_id": "mid",
        **{c: _LIVE[c] for c in ("org", "namespace", "agent", "user_id", "session_id")},
        "memory_key": "k",
        "memory_type": "episodic",  # not searchable: no chunk side paths
        "value": "v",
        "confidence": 0.9,
        "version": 1,
        "tags": [],
        "entities": [],
        "pinned": False,
        "created_at": datetime(2026, 1, 1),
        "expires_at": None,
    }
    return M.MemoryRow(**(base | over))


def _patch_memory_io(mp: pytest.MonkeyPatch, ch: Any, row: Any) -> None:
    async def get_by_id(*_a, **_k):
        return row

    async def no_insert(rows, *_a, alongside=(), **_k):
        for f in alongside:  # the funnel hands the rows it kept on
            await f(rows)
        return len(rows)

    mp.setattr("server.memory.store.lifecycle.get_by_id", get_by_id)
    mp.setattr("server.memory.store.lifecycle.insert_memory_versions", no_insert)
    mp.setattr("server.memory.store.insert_memory_versions", no_insert)
    mp.setattr("server.memory.store.embeddings._default_client", lambda: ch)
    mp.setattr("server.memory.store.lifecycle._default_client", lambda: ch)
    mp.setattr("server.memory.store.shared._default_client", lambda: ch)
    mp.setattr("server.core.ch.get_client", lambda: ch)


async def _site_delete(ch, mp):
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app import handlers

    async def get_by_key(*_a, **_k):
        return _memory_row()

    mp.setattr(handlers, "get_client", lambda: ch)
    mp.setattr("server.memory.store.get_by_key", get_by_key)
    await handlers.handle_memory_delete(
        MemoryDeleteInput(
            org="o", namespace="n", agent="a", user_id="u", session_id="s", memory_key="k"
        )
    )


async def _expiry(ch, mp, on_expire):
    from server.memory.app import expiry

    async def find(**_k):
        return [_memory_row(on_expire=on_expire, expires_at=datetime(2026, 1, 1))]

    mp.setattr("server.memory.store.find_expired_candidates", find)
    await expiry.run_pass()


async def _site_expiry_tombstone(ch, mp):
    await _expiry(ch, mp, "tombstone")


async def _site_expiry_anonymize(ch, mp):
    await _expiry(ch, mp, "anonymize")


async def _site_orphan_reconcile(ch, mp):
    from server.memory.workers import orphan_vector_reconcile

    await orphan_vector_reconcile.run_pass(ch=ch)


async def _site_sensitivity_high(ch, mp):
    from server.memory.app import sensitivity_sweep as sweep

    async def relabelled(*_a, **_k):
        return 1

    mp.setattr("server.memory.store.write_versions_by_ids", relabelled)
    await sweep.apply([sweep.SweepCandidate("mid", *_KEY[:5], "k", "high", ("credential",))], ch=ch)


async def _site_cascade(ch, mp):
    await M.tombstone_cascade_by_ids("o", ["mid"], when=datetime.now(), ch=ch)


async def _site_supersede(ch, mp):
    victim = {
        "memory_id": "mid",
        "superseded_by": "x",
        **dict(
            zip(
                ("org", "namespace", "agent", "user_id", "session_id", "memory_key"),
                _KEY,
                strict=True,
            )
        ),
    }
    await M.supersede_duplicates("o", [victim], ch=ch)


async def _site_consolidated_dependent(ch, mp):
    from server.memory.store import lifecycle

    async def deps(*_a, **_k):
        return [
            {
                "mid": "mid",
                **{c: _LIVE[c] for c in ("namespace", "agent", "user_id", "session_id")},
                "memory_key": "k",
                "tags": ["consolidated", "src:gone"],
            }
        ]

    async def none_live(*_a, **_k):
        return 0

    mp.setattr(lifecycle, "consolidated_dependents_of", deps)
    mp.setattr(lifecycle, "live_source_count", none_live)
    await M.downgrade_consolidated_dependents("o", "n", "gone", ch=ch)


async def _site_consolidate_merge(ch, mp):
    from server.memory.api.models_admin import MemoryConsolidateInput
    from server.memory.app import consolidate

    async def written(*_a, **_k):
        return 1

    mp.setattr("server.memory.store.write_versions_by_ids", written)
    await consolidate._retire_merged(
        ch,
        MemoryConsolidateInput(org="o", namespace="n", agent="a", user_id="u", session_id="s"),
        [{"memory_id": "mid", "memory_key": "k", "session_id": "s"}],
        canonical_key="canon",
        now_naive=datetime.now(),
    )


async def _site_status_reject(ch, mp):
    row = M.build_memory_version_row(_memory_row(), version=M.next_version(), status="rejected")
    await M.mirror_status_to_embeddings([row], ch=ch)


async def _site_reconcile_status(ch, mp):
    await M.reconcile_embedding_status("o", "n", dry_run=False, ch=ch)


async def _site_demote(ch, mp):
    from server.memory.api.models_lifecycle import MemoryDemoteInput
    from server.memory.app import promote
    from server.memory.store import shared

    async def fetch(*_a, **_k):
        return {
            "tombstoned_at": None,
            "reversible_until": None,
            "value": "v",
            "memory_type": "semantic",
            "confidence": 1.0,
            "promoted_from": [],
            "promotion_event": "",
            "promoted_from_candidate_id": "",
            "promoted_by": "",
            "rationale": "",
            "promoted_at": datetime.now(),
        }

    async def no_insert(*_a, **_k):
        return None

    mp.setattr(shared, "fetch_shared", fetch)
    mp.setattr(shared, "insert_shared_row", no_insert)
    await promote.handle_memory_demote(MemoryDemoteInput(org="o", namespace="n", memory_key="k"))


async def _site_move(ch, mp):
    from server.memory.app import move

    await move._redirect_source(_memory_row(), "dest", ch)


async def _site_dedup_collapse(ch, mp):
    from server.memory.api.models_graph import MemoryDedupCollapseInput
    from server.memory.app import dedup

    pair = SimpleNamespace(agent="a", user_id="u", session_id="s", memory_key="k", duplicate_of="c")

    async def pairs(*_a, **_k):
        return [pair]

    async def canon(*_a, **_k):
        return [SimpleNamespace(canonical_id="c", memory_key="kc")]

    mp.setattr("server.memory.store.find_duplicate_pairs", pairs)
    mp.setattr("server.memory.store.canonical_rows_by_ids", canon)
    await dedup.handle_memory_dedup_collapse(
        MemoryDedupCollapseInput(org="o", namespace="n", dry_run=False)
    )


async def _site_retire_vectors(ch, mp):
    # bulk_set, namespace.rollback, memory.set and update_by_id retire through this
    await M.retire_vectors_for_keys([(_KEY, M.next_version())], ch=ch)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "site",
    [
        _site_delete,
        _site_expiry_tombstone,
        _site_expiry_anonymize,
        _site_orphan_reconcile,
        _site_sensitivity_high,
        _site_cascade,
        _site_supersede,
        _site_consolidated_dependent,
        _site_consolidate_merge,
        _site_status_reject,
        _site_reconcile_status,
        _site_demote,
        _site_move,
        _site_dedup_collapse,
        _site_retire_vectors,
    ],
)
async def test_every_retire_outranks_a_paused_metadata_copy(site, monkeypatch) -> None:
    ch = _SiteCH()
    read = {**_LIVE, "created_at": datetime(2026, 1, 1), "version": M.next_version()}
    ch.emb.append(read)
    _patch_memory_io(monkeypatch, ch, _memory_row())
    await site(ch, monkeypatch)
    assert len(ch.emb) > 1, "the site wrote no retire"
    ch.emb.append({**read, "version": M.reemit_version(read)})  # the paused touch lands
    assert [r for r in ch.live_emb() if r["status"] in ("active", "conflicted")] == []


@pytest.mark.asyncio
async def test_a_vector_written_during_the_deletes_read_is_retired(monkeypatch) -> None:
    """memory.delete draws its version after its strong read, so a stub fill that
    lands during the read is below it and retired with the key."""
    from server.memory.api.models_crud import MemoryDeleteInput
    from server.memory.app import handlers

    ch = _SiteCH()
    _patch_memory_io(monkeypatch, ch, _memory_row())

    async def get_by_key(*_a, **_k):
        ch.emb.append({**_LIVE, "version": M.next_version()})  # lands mid-read
        return _memory_row()

    monkeypatch.setattr(handlers, "get_client", lambda: ch)
    monkeypatch.setattr("server.memory.store.get_by_key", get_by_key)
    await handlers.handle_memory_delete(
        MemoryDeleteInput(
            org="o", namespace="n", agent="a", user_id="u", session_id="s", memory_key="k"
        )
    )
    assert ch.live_emb() == []


@pytest.mark.copy_head_check
@pytest.mark.asyncio
async def test_a_copy_under_a_high_or_deleted_head_is_retired_above_itself(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A copy that read its head before a 'high' write landed re-reads the head after
    its insert and tombstones each copied row one above itself; a deleted head counts
    on a sequential read. Copies under a plain or profile head, tombstones, dim-0 rows
    and shared-scope rows are left alone."""
    inserted: list[dict[str, Any]] = []

    async def _heads(ch: Any, keys: Any, sequential: bool) -> dict[Any, str]:
        return {k: ("high" if k[5] == "hi" else "") for k in keys if k[5] != "gone"}

    async def _insert(rows: Any, **_k: Any) -> None:
        inserted.extend(rows)

    monkeypatch.setattr(M.embeddings, "_head_sensitivity", _heads)
    monkeypatch.setattr(M.embeddings, "insert_embedding_rows", _insert)
    monkeypatch.setattr("server.memory.store.sql.read_your_writes_enabled", lambda: True)
    base = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    live = {"embedding_dim": 3, "scope": "private"}
    rows = [
        {**base, **live, "memory_key": "hi", "version": 10},
        {**base, **live, "memory_key": "hi", "version": 12},
        {**base, **live, "memory_key": "plain", "version": 10},
        {**base, **live, "memory_key": "gone", "version": 7},
        {**base, "memory_key": "stub", "version": 7, "embedding_dim": 0, "scope": "private"},
        {**base, "memory_key": "sh", "version": 7, "embedding_dim": 3, "scope": "shared"},
        {**base, **live, "memory_key": "tomb", "version": 7, "tombstoned_at": datetime(2026, 1, 1)},
    ]
    assert await M.retire_copies_under_unembedded_heads(rows, ch=object()) == 3
    assert sorted((r["memory_key"], r["version"]) for r in inserted) == [
        ("gone", 9),
        ("hi", 12),
        ("hi", 14),
    ]
    assert all(r["tombstoned_at"] is not None for r in inserted)


@pytest.mark.copy_head_check
@pytest.mark.asyncio
async def test_a_failed_head_recheck_is_logged_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom(*_a: Any, **_k: Any) -> dict[Any, str]:
        raise RuntimeError("replica down")

    monkeypatch.setattr(M.embeddings, "_head_sensitivity", _boom)
    row = {"org": "o", "namespace": "n", "agent": "a", "user_id": "u", "session_id": "s"}
    row |= {"memory_key": "k", "version": 1, "embedding_dim": 3, "scope": "private"}
    assert await M.retire_copies_under_unembedded_heads([row], ch=object()) == 0


@pytest.mark.parametrize("site", ["restamp", "dim0_repair", "reindex_same_value"])
@pytest.mark.asyncio
async def test_every_vector_copy_site_rechecks_its_head(site: str, monkeypatch) -> None:
    """Each site that copies live vector rows runs the post-copy head re-check on what
    it inserted; dropping the call from any of them fails here."""
    from types import SimpleNamespace

    from server.memory.store import embeddings, integrity

    checked: list[list[dict[str, Any]]] = []

    async def _check(rows: Any, **_k: Any) -> int:
        checked.append(list(rows))
        return 0

    async def _insert(rows: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(embeddings, "retire_copies_under_unembedded_heads", _check)
    monkeypatch.setattr(embeddings, "insert_embedding_rows", _insert)
    monkeypatch.setattr(M, "retire_copies_under_unembedded_heads", _check)
    monkeypatch.setattr(M, "insert_embedding_rows", _insert)
    row = {**_LIVE, "version": 5}
    if site == "restamp":
        await M.restamp_embedding_rows([row], status="duplicate", version=9, ch=object())
    elif site == "dim0_repair":
        await integrity.repair_dim0_rows(
            [{**row, "embedding_dim": 0, "embedding": [0.1, 0.2, 0.3]}], ch=object()
        )
    else:
        from server.memory.app import ops

        async def _heads(keys: Any, **_k: Any) -> dict[Any, Any]:
            return {
                k: SimpleNamespace(memory_type="semantic", sensitivity="", version=8) for k in keys
            }

        async def _mirror(keys: Any, **_k: Any) -> list[dict[str, Any]]:
            ident = ("org", "namespace", "agent", "user_id", "session_id", "memory_key")
            return [
                {
                    **{c: row[c] for c in ident},
                    "content_excerpt": "same",
                    "memory_type": "semantic",
                    "created_at": datetime(2026, 1, 1),
                    "version": 8,
                }
            ]

        monkeypatch.setattr(M, "memory_rows_by_identity_keys", _heads)
        monkeypatch.setattr(M, "memory_mirror_rows_for_keys", _mirror)
        read = {**row, "memory_version": 5, "content_excerpt": "same", "memory_type": "semantic"}
        await ops._recheck_reindexed_heads([read], [row], ch=object(), model="fake")
    assert checked and checked[0][0]["memory_key"] == row["memory_key"]


@pytest.mark.asyncio
async def test_the_live_read_is_chunked_and_can_be_sequential() -> None:
    """fence_high re-reads the live vectors of every key it fenced (a rollback can pass
    20,000): one statement per 500 keys, sequentially consistent when asked."""
    calls: list[tuple[int, str]] = []

    class _CH:
        async def query_all(self, sql: str, params: Any = None) -> list[Any]:
            calls.append((len(params["k"]), sql))
            return []

    keys = [("o", "n", "a", "u", "s", f"k{i}") for i in range(1203)]
    await M.live_rows_for_keys(keys, ch=_CH(), sequential=True)
    assert [n for n, _ in calls] == [500, 500, 203]
    assert all("select_sequential_consistency" in sql for _, sql in calls)
