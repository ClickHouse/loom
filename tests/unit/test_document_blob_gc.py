"""document_blob_gc: the sweep walks one namespace at a time, reads each in
batches after a sha cursor, deletes with the age guard, counts what the delete
removed, and an idle pass says so."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from server.knowledge.store import blobs
from server.knowledge.workers import blob_gc
from server.settings import settings


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    # orphans: (org, ns) -> shas no live document names; spared: shas the
    # aged delete keeps (restamped by a write in flight).
    state: dict[str, Any] = {"orphans": {}, "spared": set(), "deleted": [], "reads": [], "idle": 0}

    async def namespaces(after: tuple[str, str], limit: int, **_: Any) -> list[tuple[str, str]]:
        return [k for k in sorted(state["orphans"]) if k > after][:limit]

    async def unreferenced(
        org: str, ns: str, min_age_s: int, limit: int, *, after: str = "", **_: Any
    ) -> list[str]:
        assert min_age_s == settings.document_blob_min_age_seconds
        state["reads"].append((org, ns, after))
        return [s for s in sorted(state["orphans"][(org, ns)]) if s > after][:limit]

    async def delete(
        org: str, ns: str, shas: list[str], *, min_age_s: int = 0, **_: Any
    ) -> blobs.Deleted:
        gone = [s for s in shas if s not in state["spared"]]
        state["deleted"].append((org, ns, gone, min_age_s))
        state["orphans"][(org, ns)] -= set(gone)
        return blobs.Deleted(len(gone))

    monkeypatch.setattr(blobs, "namespaces", namespaces)
    monkeypatch.setattr(blobs, "unreferenced", unreferenced)
    monkeypatch.setattr(blobs, "delete", delete)
    monkeypatch.setattr(blob_gc.pass_context, "mark_idle", lambda: state.update(idle=1))
    return state


def test_a_pass_walks_namespaces_and_counts_only_what_was_deleted(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(blob_gc, "_BATCH", 2)
    monkeypatch.setattr(blob_gc, "_NAMESPACES", 1)
    store["orphans"] = {("o", "a"): {"1", "2", "3"}, ("o", "b"): {"4"}}
    store["spared"] = {"2"}  # restamped by an upload between the read and the delete
    out = asyncio.run(blob_gc.run_pass())
    age = settings.document_blob_min_age_seconds
    assert store["deleted"] == [
        ("o", "a", ["1"], age),
        ("o", "a", ["3"], age),
        ("o", "b", ["4"], age),
    ]
    # The cursor moves past the spared row: it is not read again this pass.
    assert store["reads"] == [("o", "a", ""), ("o", "a", "2"), ("o", "b", "")]
    assert out == blob_gc.BlobGcResult(deleted=3, namespaces=2) and store["idle"] == 0


def test_a_pass_with_nothing_to_delete_is_idle(store: dict[str, Any]) -> None:
    store["orphans"] = {("o", "a"): set()}
    assert asyncio.run(blob_gc.run_pass()).deleted == 0
    assert store["deleted"] == [] and store["idle"] == 1


def test_the_sweep_runs_on_its_own_switch_and_cadence(monkeypatch: pytest.MonkeyPatch) -> None:
    from server.workers import registry

    monkeypatch.setattr(settings, "document_blob_gc_interval_seconds", 1234)
    meta = registry.worker_meta()["document_blob_gc"]
    assert meta == {"enabled": settings.document_blob_gc_enabled, "interval_seconds": 1234}
    monkeypatch.setattr(settings, "document_blob_gc_enabled", False)
    assert registry.worker_meta()["document_blob_gc"]["enabled"] is False
    assert "document_blob_gc" not in {s.name for s in registry.enabled_workers()}


def test_a_kept_row_and_a_refused_object_delete_are_each_the_pass_s_failure(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    from server.core.object_store import ObjectStoreError

    answers = iter(
        [
            blobs.Deleted(0, kept=1),  # a row naming an object outside the bucket
            blobs.Deleted(1, error=ObjectStoreError("delete failed", operation="delete")),
        ]
    )

    async def delete(*args: Any, **kw: Any) -> blobs.Deleted:
        return next(answers)

    failures: list[str] = []
    monkeypatch.setattr(blobs, "delete", delete)
    monkeypatch.setattr(
        blob_gc.pass_context, "note_failure", lambda e, what="": failures.append(what)
    )
    monkeypatch.setattr(blob_gc, "_BATCH", 1)
    store["orphans"] = {("o", "a"): {"1", "2"}}
    asyncio.run(blob_gc.sweep_namespace("o", "a", settings.document_blob_min_age_seconds))
    assert failures == [
        "document_blob_gc: unreachable objects",
        "document_blob_gc: object delete",
    ]


def test_a_pass_s_bucket_deletes_count_in_the_worker_s_written_rows(
    store: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    import contextlib
    from types import SimpleNamespace

    from server.workers import status_store

    async def two_objects(min_age_s: int) -> int:
        return 2

    rec = SimpleNamespace(outcome=None)
    monkeypatch.setattr(blob_gc, "_sweep_bucket", two_objects)
    monkeypatch.setattr(blob_gc.settings, "document_s3_bucket", "b")
    monkeypatch.setattr(blob_gc, "record_pass", lambda name: contextlib.nullcontext(rec))
    store["orphans"] = {("o", "a"): {"1"}}
    asyncio.run(blob_gc.run_pass())
    _, _, written = status_store.counts(rec.outcome)
    assert written == 3, rec.outcome  # one original's rows and two bucket objects


def test_a_batch_of_originals_binds_as_one_parameter() -> None:
    """The delete binds a batch's shas in one parameter: it must fit the field."""
    from server.core.ch_params import chunks

    batch = [f"{i:064x}" for i in range(blob_gc._BATCH)]
    assert len(list(chunks(batch))) == 1
    assert blob_gc._BATCH * blob_gc._MAX_BATCHES == 20_000  # the docs' bound per namespace
