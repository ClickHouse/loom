"""memory.consolidate must not corrupt or hide data.

(a) the canonical merged row carries the UNION of the sources' facets and
    the earliest validity anchors, and every persisted column;
(b) merged-from rows are tombstoned with a FULL version row (RMT FINAL
    would otherwise zero memory_id/tags/entities/created_at/... and leave
    memory.restore nothing to restore);
(c) a cluster whose merge LLM call fails is logged and recorded in
    loom.consolidation_runs with status='failed';
(d) a failing consolidation_runs insert is logged, not swallowed.

CH-free: stub client / LLM / embedder / repo lookups.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Any

import pytest

from server.memory.api.models_admin import MemoryConsolidateInput
from server.memory.app.consolidate import (
    _consolidation_canonical_row,
    _summary_sensitivity,
    handle_memory_consolidate,
)
from server.memory.store import (
    EMBEDDING_PERSISTED_COLS,
    MEMORY_PERSISTED_COLS,
    MemoryRow,
)

_T0 = datetime(2024, 1, 1, 12, 0, 0)


def _cand(key: str, mid: str, *, age_days: int, emb: list[float], **over: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "memory_id": mid,
        "memory_key": key,
        "memory_type": "semantic",
        "value": f"value of {key}",
        "created_at": _T0 - timedelta(days=age_days),
        "tags": [],
        "files": [],
        "concepts": [],
        "valid_at": None,
        "temporal_anchor": None,
        "embedding": emb,
        "entities": ["bowie"],
        "content_sha256": "a" * 64,  # what `_existing` holds, so the merge retires it
        "version": 5,
        "session_id": "-",  # the canonical row's session, so a same-key merge writes over it
    }
    row.update(over)
    return row


def _input(**over: Any) -> MemoryConsolidateInput:
    base: dict[str, Any] = {
        "org": "o",
        "namespace": "n",
        "agent": "a",
        "user_id": "u",
        "min_age_seconds": 0,
        "similarity_threshold": 0.8,
    }
    base.update(over)
    return MemoryConsolidateInput(**base)


# ----------------------------------------------------------------------
# (a) canonical row
# ----------------------------------------------------------------------


def test_canonical_row_unions_facets_and_takes_earliest_anchors() -> None:
    cluster = [
        _cand(
            "dog_name",
            "11111111-1111-1111-1111-111111111111",
            age_days=10,
            emb=[1, 0, 0, 0],
            tags=["pet"],
            files=["a.py"],
            concepts=["dog"],
            valid_at=datetime(2023, 5, 1),
            temporal_anchor=datetime(2023, 5, 2),
            entities=["bowie"],
        ),
        _cand(
            "dog_food",
            "22222222-2222-2222-2222-222222222222",
            age_days=1,
            emb=[1, 0, 0, 0],
            tags=["pet", "food"],
            files=["b.py"],
            concepts=["kibble"],
            valid_at=datetime(2023, 6, 1),
            temporal_anchor=None,
            entities=["bowie", "acana"],
        ),
    ]
    row = _consolidation_canonical_row(
        _input(),
        cluster,
        canonical_key="dog",
        canonical_value="Bowie eats Acana.",
        content_sha256="0" * 64,
        version=1,
        now=_T0,
        sensitivity=_summary_sensitivity(cluster, "Bowie eats Acana."),
    )
    assert row["tags"] == ["pet", "food"]
    assert row["entities"] == ["bowie", "acana"]
    assert row["files"] == ["a.py", "b.py"]
    assert row["concepts"] == ["dog", "kibble"]
    assert row["valid_at"] == datetime(2023, 5, 1)
    assert row["temporal_anchor"] == datetime(2023, 5, 2)
    assert row["pinned"] == 0
    assert row["memory_type"] == "semantic"
    missing = [c for c in MEMORY_PERSISTED_COLS if c not in row]
    assert not missing, f"canonical row missing {missing}"
    # New canonical key -> fresh id, created now.
    assert row["memory_id"] not in {c["memory_id"] for c in cluster}
    assert row["created_at"] == _T0


def test_canonical_row_overwriting_a_merged_key_keeps_its_identity() -> None:
    cluster = [
        _cand("dog_name", "11111111-1111-1111-1111-111111111111", age_days=10, emb=[1, 0]),
        _cand("dog_food", "22222222-2222-2222-2222-222222222222", age_days=1, emb=[1, 0]),
    ]
    row = _consolidation_canonical_row(
        _input(),
        cluster,
        canonical_key="dog_food",
        canonical_value="v",
        content_sha256="0" * 64,
        version=1,
        now=_T0,
        sensitivity=_summary_sensitivity(cluster, "v"),
    )
    assert row["memory_id"] == "22222222-2222-2222-2222-222222222222"
    assert row["created_at"] == cluster[1]["created_at"]


# ----------------------------------------------------------------------
# Fakes for the full handler
# ----------------------------------------------------------------------


class _CH:
    def __init__(self, rows: list[dict[str, Any]], *, fail_runs_insert: bool = False) -> None:
        self.rows = rows
        self.inserts: list[tuple[str, list[dict[str, Any]]]] = []
        self.executes: list[str] = []
        self.fail_runs_insert = fail_runs_insert

    async def query_all(self, sql, params=None):
        return list(self.rows)

    async def execute(self, sql, params=None):
        self.executes.append(sql)

    async def insert(self, table, rows, settings=None):
        if table == "loom.consolidation_runs" and self.fail_runs_insert:
            raise RuntimeError("CH down")
        self.inserts.append((table, rows))


class _LLM:
    provider = "fake"
    model = "fake-merge"

    def __init__(self, text: str) -> None:
        self.text = text

    async def complete(self, system, user, max_tokens=256):
        class _R:
            text = self.text
            tokens_in = 11
            tokens_out = 7

        return _R()


class _Emb:
    model_name = "fake-emb"
    dim = 4

    async def embed(self, text):
        return [1.0, 0.0, 0.0, 0.0]


def _existing(mid: str, key: str) -> MemoryRow:
    return MemoryRow(
        memory_id=mid,
        org="o",
        namespace="n",
        agent="a",
        user_id="u",
        session_id="-",
        memory_key=key,
        memory_type="semantic",
        value=f"value of {key}",
        confidence=0.9,
        version=5,
        pinned=False,
        created_at=_T0 - timedelta(days=10),
        expires_at=None,
        tags=["pet"],
        entities=["bowie"],
        content_sha256="a" * 64,
        run_id="r1",
    )


def _wire(monkeypatch: pytest.MonkeyPatch, ch: _CH, llm: _LLM, *, guard: bool = False) -> None:
    import server.core.ch as chmod
    import server.embedding as embmod
    import server.llm.client as llmmod
    from server.memory.store import lifecycle

    monkeypatch.setattr(chmod, "get_client", lambda: ch)
    monkeypatch.setattr(embmod, "get_embedder", lambda: _Emb())
    monkeypatch.setattr(llmmod, "get_llm_client", lambda: llm)
    # These fixtures' values are "value of <key>" placeholders and the fake
    # merges are unrelated sentences; the merge guard would
    # refuse every one of them. It has its own test below.
    from server.memory.domain import merge_guard

    if not guard:
        monkeypatch.setattr(
            merge_guard,
            "merge_preserves",
            lambda sources, merged: merge_guard.MergeVerdict(ok=True),
        )

    async def _get_by_id(mid, *, org=None, ch=None, **kw):
        key = {r["memory_id"]: r["memory_key"] for r in ch_rows_by_id}.get(mid, "k")
        return _existing(mid, key)

    async def _get_by_key(identity, *, memory_key, **kw):
        # the canonical key's head: the merged row there, if the cluster read one
        mid = next((r["memory_id"] for r in ch_rows_by_id if r["memory_key"] == memory_key), None)
        return _existing(mid, memory_key) if mid is not None else None

    ch_rows_by_id = ch.rows
    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)
    monkeypatch.setattr("server.memory.store.get_by_key", _get_by_key)


def _two_row_cluster() -> list[dict[str, Any]]:
    return [
        _cand(
            "dog_food",
            "22222222-2222-2222-2222-222222222222",
            age_days=1,
            emb=[1.0, 0.0, 0.0, 0.0],
            tags=["food"],
        ),
        _cand(
            "dog_name",
            "11111111-1111-1111-1111-111111111111",
            age_days=10,
            emb=[0.99, 0.01, 0.0, 0.0],
            tags=["pet"],
        ),
    ]


# ----------------------------------------------------------------------
# (b) tombstone mirrors every column
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_merged_from_rows_get_full_tombstone_version_rows(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ch = _CH(_two_row_cluster())
    _wire(
        monkeypatch,
        ch,
        _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "Bowie eats Acana kibble."})),
    )
    # The embeddings mirror is exercised by its own tests below; the stub CH
    # answers every query with the cluster rows, so keep it out of this one.
    from server.memory import store as _mr

    async def _no_emb_tomb(*a, **k):
        return 0

    monkeypatch.setattr(_mr, "tombstone_embeddings_for_key", _no_emb_tomb)
    out = await handle_memory_consolidate(_input())
    assert out.clusters_found == 1 and len(out.consolidated) == 1
    assert out.clusters_failed == 0
    # No partial-column INSERT..SELECT tombstone.
    assert ch.executes == []

    mem_rows = [r for t, rows in ch.inserts if t == "loom.memory" for r in rows]
    tombstones = [r for r in mem_rows if r.get("tombstoned_at") is not None]
    assert {r["memory_key"] for r in tombstones} == {"dog_name", "dog_food"}
    for row in tombstones:
        missing = [c for c in MEMORY_PERSISTED_COLS if c not in row]
        assert not missing, f"tombstone for {row['memory_key']} missing {missing}"
        # The columns a partial INSERT..SELECT would zero are carried forward.
        assert row["memory_id"] in {
            "11111111-1111-1111-1111-111111111111",
            "22222222-2222-2222-2222-222222222222",
        }
        assert row["tags"] == ["pet"] and row["entities"] == ["bowie"]
        assert row["created_at"] == _T0 - timedelta(days=10)
        assert row["content_sha256"] == "a" * 64
        assert row["run_id"] == "r1"
        assert row["version"] > 5

    canonical = [r for r in mem_rows if r.get("tombstoned_at") is None]
    assert len(canonical) == 1
    assert canonical[0]["memory_key"] == "dog"
    assert canonical[0]["tags"] == ["pet", "food"]  # oldest-first union
    assert canonical[0]["entities"] == ["bowie"]
    emb_rows = [r for t, rows in ch.inserts if t == "loom.memory_embeddings" for r in rows]
    assert len(emb_rows) == 1
    assert emb_rows[0]["tags"] == ["pet", "food"]
    assert not [c for c in EMBEDDING_PERSISTED_COLS if c not in emb_rows[0]]

    runs = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert len(runs) == 1 and runs[0]["status"] == "written"


@pytest.mark.asyncio
async def test_merged_from_rows_are_tombstoned_on_the_embeddings_table_too(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Search reads memory_embeddings: a merged-from row tombstoned only in
    loom.memory must not surface beside the canonical."""
    from server.memory import store as memory_repo

    ch = _CH(_two_row_cluster())
    _wire(
        monkeypatch,
        ch,
        _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "Bowie eats Acana kibble."})),
    )
    calls: list[tuple[str, str, datetime]] = []
    versions: list[int] = []

    async def _tomb(org, ns, agent, user_id, session_id, key, *, ts, version, ch=None):
        calls.append((session_id, key, ts))
        versions.append(version)
        return 1

    monkeypatch.setattr(memory_repo, "tombstone_embeddings_for_key", _tomb)
    out = await handle_memory_consolidate(_input())
    assert len(out.consolidated) == 1
    assert sorted(k for _, k, _ in calls) == ["dog_food", "dog_name"], (
        "every merged-from key, not the canonical"
    )
    assert all(isinstance(ts, datetime) for _, _, ts in calls)
    # Each vector retire carries the memory tombstone's version.
    mem_rows = [r for t, rows in ch.inserts if t == "loom.memory" for r in rows]
    assert set(versions) == {r["version"] for r in mem_rows if r.get("tombstoned_at") is not None}


@pytest.mark.asyncio
@pytest.mark.parametrize("canonical", ["dog_food", "dog_name"])
async def test_a_canonical_named_like_a_source_still_tombstones_the_rest(
    monkeypatch: pytest.MonkeyPatch, canonical: str
) -> None:
    """The model may keep one of the merged keys as the canonical (the documented
    overwrite path): every other source is still tombstoned on the embeddings
    table, and the run is still recorded as written."""
    from server.memory import store as memory_repo

    ch = _CH(_two_row_cluster())
    _wire(
        monkeypatch,
        ch,
        _LLM(json.dumps({"canonical_key": canonical, "canonical_value": "Bowie eats Acana."})),
    )
    calls: list[str] = []

    async def _tomb(org, ns, agent, user_id, session_id, key, *, ts, version, ch=None):
        calls.append(key)
        return 1

    monkeypatch.setattr(memory_repo, "tombstone_embeddings_for_key", _tomb)
    out = await handle_memory_consolidate(_input())
    assert len(out.consolidated) == 1
    assert calls == [k for k in ("dog_food", "dog_name") if k != canonical]
    runs = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert [r["status"] for r in runs] == ["written"]


def test_tombstone_embeddings_for_key_mirrors_every_column_and_sets_the_stamp() -> None:
    import asyncio
    from unittest.mock import AsyncMock, patch

    from server.memory import store as mr
    from server.memory.store import embeddings

    ch = AsyncMock()
    ch.query_all.return_value = [
        {
            "org": "o",
            "namespace": "n",
            "agent": "a",
            "user_id": "-",
            "session_id": "s1",
            "memory_key": "dog_name",
            "status": "active",
            "embedding": [0.1, 0.2],
            "embedding_dim": 2,
            "embedding_model": "m",
            "content_excerpt": "x",
            "tombstoned_at": None,
            "tags": ["pet"],
        }
    ]
    inserted: list[dict[str, Any]] = []

    async def fake_insert(rows, *, ch=None):
        inserted.extend(rows)

    ts = datetime(2026, 9, 11, 12, 0, 0)
    with patch.object(embeddings, "insert_embedding_rows", new=fake_insert):
        n = asyncio.run(
            mr.tombstone_embeddings_for_key(
                "o", "n", "a", "-", "s1", "dog_name", ts=ts, version=7, ch=ch
            )
        )
    assert n == 1 and inserted[0]["tombstoned_at"] == ts
    assert inserted[0]["embedding"] == [0.1, 0.2] and inserted[0]["tags"] == ["pet"], (
        "vector + facets carried"
    )
    assert not [c for c in EMBEDDING_PERSISTED_COLS if c not in inserted[0]]


# ----------------------------------------------------------------------
# (c) failed merge is recorded, (d) failing run insert is logged
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failed_merge_writes_failed_run_row_and_warns(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ch = _CH(_two_row_cluster())
    _wire(monkeypatch, ch, _LLM("Sorry, I cannot merge these."))
    with caplog.at_level(logging.WARNING, logger="server.memory.app.consolidate"):
        out = await handle_memory_consolidate(_input())
    assert out.clusters_found == 1
    assert out.consolidated == []
    assert out.clusters_failed == 1
    # Nothing was written to the memory tables.
    assert not [t for t, _ in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")]
    runs = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert len(runs) == 1
    assert runs[0]["status"] == "failed"
    assert "no JSON object" in runs[0]["failure_reason"]
    assert runs[0]["source_memory_keys"] == ["dog_name", "dog_food"]
    assert any("merge failed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.asyncio
async def test_consolidation_runs_insert_failure_is_logged(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    ch = _CH(_two_row_cluster(), fail_runs_insert=True)
    _wire(
        monkeypatch,
        ch,
        _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "Bowie eats Acana kibble."})),
    )
    with caplog.at_level(logging.WARNING, logger="server.memory.app.consolidate"):
        out = await handle_memory_consolidate(_input())
    assert len(out.consolidated) == 1  # the merge itself succeeded
    assert any("consolidation_runs insert failed" in rec.getMessage() for rec in caplog.records)


# --- merges run concurrently, results keep cluster order ------


@pytest.mark.asyncio
async def test_consolidate_results_follow_cluster_order_not_completion_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three clusters whose merge calls complete in reverse order are still reported in
    the order their merges started, the cluster order."""
    import asyncio

    from server.memory.app import consolidate
    from server.settings import settings

    assert "Later entries update earlier ones, never the reverse" in consolidate._MERGE_SYSTEM
    monkeypatch.setattr(settings, "consolidation_merge_concurrency", 3)
    rows = [
        _cand(f"{topic}{i}", f"{n}{i}" * 8, age_days=i, emb=emb, entities=[topic])
        for n, (topic, emb) in enumerate(
            [("a", [1.0, 0, 0, 0]), ("b", [0, 1.0, 0, 0]), ("c", [0, 0, 1.0, 0])]
        )
        for i in (1, 2)
    ]
    started: list[str] = []
    finished: list[str] = []
    gates = {t: asyncio.Event() for t in "abc"}

    class _Gated(_LLM):
        async def complete(self, system, user, max_tokens=256):
            topic = next(t for t in "abc" if f"{t}1" in user)
            started.append(topic)
            if len(started) == 3:  # all three are in flight: release them newest first
                gates[topic].set()
            await gates[topic].wait()
            finished.append(topic)
            if len(started) > len(finished):
                gates[started[-1 - len(finished)]].set()
            self.text = json.dumps({"canonical_key": topic, "canonical_value": f"{topic} merged"})
            return await super().complete(system, user, max_tokens)

    _wire(monkeypatch, _CH(rows), _Gated(""))
    out = await handle_memory_consolidate(_input(dry_run=True))
    assert len(started) == 3 and finished == started[::-1]
    assert [r.canonical_key for r in out.consolidated] == started


def test_canonical_row_inherits_the_most_sensitive_source() -> None:
    """A 'medium' source (an email) merged into a canonical row must not come
    out unlabelled — the label rides with the content it describes."""
    cluster = [
        _cand(
            "contact",
            "11111111-1111-1111-1111-111111111111",
            age_days=3,
            emb=[1, 0, 0, 0],
            tags=[],
            files=[],
            concepts=[],
            valid_at=None,
            temporal_anchor=None,
            entities=[],
        ),
        _cand(
            "contact2",
            "22222222-2222-2222-2222-222222222222",
            age_days=1,
            emb=[1, 0, 0, 0],
            tags=[],
            files=[],
            concepts=[],
            valid_at=None,
            temporal_anchor=None,
            entities=[],
        ),
    ]
    cluster[0]["sensitivity"] = "medium"
    row = _consolidation_canonical_row(
        _input(),
        cluster,
        canonical_key="contact",
        canonical_value="Owner prefers email.",
        content_sha256="0" * 64,
        version=1,
        now=_T0,
        sensitivity=_summary_sensitivity(cluster, "Owner prefers email."),
    )
    assert row["sensitivity"] == "medium" and "sensitivity:medium" in row["tags"]
    # unlabelled sources, but the merged text itself carries a contact detail
    for c in cluster:
        c["sensitivity"] = ""
    row = _consolidation_canonical_row(
        _input(),
        cluster,
        canonical_key="contact",
        canonical_value="Owner: ops@example.com",
        content_sha256="0" * 64,
        version=1,
        now=_T0,
        sensitivity=_summary_sensitivity(cluster, "Owner: ops@example.com"),
    )
    assert row["sensitivity"] == "medium"
    row = _consolidation_canonical_row(
        _input(),
        cluster,
        canonical_key="contact",
        canonical_value="Owner prefers email.",
        content_sha256="0" * 64,
        version=1,
        now=_T0,
        sensitivity=_summary_sensitivity(cluster, "Owner prefers email."),
    )
    assert row["sensitivity"] == ""


# ----------------------------------------------------------------------
# (e) the merge guard: a lossy merge is refused, the sources stay
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_lossy_merge_is_refused_and_sources_stay(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    rows = _two_row_cluster()
    rows[0]["value"] = "Bowie eats Acana kibble, 300 g a day."
    rows[1]["value"] = "The dog adopted on 3 March 2023 is named Bowie."
    ch = _CH(rows)
    _wire(
        monkeypatch,
        ch,
        _LLM(
            json.dumps(
                {"canonical_key": "dog", "canonical_value": "The dog Bowie eats Acana kibble."}
            )
        ),
        guard=True,
    )

    with caplog.at_level(logging.WARNING, logger="server.memory.app.consolidate"):
        out = await handle_memory_consolidate(_input())
    assert out.clusters_found == 1 and out.consolidated == []
    assert out.clusters_failed == 1 and out.clusters_refused_lossy == 1
    assert not [t for t, _ in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")]
    runs = [r for t, rs in ch.inserts if t == "loom.consolidation_runs" for r in rs]
    assert len(runs) == 1 and runs[0]["status"] == "refused_lossy"
    # what was lost, by kind and count: the sources' values never reach the run table
    assert "dates: 1" in runs[0]["failure_reason"] and "quantities: 1" in runs[0]["failure_reason"]
    assert "300" not in runs[0]["failure_reason"] and "march" not in runs[0]["failure_reason"]
    # a merge that keeps every value goes through
    ch2 = _CH(_two_row_cluster())
    for r in ch2.rows:
        r["value"] = {
            "dog_food": "Bowie eats Acana kibble, 300 g a day.",
            "dog_name": "The dog adopted on 3 March 2023 is named Bowie.",
        }[r["memory_key"]]
    _wire(
        monkeypatch,
        ch2,
        _LLM(
            json.dumps(
                {
                    "canonical_key": "dog",
                    "canonical_value": "Bowie, the dog adopted on 3 March 2023, eats Acana kibble, 300 g a day.",
                }
            )
        ),
        guard=True,
    )
    out2 = await handle_memory_consolidate(_input())
    assert out2.clusters_refused_lossy == 0 and len(out2.consolidated) == 1


@pytest.mark.asyncio
async def test_an_unlabelled_credential_row_never_reaches_the_merge_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # copy_to and imports leave a row unlabelled until the sweep; its value never goes into
    # the merge prompt.
    from server.memory.app.consolidate import handle_memory_consolidate

    token = "ghp_" + "a" * 36
    rows = _two_row_cluster()
    rows[0]["value"] = f"deploy token:\n{token}"
    sent: list[str] = []
    llm = _LLM('{"canonical_key":"dog","canonical_value":"a dog"}')

    async def _complete(system, user, max_tokens=256):
        sent.append(user)
        return await _LLM.complete(llm, system, user, max_tokens)

    llm.complete = _complete  # type: ignore[method-assign]
    _wire(monkeypatch, _CH(rows), llm)
    out = await handle_memory_consolidate(_input())
    assert out.examined == 1 and not any(token in u for u in sent)


@pytest.mark.parametrize(
    "since",
    [
        {"pinned": True},
        {"lock_mode": "no_delete"},
        {"content_sha256": "b" * 64},
        {"redacted_at": _T0},
    ],
    ids=["pinned", "locked", "rewritten", "redacted"],
)
@pytest.mark.asyncio
async def test_a_canonical_is_not_written_over_a_head_held_since_the_read(
    monkeypatch: pytest.MonkeyPatch, since: dict[str, Any]
) -> None:
    """The canonical key is a merged one whose head was pinned, locked or rewritten after
    the cluster read: the merge leaves it, and its cluster counts as failed."""
    from server.memory.app import consolidate

    inserted: list[Any] = []

    cluster = _two_row_cluster()
    over = next(r for r in cluster if r["memory_key"] == "dog_food")

    async def _head(identity, *, memory_key, **_):
        return _existing(over["memory_id"], memory_key).model_copy(update=since)

    async def _insert(rows, *a, **k):
        inserted.extend(rows)

    monkeypatch.setattr("server.memory.store.get_by_key", _head)
    monkeypatch.setattr("server.memory.store.insert_memory_versions", _insert)
    monkeypatch.setattr("server.memory.store.insert_versions_over_heads", _insert)
    with pytest.raises(consolidate._HeldSince):
        await consolidate._write_canonical(
            object(),
            _input(),
            _Emb(),
            cluster,
            canonical_key="dog_food",
            canonical_value="merged",
            now_naive=_T0,
            claimed=set(),
        )
    assert inserted == []


def _canonical_writes(monkeypatch: pytest.MonkeyPatch, *heads: Any) -> list[dict[str, Any]]:
    """`_write_canonical` reading `heads` at the keys of the same names; with the commits it
    makes, each running its `landed` and `then` as the commit does, and the lineage each
    `landed` links."""
    commits: list[dict[str, Any]] = []
    by_key = {h.memory_key: h for h in heads}

    async def _head(identity, *, memory_key, **_):
        assert identity.session_id == "-"  # the canonical row's own identity
        return by_key.get(memory_key)

    async def _commit(rows, read, *, ch=None, then=None, landed=None, on_commit=None, **k):
        from server.core import pg

        commits.append({"rows": rows, "read": read, **k})
        if on_commit is not None:
            on_commit()
        if not pg.enabled():  # the rows, `landed`, then `then`
            await landed()
            await then()
            return 0
        failed = None  # `then` under the lease, `landed` after the publish, then its error
        try:
            await then()
        except Exception as e:  # noqa: BLE001 — raised after `landed`, as the commit does
            failed = e
        await landed()
        if failed is not None:
            raise failed
        return 0

    async def _linked(**link):
        commits.append({"lineage": link["memory_links"]})

    monkeypatch.setattr("server.memory.store.get_by_key", _head)
    monkeypatch.setattr("server.memory.store.insert_versions_over_heads", _commit)
    monkeypatch.setattr("server.memory.app.evidence.link_belief_sources", _linked)
    return commits


@pytest.mark.asyncio
async def test_a_canonical_over_a_merged_key_goes_above_it_with_every_merged_row_as_a_source(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    over = next(r for r in cluster if r["memory_key"] == "dog_food")
    head = _existing(over["memory_id"], "dog_food")  # as the cluster read it, at 5
    commits = _canonical_writes(monkeypatch, head)
    ch = _CH([])
    await consolidate._write_canonical(
        ch,
        _input(),
        _Emb(),
        cluster,
        canonical_key="dog_food",
        canonical_value="v",
        now_naive=_T0,
        claimed=set(),
    )
    commit, lineage = commits
    [row] = commit["rows"]
    assert commit["read"] == [head] and row["version"] >= 7
    assert lineage == {"lineage": [("dog_food", over["memory_id"])]}
    assert commit["sources"] == [(str(r["memory_id"]), 5) for r in cluster]
    assert commit["live_sources"] and commit["documented"] == [True]
    # the vector, written by the commit's `then`, at the row's own version and identity
    [(table, [vector])] = ch.inserts
    assert table == "loom.memory_embeddings"
    assert (vector["version"], vector["session_id"]) == (row["version"], "-")


@pytest.mark.parametrize("pinned", [False, True])
@pytest.mark.asyncio
async def test_a_key_holding_a_head_the_merge_never_read_gives_way_to_the_clusters_own(
    monkeypatch: pytest.MonkeyPatch, pinned: bool
) -> None:
    """The summary is not written over a live head it never read, protected or not: it
    takes the cluster's own key."""
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    unread = _existing("33333333-3333-3333-3333-333333333333", "taken").model_copy(
        update={"pinned": pinned}
    )
    commits = _canonical_writes(monkeypatch, unread)
    claimed: set[str] = set()
    row = await consolidate._write_canonical(
        _CH([]),
        _input(),
        _Emb(),
        cluster,
        canonical_key="taken",
        canonical_value="v",
        now_naive=_T0,
        claimed=claimed,
    )
    own = consolidate._cluster_key("taken", cluster)
    commit, lineage = commits
    assert row["memory_key"] == own and own.startswith("taken-") and commit["read"] == [None]
    assert lineage == {"lineage": [(own, row["memory_id"])]}
    assert claimed == {own}  # the model's key was another's row: left unclaimed


@pytest.mark.asyncio
async def test_a_key_another_cluster_of_the_pass_claimed_is_not_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clusters merge concurrently: the second to choose a key takes its own, whatever the
    first has written yet."""
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    read: list[str] = []

    async def _head(identity, *, memory_key, **_):
        read.append(memory_key)

    monkeypatch.setattr("server.memory.store.get_by_key", _head)
    key, head = await consolidate._summary_target(_input(), cluster, "pets", {"pets"}, high=False)
    assert (key, head, read) == (consolidate._cluster_key("pets", cluster), None, [key])


@pytest.mark.parametrize("case", ["both keys taken", "contended", "erased"])
@pytest.mark.asyncio
async def test_a_canonical_the_merge_cannot_stand_behind_leaves_the_cluster(
    monkeypatch: pytest.MonkeyPatch, case: str
) -> None:
    """Both keys a summary may take holding rows the merge never read, or a commit that
    finds a merged row moved or erased, refuses the cluster."""
    from server.memory.app import consolidate
    from server.memory.store import pg_memory

    cluster = _two_row_cluster()
    if case == "both keys taken":
        own = consolidate._cluster_key("taken", cluster)
        commits = _canonical_writes(
            monkeypatch,
            _existing("33333333-3333-3333-3333-333333333333", "taken"),
            _existing("44444444-4444-4444-4444-444444444444", own),
        )
    else:
        over = next(r for r in cluster if r["memory_key"] == "dog_food")
        commits = _canonical_writes(monkeypatch, _existing(over["memory_id"], "dog_food"))
        refused = (
            pg_memory.AdmissionContendedError("moved")
            if case == "contended"
            else pg_memory.MemoryErased()
        )

        async def _refused(*a, **k):
            commits.append(k)
            raise refused

        monkeypatch.setattr("server.memory.store.insert_versions_over_heads", _refused)
    ch = _CH([])
    refusal = consolidate._KeyTaken if case == "both keys taken" else consolidate._HeldSince
    with pytest.raises(refusal):
        await consolidate._write_canonical(
            ch,
            _input(),
            _Emb(),
            cluster,
            canonical_key="taken" if case == "both keys taken" else "dog_food",
            canonical_value="v",
            now_naive=_T0,
            claimed=set(),
        )
    assert len(commits) == (0 if case == "both keys taken" else 1)
    assert ch.inserts == []


@pytest.mark.asyncio
async def test_a_merged_key_in_another_session_is_retired_beside_a_canonical_of_its_own(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    for r in cluster:
        r["session_id"] = "s1"
    commits = _canonical_writes(monkeypatch)
    await consolidate._write_canonical(
        _CH([]),
        _input(),
        _Emb(),
        cluster,
        canonical_key="dog_food",
        canonical_value="v",
        now_naive=_T0,
        claimed=set(),
    )
    assert commits[0]["read"] == [None]  # a key of its own at session '-'
    retired = await _retire(monkeypatch, cluster, canonical_key="dog_food")
    assert retired[0] == [str(r["memory_id"]) for r in cluster]


def _patch_retire(
    monkeypatch: pytest.MonkeyPatch,
    *,
    contended: int = 0,
    erased: bool = False,
    held: frozenset[str] = frozenset(),
    gone: frozenset[str] = frozenset(),
) -> list[list[str]]:
    """The id batches `_retire_merged` writes tombstones for, the first `contended` of
    them refused under Postgres (a moved head, or an `erased` row), the `held` ids read
    as rewritten since the read and the `gone` ones as deleted, then a
    `["search rows"]` for each search-row retire and a `["chunks", key]` for each chunk
    retire it makes."""
    from server.memory.store import pg_memory

    batches: list[list[str]] = []

    async def _write(org, ids, build, *, skip, **_):
        batches.append(list(ids))
        if len(batches) <= contended:
            raise pg_memory.MemoryErased() if erased else pg_memory.AdmissionContendedError("moved")
        for mid in ids:
            head = _existing(mid, "k")
            if mid in held:
                head = head.model_copy(update={"content_sha256": "b" * 64})
            if mid not in gone and not skip(head):
                build(head)

    async def _search_rows(*_a, **_k):
        batches.append(["search rows"])
        return 0

    async def _chunks(*, memory_key, **_):
        batches.append(["chunks", memory_key])

    monkeypatch.setattr("server.memory.store.write_versions_by_ids", _write)
    monkeypatch.setattr("server.memory.store.tombstone_embeddings_for_key", _search_rows)
    monkeypatch.setattr("server.memory.app.set_chunks.tombstone_set_chunks_fail_open", _chunks)
    return batches


async def _retire(
    monkeypatch: pytest.MonkeyPatch,
    cluster: list[dict[str, Any]],
    *,
    canonical_key: str,
    contended: int = 0,
    erased: bool = False,
    held: frozenset[str] = frozenset(),
    gone: frozenset[str] = frozenset(),
) -> list[list[str]]:
    """What `_retire_merged` writes (`_patch_retire`), checking what it answers."""
    from server.memory.app import consolidate
    from server.settings import settings

    batches = _patch_retire(monkeypatch, contended=contended, erased=erased, held=held, gone=gone)
    retired = await consolidate._retire_merged(
        _CH([]), _input(), cluster, canonical_key=canonical_key, now_naive=_T0
    )
    assert retired == (contended < settings.pg_admission_rounds and not held)
    return batches


@pytest.mark.parametrize("contended", [1, 99])
@pytest.mark.asyncio
async def test_retiring_merged_rows_that_moved_reads_them_again(
    monkeypatch: pytest.MonkeyPatch, contended: int
) -> None:
    """A moved head refuses the batch: it is read again, up to `pg_admission_rounds`."""
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_admission_rounds", 3)
    cluster = _two_row_cluster()
    batches = await _retire(monkeypatch, cluster, canonical_key="dog", contended=contended)
    rounds = [b for b in batches if b[0] not in ("search rows", "chunks")]
    assert len(rounds) == min(contended + 1, 3)
    # a batch never written retires no search rows at versions that do not exist
    assert len(batches) - len(rounds) == (0 if contended >= 3 else 2 * len(cluster))


@pytest.mark.asyncio
async def test_a_merged_row_erased_while_retiring_is_read_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An erasure refuses the batch as a moved head does; read again, the row is gone."""

    batches = await _retire(
        monkeypatch,
        _two_row_cluster(),
        canonical_key="dog",
        contended=1,
        erased=True,
    )
    assert [b for b in batches if b[0] not in ("search rows", "chunks")] == [
        [r["memory_id"] for r in _two_row_cluster()]
    ] * 2


@pytest.mark.asyncio
async def test_the_merged_row_a_summary_writes_over_loses_its_chunks_not_its_row(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cluster = _two_row_cluster()
    batches = await _retire(monkeypatch, cluster, canonical_key="dog_food")
    assert batches[0] == ["chunks", "dog_food"]  # the summary's own key: no chunks of its own
    other = next(r for r in cluster if r["memory_key"] != "dog_food")
    assert batches[1] == [other["memory_id"]]


@pytest.mark.parametrize(
    ("refusal", "drop", "reason"),
    [
        ("_HeldSince", "held_since_read", "a merged row changed since the read"),
        ("_KeyTaken", "key_taken", "no key was free for the summary"),
    ],
)
@pytest.mark.asyncio
async def test_a_cluster_the_merge_cannot_write_is_recorded_as_failed(
    monkeypatch: pytest.MonkeyPatch, refusal: str, drop: str, reason: str
) -> None:
    """Counted, in the drop ledger and in a failed run row, and nothing written."""
    from server.memory.app import consolidate

    ch = _CH(_two_row_cluster())
    _wire(
        monkeypatch,
        ch,
        _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "Bowie eats Acana."})),
    )
    dropped: list[str] = []

    async def _refused(*_a, **_k):
        raise getattr(consolidate, refusal)("dog")

    monkeypatch.setattr(consolidate, "_write_canonical", _refused)
    monkeypatch.setattr(
        consolidate, "record_drop", lambda _org, _ns, _stage, why, **_: dropped.append(why)
    )
    out = await handle_memory_consolidate(_input())
    assert (out.clusters_failed, out.consolidated, dropped) == (1, [], [drop])
    assert not [t for t, _ in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")]
    [run] = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert (run["status"], run["failure_reason"]) == ("failed", reason)


@pytest.mark.asyncio
async def test_merged_rows_left_live_are_recorded_beside_a_written_summary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import consolidate
    from server.settings import settings

    monkeypatch.setattr(settings, "pg_admission_rounds", 2)
    cluster = _two_row_cluster()
    ch = _CH(cluster)
    _wire(monkeypatch, ch, _LLM(json.dumps({"canonical_key": "dog_food", "canonical_value": "v"})))
    dropped: list[tuple[str, str, int]] = []

    async def _written(*_a, **_k):
        return {"memory_key": "dog_food", "memory_type": "semantic"}

    monkeypatch.setattr(consolidate, "_write_canonical", _written)
    monkeypatch.setattr(
        consolidate,
        "record_drop",
        lambda _org, _ns, _stage, why, *, item, n: dropped.append((why, item, n)),
    )
    _patch_retire(monkeypatch, contended=99)
    await handle_memory_consolidate(_input())
    # only the row the summary does not keep was to be retired
    assert dropped == [("retire_contended", "dog_name", 1)]
    [run] = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert run["status"] == "written" and "left live" in run["failure_reason"]


@pytest.mark.parametrize("authority", ["postgres", "clickhouse"])
@pytest.mark.asyncio
async def test_a_failed_summary_vector_is_owed_under_postgres_and_raised_on_clickhouse(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    """The failure leaves the commit's `then`, so the commit does not settle the document
    it owes; the merge goes on under Postgres, where the delivery worker writes it."""
    from server.core import pg
    from server.memory.app import consolidate

    class _DownCH(_CH):
        async def insert(self, table, rows, settings=None):
            raise RuntimeError("CH down")

    commits = _canonical_writes(monkeypatch)
    monkeypatch.setattr(pg, "enabled", lambda: authority == "postgres")
    call = consolidate._write_canonical(
        _DownCH([]),
        _input(),
        _Emb(),
        _two_row_cluster(),
        canonical_key="dog",
        canonical_value="v",
        now_naive=_T0,
        claimed=set(),
    )
    if authority == "postgres":
        assert (await call)["memory_key"] == "dog"
    else:
        with pytest.raises(RuntimeError, match="CH down"):
            await call
    assert commits[0]["documented"] == [True]
    assert "lineage" in commits[1]  # written whatever the vector did


@pytest.mark.parametrize("authority", ["postgres", "clickhouse"])
@pytest.mark.asyncio
async def test_a_high_summarys_failed_fence_is_recorded_and_the_summary_stands(
    monkeypatch: pytest.MonkeyPatch, authority: str
) -> None:
    """At a key of no merged row's, only a vector still in flight goes unfenced: the key is
    recorded, and the merge goes on to retire its rows."""
    from server.core import pg
    from server.memory.app import consolidate, high_fence

    async def _down(*_a, **_k):
        raise RuntimeError("CH down")

    dropped: list[tuple[str, str]] = []
    commits = _canonical_writes(monkeypatch)
    monkeypatch.setattr(pg, "enabled", lambda: authority == "postgres")
    monkeypatch.setattr(high_fence, "fence_high", _down)
    monkeypatch.setattr(
        high_fence, "record_drop", lambda _o, _n, _s, why, *, item: dropped.append((why, item))
    )
    row = await consolidate._write_canonical(
        _CH([]),
        _input(),
        _Emb(),
        _two_row_cluster(),
        canonical_key="dog",
        canonical_value="the deploy key is sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
        now_naive=_T0,
        claimed=set(),
    )
    assert row["sensitivity"] == "high" and dropped == [("fence_failed", "a/-/dog")]
    assert commits[0]["documented"] == [False]


@pytest.mark.asyncio
async def test_a_high_summary_never_takes_a_merged_rows_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The merged row at the model's key keeps vectors only a fence would hide: the summary
    takes the cluster's own key, and the row is tombstoned with its vectors."""
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    over = next(r for r in cluster if r["memory_key"] == "dog_food")
    commits = _canonical_writes(monkeypatch, _existing(over["memory_id"], "dog_food"))
    row = await consolidate._write_canonical(
        _CH([]),
        _input(),
        _Emb(),
        cluster,
        canonical_key="dog_food",
        canonical_value="the deploy key is sk-proj-test-ABCDEFGHIJKLMNOPQRSTUVWX0123",
        now_naive=_T0,
        claimed=set(),
    )
    assert row["memory_key"] == consolidate._cluster_key("dog_food", cluster)
    assert row["memory_id"] != over["memory_id"] and commits[0]["read"] == [None]
    batches = await _retire(monkeypatch, cluster, canonical_key=row["memory_key"])
    assert batches[0] == [r["memory_id"] for r in cluster]  # both merged rows tombstoned


@pytest.mark.asyncio
async def test_a_summary_that_cannot_be_embedded_is_recorded_and_nothing_written(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import server.embedding as embmod

    class _Down(_Emb):
        async def embed(self, text):
            raise RuntimeError("embedder down")

    ch = _CH(_two_row_cluster())
    _wire(monkeypatch, ch, _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "v"})))
    monkeypatch.setattr(embmod, "get_embedder", lambda: _Down())
    out = await handle_memory_consolidate(_input())
    assert (out.clusters_failed, out.consolidated) == (1, [])
    assert not [t for t, _ in ch.inserts if t in ("loom.memory", "loom.memory_embeddings")]
    [run] = [r for t, rows in ch.inserts if t == "loom.consolidation_runs" for r in rows]
    assert (run["status"], run["failure_reason"]) == ("failed", "the summary could not be embedded")


@pytest.mark.asyncio
async def test_a_summary_over_a_deleted_head_goes_above_it_on_clickhouse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted head is read too, so a summary lands above one whose writer's clock ran
    ahead instead of below its tombstone, never served."""
    from server.core import pg
    from server.memory.app import consolidate
    from server.memory.store.rows import next_version

    asked: list[bool] = []
    ahead = _existing("33333333-3333-3333-3333-333333333333", "dog").model_copy(
        update={"version": next_version() + 10**12, "tombstoned_at": _T0}
    )

    async def _head(identity, *, memory_key, include_tombstoned=False, **_):
        asked.append(include_tombstoned)
        return ahead

    commits = _canonical_writes(monkeypatch)
    monkeypatch.setattr("server.memory.store.get_by_key", _head)
    monkeypatch.setattr(pg, "enabled", lambda: False)
    row = await consolidate._write_canonical(
        _CH([]),
        _input(),
        _Emb(),
        _two_row_cluster(),
        canonical_key="dog",
        canonical_value="v",
        now_naive=_T0,
        claimed=set(),
    )
    assert asked == [True] and row["version"] >= ahead.version + 2
    assert commits[0]["read"] == [ahead]


@pytest.mark.asyncio
async def test_two_clusters_choosing_one_key_leave_it_to_the_one_whose_row_is_there(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The first to read the key finds the other's row and takes its own key; the key is
    not left claimed, so the cluster holding that row writes over it."""
    from server.memory.app import consolidate

    owner = _two_row_cluster()  # dog_food (session '-') and dog_name
    other = [
        _cand("cat_food", "55555555-5555-5555-5555-555555555555", age_days=1, emb=[0, 1.0, 0, 0]),
        _cand("cat_name", "66666666-6666-6666-6666-666666666666", age_days=2, emb=[0, 1.0, 0, 0]),
    ]
    over = next(r for r in owner if r["memory_key"] == "dog_food")
    commits = _canonical_writes(monkeypatch, _existing(over["memory_id"], "dog_food"))
    claimed: set[str] = set()
    written = [
        await consolidate._write_canonical(
            _CH([]),
            _input(),
            _Emb(),
            cluster,
            canonical_key="dog_food",
            canonical_value="v",
            now_naive=_T0,
            claimed=claimed,
        )
        for cluster in (other, owner)
    ]
    assert [r["memory_key"] for r in written] == [
        consolidate._cluster_key("dog_food", other),
        "dog_food",
    ]
    assert written[1]["memory_id"] == over["memory_id"]  # over its own row, not beside it
    assert [c["read"] for c in commits if "read" in c][1] == [
        _existing(over["memory_id"], "dog_food")
    ]


@pytest.mark.asyncio
async def test_the_key_a_summary_gave_way_to_is_the_one_reported_and_kept(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The result and the retire name the key written, not the one the model chose."""
    from server.memory.app import consolidate

    ch = _CH(_two_row_cluster())
    _wire(monkeypatch, ch, _LLM(json.dumps({"canonical_key": "dog", "canonical_value": "v"})))
    kept: list[str] = []

    async def _written(*_a, **_k):
        return {"memory_key": "dog_food", "memory_type": "semantic"}

    async def _retire(*_a, canonical_key, **_k):
        kept.append(canonical_key)
        return True

    monkeypatch.setattr(consolidate, "_write_canonical", _written)
    monkeypatch.setattr(consolidate, "_retire_merged", _retire)
    out = await handle_memory_consolidate(_input())
    assert [r.canonical_key for r in out.consolidated] == kept == ["dog_food"]


@pytest.mark.asyncio
async def test_a_merged_row_held_since_the_read_is_left_live_and_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    moved = next(r for r in cluster if r["memory_key"] == "dog_name")
    dropped: list[tuple[str, str, int]] = []
    monkeypatch.setattr(
        consolidate,
        "record_drop",
        lambda _o, _n, _s, why, *, item, n: dropped.append((why, item, n)),
    )
    batches = await _retire(
        monkeypatch, cluster, canonical_key="dog", held=frozenset({moved["memory_id"]})
    )
    assert dropped == [("merged_row_left_live", "dog_name", 1)]
    # only the row tombstoned loses its search rows and chunks
    assert ["chunks", "dog_name"] not in batches and ["chunks", "dog_food"] in batches


@pytest.mark.asyncio
async def test_a_merged_row_deleted_since_the_read_is_not_held(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Gone, not left live: nothing is recorded and the retire is complete."""
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    deleted = next(r for r in cluster if r["memory_key"] == "dog_name")
    dropped: list[str] = []
    monkeypatch.setattr(
        consolidate, "record_drop", lambda _o, _n, _s, why, **_: dropped.append(why)
    )
    await _retire(monkeypatch, cluster, canonical_key="dog", gone=frozenset({deleted["memory_id"]}))
    assert dropped == []


@pytest.mark.asyncio
async def test_a_high_summary_with_its_own_key_taken_too_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The model's key holds a merged row and the cluster's own key a row the merge never
    read: nothing is free, and neither key is read twice."""
    from server.memory.app import consolidate

    cluster = _two_row_cluster()
    own = consolidate._cluster_key("dog_food", cluster)
    read: list[str] = []
    unread = _existing("33333333-3333-3333-3333-333333333333", own)

    async def _head(identity, *, memory_key, **_):
        read.append(memory_key)
        return unread if memory_key == own else None

    monkeypatch.setattr("server.memory.store.get_by_key", _head)
    with pytest.raises(consolidate._KeyTaken):
        await consolidate._summary_target(_input(), cluster, "dog_food", set(), high=True)
    assert read == [own]  # the merged row's key is skipped before any read


@pytest.mark.asyncio
async def test_a_summary_that_cannot_be_embedded_claims_no_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The embed runs before the key is chosen: another cluster of the pass may still take
    the model's key."""
    from server.memory.app import consolidate

    class _Down(_Emb):
        async def embed(self, text):
            raise RuntimeError("embedder down")

    commits = _canonical_writes(monkeypatch)
    claimed: set[str] = set()
    with pytest.raises(consolidate._EmbedFailed):
        await consolidate._write_canonical(
            _CH([]),
            _input(),
            _Down(),
            _two_row_cluster(),
            canonical_key="dog",
            canonical_value="v",
            now_naive=_T0,
            claimed=claimed,
        )
    assert claimed == set() and commits == []


@pytest.mark.asyncio
async def test_a_cluster_that_cannot_be_embedded_leaves_its_key_to_the_other(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two clusters choose one key and one cannot be embedded: the other writes the key
    itself, not the cluster's own fallback key."""
    import server.embedding as embmod
    from server.settings import settings

    monkeypatch.setattr(settings, "consolidation_merge_concurrency", 1)  # the failing one first

    rows = [
        _cand(f"{topic}{i}", f"{n}{i}" * 8, age_days=i, emb=emb, entities=[topic])
        for n, (topic, emb) in enumerate([("a", [1.0, 0, 0, 0]), ("b", [0, 1.0, 0, 0])])
        for i in (1, 2)
    ]

    embedded: list[str] = []

    class _OneDown(_Emb):
        async def embed(self, text):
            embedded.append(text)
            if text == "a merged":
                raise RuntimeError("embedder down")
            return await super().embed(text)

    class _SameKey(_LLM):
        async def complete(self, system, user, max_tokens=256):
            topic = next(t for t in "ab" if f"{t}1" in user)
            self.text = json.dumps({"canonical_key": "pets", "canonical_value": f"{topic} merged"})
            return await super().complete(system, user, max_tokens)

    ch = _CH(rows)
    _wire(monkeypatch, ch, _SameKey(""))
    monkeypatch.setattr(embmod, "get_embedder", lambda: _OneDown())
    out = await handle_memory_consolidate(_input())
    written = [r for t, batch in ch.inserts if t == "loom.memory" for r in batch]
    summaries = [r["memory_key"] for r in written if r.get("tombstoned_at") is None]
    assert embedded == ["a merged", "b merged"]  # the failing cluster went first
    assert (out.clusters_failed, summaries) == (1, ["pets"])
