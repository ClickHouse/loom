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
from server.memory.app.consolidate import _consolidation_canonical_row, handle_memory_consolidate
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

    ch_rows_by_id = ch.rows
    monkeypatch.setattr(lifecycle, "get_by_id", _get_by_id)


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

    async def _tomb(org, ns, agent, user_id, session_id, key, *, ts, ch=None):
        calls.append((session_id, key, ts))
        return 1

    monkeypatch.setattr(memory_repo, "tombstone_embeddings_for_key", _tomb)
    out = await handle_memory_consolidate(_input())
    assert len(out.consolidated) == 1
    assert sorted(k for _, k, _ in calls) == ["dog_food", "dog_name"], (
        "every merged-from key, not the canonical"
    )
    assert all(isinstance(ts, datetime) for _, _, ts in calls)


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

    async def _tomb(org, ns, agent, user_id, session_id, key, *, ts, ch=None):
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
            mr.tombstone_embeddings_for_key("o", "n", "a", "-", "s1", "dog_name", ts=ts, ch=ch)
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


def test_consolidate_results_follow_cluster_order_not_completion_order(monkeypatch) -> None:
    """Three clusters whose merge calls complete in reverse order must still be
    reported in cluster order, with the bounded semaphore in the loop."""
    import asyncio
    import inspect

    from server.memory.app import consolidate as consolidate
    from server.memory.app import ops as ops
    from server.settings import settings

    src = inspect.getsource(consolidate.handle_memory_consolidate)
    assert "Semaphore(max(1, int(_settings.consolidation_merge_concurrency)))" in src
    assert "slots[slot] = MemoryConsolidateResult(" in src
    assert "Later entries update earlier ones, never the reverse" in consolidate._MERGE_SYSTEM
    assert settings.consolidation_merge_concurrency >= 1
    # The sequential fallback is one knob away.
    monkeypatch.setattr(settings, "consolidation_merge_concurrency", 1)
    assert asyncio.Semaphore(max(1, int(settings.consolidation_merge_concurrency)))._value == 1


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
    )
    assert row["sensitivity"] == "medium"
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
    assert "300" in runs[0]["failure_reason"] and "3 march 2023" in runs[0]["failure_reason"]
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
